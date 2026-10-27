%global debug_package %{nil}
# No binaries in this package: skip the ldconfig post-processing step.
%global __brp_ldconfig %{nil}
%global __brp_ldconfig_cleanup %{nil}
# linkdupes/selabel need SELinux labels, unavailable inside the build sandbox.
%global __brp_linkdupes /bin/true

Name:           switcher
Version:        1.0.0
Release:        1%{?dist}
Summary:        Punto-Switcher-style automatic keyboard layout correction
License:        MIT
URL:            https://github.com/switcher-linux/switcher
BuildArch:      noarch
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  python3
Requires:       python3-evdev
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
  * system tray icon and settings window (PySide6): word lists, behaviour, log
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
