# Архитектура Switcher Linux

Документ описывает внутреннее устройство пакета `switcher` (v1.0.0): слои,
поток событий, IPC, файлы состояния и инфраструктурные грабли. Личных путей и
железа здесь нет — всё строится из XDG, поэтому документ безопасно публиковать.

## 1. Назначение и границы

Автокоррекция слова, набранного не в той раскладке (RU/EN, пары
QWERTY ↔ ЙЦУКЕН). Демон **владеет** клавиатурой: захватывает физические
устройства через `EVIOCGRAB` и переизлучает каждое событие 1:1 через
собственное uinput-устройство. Благодаря этому коррекция выполняется
синхронно, внутри цикла событий, и не гоняется с набором (Punto-подход).

Что НЕ делается: не правится орфография, нет своей раскладки/движка ввода,
нет работы с сетью и секретами, нет root-демона.

## 2. Среда и зависимости

| Компонент | Требование |
|-----------|-----------|
| ОС | Linux, Fedora-подобная (пакет RPM) |
| Сессия | KDE Plasma, X11 **и** Wayland (раскладка — через KWin DBus) |
| Python | 3.9+ (`tomllib` → фактически 3.11+) |
| Библиотеки | `evdev` (захват/uinput), `PySide6` (трей) |
| Внешнее | `hunspell` + словари `ru_RU`, `en_US`; `busctl` (KDE DBus) |
| Права | членство в группе `input`, запись в `/dev/uinput`; root не нужен |

## 3. Структура репозитория

```
switcher/
  core.py         чистая логика: карты раскладок, словари, decide() — без I/O
  capture.py      карта KEY-код → US-символ, поиск клавиатур, пассивный ридер
  intercept.py    захват клавиатур (EVIOCGRAB), буфер слова, staged-модификаторы,
                  hotkeys, удержание boundary-события
  injector.py     uinput-эмуляция: backspace, печать строки/символа, toggle
  layout_probe.py живая раскладка: KDE DBus → setxkbmap → kxkbrc → "us"
  spell.py        hunspell -a (pipe-режим), кэш, селфтест
  ipc.py          JSON-lines по unix-сокету: сервер + клиент
  config.py       пути (XDG) и config.toml (плоский TOML, DEFAULTS)
  daemon.py       сборка всего + IPC-диспетчер + состояние (точка входа)
  main.py         алиас на daemon.main (`-m switcher.main` = `-m switcher.daemon`)
  ctl.py          CLI поверх IPC
  gui/            трей (app.py), окно настроек (window.py), клиент (client.py)
packaging/        spec, обёртки bin, user-unit, udev-правило, .desktop, иконка, LICENSE
legacy/           ранние прототипы (PyQt5), к текущему коду не подключены
```

## 4. Поток событий (intercept mode)

```
физическая клавиатура (/dev/input/event*)
        │  EVIOCGRAB — приложения напрямую НЕ видят
        ▼
intercept.Interceptor  ──►  буфер слова в US-ПОЗИЦИЯХ (не в символах)
        │                        │
        │  обычная клавиша       │ boundary (Space/Enter/KPEnter/Tab)
        ▼                        ▼
 uinput "switcher-keyboard"   удержать boundary
 (1:1 переизлучение)              │
                                  ▼
                     visible(raw, layout) → строка, которую видит юзер
                                  │
                                  ▼
                     core.Switcher.decide(word, layout)
                                  │
             none ◄───────────────┴───────────────► switch-rus / switch-eng
              │                                            │
   отпустить boundary                        backspace(len) → set_layout()
              │                              → type_string() → отпустить boundary
              ▼                                            │
        ввод продолжается                        IPC broadcast {"event":"correct"}
```

Пока идёт коррекция, нажатия пользователя копятся в evdev-буфере ядра и
выходят через десятки миллисекунд — это и есть «микропауза» Punto без порчи
текста. Восстановление после падения: ядро снимает grab само при закрытии fd,
плюс явный `stop()`/ungrab в `finally`.

Переключение раскладки: сначала KDE DBus (`org.kde.keyboard` `/Layouts`,
`setLayout` — синхронный, ждём `settle_dbus`), при недоступности — Ctrl+Shift
через uinput и ожидание `settle`. Перед коррекцией живая раскладка
перепроверяется (`layout_probe.current_layout`) — если юзер переключил мимо нас,
слово пересчитывается.

