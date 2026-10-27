# Switcher Linux

[Русский](README.md) | **English**

Automatic fix-up of words typed in the wrong keyboard layout (RU/EN) for
KDE Plasma on X11 and Wayland. The idea is the same as Punto Switcher.

The daemon grabs the keyboard with `EVIOCGRAB` and re-emits every event through
`uinput`. When a word is finished (Space, Enter, Tab) it looks the word up in
the dictionaries and, if the word only makes sense in the other layout, deletes
it and retypes it in the right one — synchronously, so the correction can never
race with your typing. While the daemon runs, the keyboard only goes through it.

## Features

- RU/EN autocorrection: 146k Russian and 46k English words + hunspell checks for
  inflected forms (`продолжай`, `running`)
- Sanity filters: very short words (below 3 letters by default), words with
  digits and candidates without a single vowel are left alone — `ok`, `Jr.`,
  `H2O` stay as they are
- Punctuation survives the layout switch: `, . ; : ? ! " № / \ - _ + =` and
  shifted number-row symbols are re-typed on the correct physical key
- Teach and ban words: `Ctrl+Alt+T` / `Ctrl+Alt+B`, undo with `Ctrl+Alt+Z`
- Pause: `Ctrl+Alt+P`
- Per-program exclusions: add a game (say `dota2`) in the tray and the switcher
  releases the keyboard for it and keeps out of the way completely — no
  interception, no modifier staging, no hotkeys. Two modes: "while its window is
  focused" (games, X11/XWayland) and "while the process is running"; the tray
  pops "Switcher is inactive: dota2" and tells you when it takes over again.
  Settings → Exclusions → Add offers the running applications (Steam games with
  their real title, desktop apps, X11 windows) and pre-selects the mode; a
  process name can always be typed by hand
- Tray icon and settings window (PySide6): word lists, exclusions, behaviour, log
- Single daemon instance, IPC over `$XDG_RUNTIME_DIR/switcher_linux.sock`

## Why it owns the keyboard

A passive reader can never correct a word safely: while it backspaces and
retypes, a fast typist keeps typing and the two streams interleave, so the text
gets corrupted (that is why the old design simply aborted on every fast input).
Owning the event stream fixes it: keys pressed during a correction queue in the
kernel for a few tens of milliseconds and come out afterwards, already in the
new layout. The price is the Punto-style "micro pause" on a corrected word.

If the daemon dies, the kernel drops the grab when the file descriptor closes,
so the real keyboard works again. The daemon also ungrabs explicitly on exit.

## Install (Fedora, RPM)

```fish
sudo dnf install ./switcher-*.rpm
sudo usermod -aG input $USER     # access to /dev/input, re-login required
systemctl --user enable --now switcher
```

The tray comes up by itself on the next session login (autostart). Check it:

```fish
switcher-ctl status
switcher-ctl log 20
```

Build the package from a checkout:

```fish
./packaging/build-rpm.sh      # result: temp/rpmbuild/RPMS/noarch/
```

## Hotkeys

| Combination   | Action                                  |
|---------------|-----------------------------------------|
| `Ctrl+Alt+P` | pause / resume autocorrection           |
| `Ctrl+Alt+Z` | undo the last correction (toggles back) |
| `Ctrl+Alt+B` | undo it and never touch that word again |
| `Ctrl+Alt+T` | fix the last word and remember it       |

The hotkeys are swallowed: neither KWin nor the focused application ever sees
them. `Ctrl+Shift` (and `Meta+Alt+K` / `Meta+Alt+L`) switch the layout — the
daemon does that itself through KWin DBus and tracks the result.

## Configuration

`~/.config/switcher_linux/config.toml` — hand editing is fine; the daemon
re-reads it on `switcher-ctl config.reload` or from the tray settings window.

Main keys: `min_len`, `settle`, `settle_dbus`, `gap_bs`, `gap_type`, `debug`,
`exclude_focus`, `exclude_running`, `notify_exclude` (tray notifications when the
switcher steps aside for a program). Learned and banned words live next to it:
`learned.json`, `blocked.json`.

