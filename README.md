# Switcher Linux

[Русский](README.md) | [English](README.en.md)

Автоматическое исправление слов, набранных в неправильной раскладке (RU/EN),
для KDE Plasma (X11 и Wayland). Идейный аналог Punto Switcher.

Демон захватывает клавиатуру через `EVIOCGRAB` и пересылает события в систему
через `uinput`. Когда слово завершено (пробел, Enter, Tab), он проверяет его
словарями и, если слово осмысленно только в другой раскладке, стирает его и
перепечатывает в правильной — синхронно, поэтому коррекция не гоняется с
набором. Пока демон работает, клавиатура идёт только через него.

## Возможности

- Автокоррекция RU/EN: словари на 146k (ru) и 46k (en) + проверка hunspell
- Умный отсев: короткие слова (по умолчанию < 3 букв), слова с цифрами и
  кандидаты без гласных не трогаются — `ok`, `Jr.`, `H2O` остаются как есть
- Пунктуация переживает переключение: `, . ; : ? ! " № / \ - _ + =` и
  shifted-символы цифрового ряда вставляются на правильную физическую клавишу
- Обучение и бан слов: `Ctrl+Alt+T` / `Ctrl+Alt+B`, откат `Ctrl+Alt+Z`
- Пауза `Ctrl+Alt+P`
- Исключения по программам: добавь игру (например `dota2`) в трее — свитчер
  отпускает для неё клавиатуру и не вмешивается вообще: ни перехвата, ни
  staging-модификаторов, ни хоткеев. Есть режим «пока окно активно» (игры,
  X11/XWayland) и «пока процесс запущен»; трей показывает уведомление
  «Switcher не активен: dota2» и сообщает, когда перехват вернулся.
  В «Настройки → Исключения → Добавить...» есть список запущенных приложений
  (Steam-игры с человеческим названием, десктоп-приложения, X11-окна), режим
  подставляется сам; имя процесса можно вписать и руками
- Иконка в трее и окно настроек (PySide6): списки слов, исключения, поведение, лог
- Один экземпляр демона, IPC по `$XDG_RUNTIME_DIR/switcher_linux.sock`

## Установка (Fedora, RPM)

```fish
sudo dnf install ./switcher-*.rpm
sudo usermod -aG input $USER     # доступ к /dev/input, требуется перелогин
systemctl --user enable --now switcher
```

Трей поднимется сам при следующем входе в сессию (autostart). Проверка:

```fish
switcher-ctl status
switcher-ctl log 20
```

## Горячие клавиши

| Комбинация    | Действие                          |
|---------------|-----------------------------------|
| `Ctrl+Alt+P`  | пауза / продолжить                |
| `Ctrl+Alt+Z`  | откатить последнюю коррекцию      |
| `Ctrl+Alt+B`  | забанить слово (больше не трогать)|
| `Ctrl+Alt+T`  | обучить слово (всегда исправлять) |

## Конфигурация

`~/.config/switcher_linux/config.toml` — правится и вручную, демон
перечитывает по `switcher-ctl config.reload` или из окна настроек трея.

Ключевые параметры: `min_len`, `settle`, `settle_dbus`, `gap_bs`, `gap_type`,
`debug`, `exclude_focus`, `exclude_running`, `notify_exclude` (уведомления трея
о том, что свитчер уступил программу). Выученные и забаненные слова лежат
рядом: `learned.json`, `blocked.json`.

Исключения — это массивы имён процессов:

```toml
# игнорировать, пока активно окно программы (игры: X11/XWayland)
exclude_focus = ["dota2"]
# игнорировать всё время, пока процесс запущен (фокус не важен)
exclude_running = []
```

Добавить/убрать без правки файла:

```fish
switcher-ctl focus                                      # кто сейчас в фокусе
switcher-ctl exclude.add pattern=dota2 mode=focus
switcher-ctl exclude.del pattern=dota2 mode=focus
```

## Запуск из исходников

```fish
cd ~/Документы/Projects/Switcher_linux
python3 -m venv .venv
.venv/bin/pip install evdev pyside6
.venv/bin/python -m switcher.main      # демон
.venv/bin/python -m switcher.gui       # трей
.venv/bin/python -m switcher.ctl status
```

Демону нужны права на чтение `/dev/input/event*` — группа `input`.

## Слои

- `switcher/intercept.py` — захват клавиатуры, буфер слова, удержание
  boundary-клавиши, hotkeys
- `switcher/focus.py` — что за окно сейчас впереди (X11/XWayland: `_NET_ACTIVE_WINDOW`,
  `_NET_WM_PID`, `WM_CLASS`) и имена процессов из `/proc` — на этом держатся исключения
- `switcher/injector.py` — uinput-эмуляция: backspace, переключение раскладки,
  перепечатка символа на нужной физической клавише
- `switcher/core.py` — решение (decide), словари, таблицы раскладок `US2RU` /
  `SHIFT_US2RU`
- `switcher/daemon.py` — сборка всего, IPC, состояние
- `switcher/gui/` — трей и настройки
- `legacy/` — ранние прототипы (PyQt5), к текущему коду отношения не имеют

## Требования

- Linux (KDE Plasma, X11 или Wayland), Python 3.9+
- `evdev`, `PySide6`, `hunspell` + словари `ru_RU`, `en_US`
- для исключений по активному окну: `python-xlib` (быстро) или `xprop` (fallback)

## Лицензия

MIT — см. `packaging/LICENSE`.