## 5. Слои и инварианты

- **core** не знает про evdev/файлы: на вход строка и раскладка, на выходе
  `(action, corrected)`. Юнит-тестируется запуском модуля.
- **Буфер хранит US-позиции**, а не символы: раскладка применяется на выходе
  (`US2RU` + `SHIFT_US2RU`), поэтому переключение посреди слова не ломает
  интерпретацию.
- **Модификаторы staged**: Shift/Ctrl/Alt/Meta не форвардятся сразу, а ждут,
  не сложатся ли в hotkey демона или в KDE-комбо; одиночный тап
  переигрывается press+release (иначе теряется Win/Alt).
- **Демон — единственный писатель** `learned.json`/`blocked.json`; трей и CLI
  ходят только через IPC.
- **Один экземпляр**: при старте `ping` по сокету; живой ответ → выход.

## 6. IPC

Транспорт: unix-сокет `$XDG_RUNTIME_DIR/switcher_linux.sock` (chmod 600),
JSON-lines, по потоку на клиента.

```
запрос  {"cmd": "<name>", "params": {...}}
ответ    {"ok": true, "data": ...} | {"ok": false, "error": "..."}
push     {"event": "correct"|"paused"|"config", ...}   # после {"cmd":"subscribe"}
```

| Команда | Назначение |
|---------|-----------|
| `ping` | живость демона (проверка единственного экземпляра) |
| `status` | пауза, раскладка, счётчики learned/blocked, конфиг, сокет |
| `pause` / `resume` | автоисправление вкл/выкл (клавиатура продолжает идти) |
| `reload` | перечитать словари hunspell |
| `learned.list` / `.add` / `.del` | выученные пары `{layout:{visible:target}}` |
| `blocked.list` / `.add` / `.del` | запрет автокоррекции слова |
| `config.get` / `config.set` / `config.reload` | параметры на лету + запись в файл |
| `log.tail` | последние N строк лога (1..500) |
| `shutdown` | ответить, затем выйти и снять grab |

CLI: `switcher-ctl [status|pause|resume|reload|ping|shutdown|learned|blocked|config|log N|config.set k=v ...]`.

## 7. Файлы, пути, состояние

Всё строится из XDG-переменных (`config.Paths`), никаких абсолютных домашних
путей в коде.

| Что | Путь |
|-----|------|
| Конфиг | `$XDG_CONFIG_HOME/switcher_linux/config.toml` (по умолчанию `~/.config/...`) |
| Выученные | `.../switcher_linux/learned.json` |
| Забаненные | `.../switcher_linux/blocked.json` |
| Лог | `$XDG_DATA_HOME/switcher_linux/switcher.log` |
| Сокет | `$XDG_RUNTIME_DIR/switcher_linux.sock` |

Плюрализм фолбэков (осознанный, закрывает «фантомный демон не запущен»):
каталог сокета — `$XDG_RUNTIME_DIR` → `/run/user/${UID}` → `/tmp`
(при отсутствии переменной, как под nohup/autostart); если в текущем runtime
каталоге сокета нет, а в `/run/user/${UID}` есть — используется существующий.

`config.toml` — плоский TOML, ключи фиксированы `DEFAULTS`, типы приводятся к
типу дефолта, неизвестные ключи игнорируются, запись атомарная (tmp+rename):

| Ключ | Дефолт | Смысл |
|------|--------|-------|
| `min_len` | 3 | минимум букв для словарной коррекции (жёсткий пол — 3) |
| `settle` | 0.45 | ожидание KDE при переключении через uinput, с |
| `settle_dbus` | 0.06 | то же для синхронного DBus, с |
| `gap_bs` | 0.012 | пауза между backspace, с |
| `gap_type` | 0.012 | пауза между печатаемыми символами, с |
| `debug` | false | писать в лог каждый FLUSH (решение по слову) |

Изученные решения живут не в конфиге: `learned.json` (принудительный маппинг
`layout → {видимое_слово: цель}`) и `blocked.json` (deny-list, отдельный файл,
чтобы чистить обучение без потери запретов). Оба применяются ДО фильтров длины.

## 8. Установка: пакет, юниты, права

RPM noarch, `Name: switcher`, `Release: 2`; код в `/usr/lib/switcher/switcher`,
bin-обёртки `/usr/bin/{switcher,switcher-tray,switcher-ctl}` — это `/bin/sh`,
которые выставляют `PYTHONPATH=/usr/lib/switcher` и зовут `python3 -m ...`.

