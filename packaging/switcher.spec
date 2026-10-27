%global debug_package %{nil}
# No binaries in this package: skip the ldconfig post-processing step.
%global __brp_ldconfig %{nil}
%global __brp_ldconfig_cleanup %{nil}
# linkdupes/selabel need SELinux labels, unavailable inside the build sandbox.
%global __brp_linkdupes /bin/true

Name:           switcher
Version:        1.1.0
Release:        1%{?dist}
Summary:        Punto-Switcher-style automatic keyboard layout correction
License:        MIT
URL:            https://github.com/switcher-linux/switcher
BuildArch:      noarch
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  python3
Requires:       python3-evdev
# xprop - fallback для определения активного окна (исключения по процессам);
# python3-xlib, если стоит, делает тот же запрос дешевле (без процессов).
Requires:       xprop
Requires:       python3-pyside6
Requires:       hunspell
Requires:       hunspell-ru
Requires:       hunspell-en

%description
Switcher Linux watches the keyboard, detects words typed in the wrong layout
and retypes them synchronously in the right one. The daemon owns the event
stream (EVIOCGRAB) and forwards keys through uinput, so a correction can never
race with the user typing - the same trick Punto Switcher uses.

Features:
  * RU/EN autocorrect with a 146k Russian dictionary and hunspell validation
  * learn / ban words (Ctrl+Alt+T / Ctrl+Alt+B), rollback (Ctrl+Alt+Z)
  * per-program exclusions: the daemon releases the keyboard for a game
    (dota2 and friends) and stops intercepting it at all, with a tray
    notification when it steps aside and when it takes over again
  * system tray icon and settings window (PySide6): word lists, exclusions,
    behaviour, log
  * single-instance daemon, IPC over $XDG_RUNTIME_DIR/switcher_linux.sock

%prep
%setup -q

%build

%install
rm -rf %{buildroot}
install -d %{buildroot}%{_prefix}/lib/switcher
cp -a switcher %{buildroot}%{_prefix}/lib/switcher/
find %{buildroot}%{_prefix}/lib/switcher -name __pycache__ -type d -prune -exec rm -rf {} +
%{python3} -m compileall -q -d %{_prefix}/lib/switcher %{buildroot}%{_prefix}/lib/switcher/switcher

install -Dm755 packaging/switcher            %{buildroot}%{_bindir}/switcher
install -Dm755 packaging/switcher-tray       %{buildroot}%{_bindir}/switcher-tray
install -Dm755 packaging/switcher-ctl        %{buildroot}%{_bindir}/switcher-ctl
install -Dm644 packaging/switcher.service    %{buildroot}%{_userunitdir}/switcher.service
install -Dm644 packaging/70-switcher-uinput.rules %{buildroot}%{_udevrulesdir}/70-switcher-uinput.rules
install -Dm644 packaging/switcher.desktop    %{buildroot}%{_datadir}/applications/switcher.desktop
install -Dm644 packaging/switcher-tray.desktop %{buildroot}%{_sysconfdir}/xdg/autostart/switcher-tray.desktop
install -Dm644 packaging/switcher.svg        %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/switcher.svg
install -Dm644 packaging/LICENSE             %{buildroot}%{_datadir}/licenses/switcher/LICENSE

%post
udevadm control --reload-rules >/dev/null 2>&1 || :
echo "Switcher установлен."
echo "1) Добавь себя в группу input (нужно для захвата клавиатуры):"
echo "     sudo usermod -aG input ВАШ_ЛОГИН"
echo "   и перелогинься (группы применяются при новом входе)."
echo "2) Включи демон:"
echo "     systemctl --user enable --now switcher"
echo "3) Трей поднимется сам при следующем входе в сессию."

%postun
udevadm control --reload-rules >/dev/null 2>&1 || :

