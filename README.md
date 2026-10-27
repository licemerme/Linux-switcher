# Switcher Linux

Автоматическое исправление слов, набранных в неправильной раскладке (RU/EN),
для KDE / X11. Идейный аналог Punto Switcher.

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
- Иконка в трее и окно настроек (PySide6): списки слов, поведение, лог
- Один экземпляр демона, IPC по `$XDG_RUNTIME_DIR/switcher_linux.sock`

## Установка (Fedora, RPM)

```fish
sudo dnf install ./switcher-1.0.0-1.fc44.noarch.rpm
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
`debug`. Выученные и забаненные слова лежат рядом: `learned.json`,
`blocked.json`.

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
- `switcher/injector.py` — uinput-эмуляция: backspace, переключение раскладки,
  перепечатка символа на нужной физической клавише
- `switcher/core.py` — решение (decide), словари, таблицы раскладок `US2RU` /
  `SHIFT_US2RU`
- `switcher/daemon.py` — сборка всего, IPC, состояние
- `switcher/gui/` — трей и настройки
- `legacy/` — ранние прототипы (PyQt5), к текущему коду отношения не имеют

## Требования

- Linux (KDE Plasma, X11), Python 3.9+
- `evdev`, `PySide6`, `hunspell` + словари `ru_RU`, `en_US`

## Лицензия

MIT — см. `packaging/LICENSE`.