| Артефакт | Куда ставится |
|----------|---------------|
| user-unit | `/usr/lib/systemd/user/switcher.service` (`WantedBy=graphical-session.target`) |
| udev | `/usr/lib/udev/rules.d/70-switcher-uinput.rules` (`uinput` и `event*` → `0660 input`) |
| автозапуск трея | `/etc/xdg/autostart/switcher-tray.desktop` (`%config(noreplace)`) |
| ярлык | `/usr/share/applications/switcher.desktop` |
| иконка | `/usr/share/icons/hicolor/scalable/apps/switcher.svg` |
| лицензия | `/usr/share/licenses/switcher/LICENSE` |

Юнит: `Type=simple`, `Restart=on-failure`, `KillSignal=SIGINT` — демон ловит
`KeyboardInterrupt` вокруг `Interceptor.run()` и снимает grab штатно, а не
жёстким kill. Сборка: `packaging/build-rpm.sh` → `temp/rpmbuild/RPMS/noarch/`.

Трей при ручном запуске демона предпочитает `<repo>/.venv/bin/python`, если он
есть (в системном интерпретаторе обычно нет `evdev`).

## 9. Грабли (не наступать повторно)

1. **uinput capabilities**: любой KEY-код, который может эмитить инжектор,
   обязан быть в списке capabilities виртуального устройства — иначе ядро
   молча дропает событие (так ломался toggle без `KEY_LEFTCTRL`).
2. **hunspell `-a`**: одна строка запроса = один ответ + пустая строка-разделитель.
   Строку-разделитель надо вычитывать, иначе пайп рассинхронизируется после
   первого слова. Запросы — только чистые буквы (`isalpha`), иначе один запрос
   даёт несколько ответов.
3. **US2RU vs SHIFT_US2RU** держим раздельно: `:` → `ж` в shifted-карте не
   должен перетирать `RU2US["ж"] = ";"`.
4. **Своё виртуальное устройство чёрное**: `capture.DEVICE_BLACKLIST` содержит
   `switcher` (и имена мышей с «клавиатурным» узлом), иначе рестарт захватит
   собственный вывод.
5. **Словари**: `ru_RU.dic` в KOI8-R, `en_US.dic` в UTF-8 — `load_dict`
   перебирает кодировки; «слово/флаги» режется по `/`.
6. **min_len**: словарная коррекция не ниже 3 букв, иначе 2-буквенный шум
   (`ок`→`jr`). Выученные пары проверяются раньше лимита.
7. **Слова с цифрами** не трогаются (коды, версии, пароли); цель без гласной
   отбрасывается (kills hunspell-аббревиатуры `Jr.`, `Dr.`).
8. **`shutdown` отвечает ДО выхода**: если снять цикл раньше, трей видит
   «нет соединения» вместо чистой остановки.
9. **Event-ноды перенумеровываются** при подключении BT-клавиатур —
   `Interceptor` раз в 2 с досканирует устройства.
10. **Сокет/Runtime**: не полагаться на `XDG_RUNTIME_DIR` (см. §7).
11. **Записывание истории git**: `temp/`, `*.rpm`, `*.log`, `.venv/`,
    `data/` — не в репозиторий; личные заметки тоже (см. `.gitignore`).

## 10. Отладка

```bash
python -m switcher.core          # карты + самопроверки decide()
python -m switcher.layout_probe  # каждый источник раскладки по отдельности
python -m switcher.injector      # виртуальное устройство печатает "king"
python -m switcher.capture       # дамп символов с физических клавиатур
python -m switcher.main --check "rjhjkm" "hello"   # решение без захвата клавиатуры
python -m switcher.main --debug  # лог каждого FLUSH/решения
python -m switcher.main --paused # только переизлучение, без коррекций
```

## 11. Ограничения

- Поддерживаются только раскладки `us`/`ru` (`eu`-карты — нет); карта
  соответствия захардкожена под стандартный YTsUKEN.
- Автокоррекция работает на границе слова: Space/Enter/Tab. Пунктуация внутри
  слова участвует как трейлинг и переносится на правильную физическую клавишу.
- Переключение раскладки вне KDE (нет DBus) — только Ctrl+Shift + ожидание.
- Никакого анализа контекста/предложений: правило локальное, по словарю.