%files
%{_prefix}/lib/switcher
%{_bindir}/switcher
%{_bindir}/switcher-tray
%{_bindir}/switcher-ctl
%{_userunitdir}/switcher.service
%{_udevrulesdir}/70-switcher-uinput.rules
%{_datadir}/applications/switcher.desktop
%config(noreplace) %{_sysconfdir}/xdg/autostart/switcher-tray.desktop
%{_datadir}/icons/hicolor/scalable/apps/switcher.svg
%{_datadir}/licenses/switcher/LICENSE

%changelog
* Tue Oct 27 2026 Sanek <LICEMER.ME@gmail.com> - 1.1.0-1
- Per-program exclusions (exclude_focus / exclude_running): while an excluded
  program owns the screen the daemon ungrabs every keyboard, so the game gets
  raw input with no uinput device and no modifier staging in the path;
  hotkeys and autocorrection are off for it by design. New module
  switcher/focus.py (X11/XWayland _NET_ACTIVE_WINDOW + /proc names, xlib or
  xprop), new IPC commands exclude.list/add/del, new settings tab.
- Exclusions dialog offers the running applications: Steam games (title from
  the appmanifest, pattern from the game binary), desktop-launched apps (their
  systemd scope, title from the .desktop Name) and X11/XWayland windows; the
  mode is pre-selected per application, because focus only works for X11
  windows. A name can always be typed by hand (switcher/procs.py).
- Tray notification when the switcher steps aside for an excluded program and
  when it comes back (notify_exclude), debounced against Alt+Tab flicker.
- Fix: a trailing "." "," ";" "'" "[" "]" or "`" typed on the US layout may be
  a Russian letter (ю б ж э х ъ ё), so words like "gjkyjcnm." (полностью) and
  "yj;" (нож) were never corrected. decide() now tries every split point of the
  trailing punctuation.
- config.toml: string arrays are supported (excluded lists); the daemon
  re-applies them without a restart.
- README.en.md: English documentation for GitHub.
- Requires: xprop (focus fallback).

* Tue Oct 27 2026 Sanek <LICEMER.ME@gmail.com> - 1.0.0-2
- Unify the copyright line in packaging/LICENSE with the repo root (LICEMER);
  broaden .gitignore (caches, patch leftovers, local secrets).

* Tue Oct 27 2026 Sanek <LICEMER.ME@gmail.com> - 1.0.0-1
- 1.0 release. Stable RU/EN autocorrect built on EVIOCGRAB + uinput:
  full punctuation map (SHIFT_US2RU), Super key tap fix, tray start/stop
  toggle, IPC shutdown, legacy prototypes moved to legacy/.

* Tue Oct 27 2026 Sanek <LICEMER.ME@gmail.com> - 0.3.0-6
- Tray: the daemon entry flips to "stop the daemon" while it is running;
  new IPC command shutdown (answers first, then exits and ungrabs).

* Tue Oct 27 2026 Sanek <LICEMER.ME@gmail.com> - 0.3.0-5
- Super key fix: a modifier tapped alone (Win/Alt/Ctrl+Shift) is now
  replayed as press+release instead of being dropped by the staging
  logic; our own hotkeys swallow their modifiers instead.

* Tue Oct 27 2026 Sanek <LICEMER.ME@gmail.com> - 0.3.0-4
- Full punctuation map: shifted symbols on the letter keys ([ ] ; #' , . / \) 
  now survive a layout switch; SHIFT_US2RU keeps RU2US intact.

* Tue Oct 27 2026 Sanek <LICEMER.ME@gmail.com> - 0.3.0-3
- Punctuation fix: track Shift in the interceptor and map shifted US symbols
  to RU punctuation (Shift+6 = colon), so #"ok:#" no longer becomes #"jr6#"

* Tue Oct 27 2026 Sanek <LICEMER.ME@gmail.com> - 0.3.0-2
- Fix false autocorrect: 2-letter words and words with digits are ignored,
  target must contain a vowel (kills ok -> jr / Jr. abbreviation)

* Tue Oct 27 2026 Sanek <LICEMER.ME@gmail.com> - 0.3.0-1
- Initial package: intercept daemon, IPC socket, PySide6 tray and settings window