Exclusions are plain arrays of process names:

```toml
# ignore the switcher while this program's window is focused (games: X11/XWayland)
exclude_focus = ["dota2"]
# ignore it the whole time the process exists (focus does not matter)
exclude_running = []
```

Names are matched case-insensitively as substrings against the window class
(`WM_CLASS`), `/proc/PID/comm`, `argv[0]` and the executable path, plus up to
four ancestors of the window's PID. While an exclusion is in force the daemon
holds no grab at all, so the hotkeys do not work either — there is nothing left
to intercept, which is exactly the point. The tray notifies you about it: one
message when the program takes the keyboard over, one when the interception is
back (debounced, so a quick Alt+Tab does not spam you). `notify_exclude = false`
turns that off.

Add or remove one without touching the file:

```fish
switcher-ctl focus                                    # what window is in front
switcher-ctl exclude                                  # lists + what is active now
switcher-ctl exclude.add pattern=dota2 mode=focus
switcher-ctl exclude.del pattern=dota2 mode=focus
```

The "while its window is focused" mode can only identify X11/XWayland windows:
a Wayland compositor does not hand out the owner PID of a native Wayland window.
Games are not affected (they run through XWayland); for everything else use the
`running` mode, which ignores focus entirely. In the tray, "Ignore current
window" does this in one click.

## Run from source

```fish
git clone <this repo> && cd Switcher_linux
python3 -m venv .venv
.venv/bin/pip install evdev pyside6 python-xlib   # python-xlib is optional
.venv/bin/python -m switcher.main      # daemon
.venv/bin/python -m switcher.gui       # tray + settings
.venv/bin/python -m switcher.ctl status
```

The daemon needs read access to `/dev/input/event*` (group `input`) and write
access to `/dev/uinput` (udev rule in `packaging/`). No root, no setuid.

## Layers

- `switcher/intercept.py` — keyboard grab, word buffer, holding the boundary key,
  staged modifiers, hotkeys
- `switcher/focus.py` — which window is in front (`_NET_ACTIVE_WINDOW`,
  `_NET_WM_PID`, `WM_CLASS`) and process names from `/proc`: the base of the
  exclusion feature
- `switcher/injector.py` — uinput emulation: backspace, layout switch, retyping a
  character on the right physical key
- `switcher/core.py` — the decision (`decide()`), dictionaries, layout maps
  `US2RU` / `SHIFT_US2RU`; pure logic, no I/O
- `switcher/spell.py` — long-lived `hunspell -a` process, cache, selftest
- `switcher/layout_probe.py` — live layout: KWin DBus → `setxkbmap` → `kxkbrc`
- `switcher/ipc.py`, `switcher/daemon.py`, `switcher/ctl.py` — JSON-lines IPC over
  a unix socket, the daemon that wires everything together, and a small CLI
- `switcher/config.py` — XDG paths and `config.toml`
- `switcher/gui/` — tray and settings window
- `legacy/` — early PyQt5 prototypes, unrelated to the current code

## Requirements

- Linux, KDE Plasma (X11 or Wayland), Python 3.9+ (3.11+ for `tomllib`)
- `evdev`, `PySide6`
- `hunspell` with the `ru_RU` and `en_US` dictionaries
- `python-xlib` (fast) or `xprop` (fallback) for focus-based exclusions
- membership in the `input` group; no root

## Limitations

- Two layouts only: `us` and `ru`, hardcoded for the standard YTsUKEN map.
- Layout switching outside KDE (no DBus) falls back to a synthetic `Ctrl+Shift`.
- No sentence/context analysis: the rule is local and dictionary-based.
- While a modifier is held the daemon stages it, so a `Shift`+mouse-click
  combination does not reach the application until the next key is pressed.
- Focus-based exclusions see X11/XWayland windows only (see above).

## License

MIT — see `packaging/LICENSE`.
