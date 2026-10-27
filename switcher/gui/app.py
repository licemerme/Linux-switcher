# gui/app.py - system tray (StatusNotifierItem) for Switcher Linux.
from __future__ import annotations

import os
import signal
import subprocess
import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from .. import config as cfg
from . import client
from .window import SettingsWindow

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _daemon_python():
    # The daemon needs evdev. The tray is often started with the system
    # interpreter, which has PySide6 but usually not evdev, so prefer the
    # project venv when it exists.
    venv = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
    if os.path.exists(venv):
        return venv
    return sys.executable


def _icon(color, letter):
    # Render the tray icon in-process: no image files to ship.
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(2, 2, 60, 60)
    p.setPen(QColor("white"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(34)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, letter)
    p.end()
    return QIcon(pm)


class Tray:
    def __init__(self, app):
        self.app = app
        self.window = None
        self.status = None
        self._warned = False

        self.icon_on = _icon("#2e7d32", "S")
        self.icon_off = _icon("#616161", "S")
        self.icon_pause = _icon("#f9a825", "II")

        self.tray = QSystemTrayIcon(self.icon_off)
        self.tray.setToolTip("Switcher Linux")

        self.menu = QMenu()
        self.act_state = QAction("Состояние: -")
        self.act_state.setEnabled(False)
        # One entry for both states: the label flips in refresh().
        self.act_start = QAction("Запустить демон")
        self.act_start.triggered.connect(self.toggle_daemon)
        self.act_pause = QAction("Пауза")
        self.act_pause.setCheckable(True)
        self.act_pause.triggered.connect(self.on_pause)
        # Keep a reference to EVERY action: a local QAction added to a QMenu
        # has no Python owner, gets garbage-collected, and the menu entry turns
        # into an empty/vanishing row.
        self.act_settings = QAction("Настройки...")
        self.act_settings.triggered.connect(self.open_settings)
        self.act_reload = QAction("Перечитать словари")
        self.act_reload.triggered.connect(self.reload_dicts)
        self.act_log = QAction("Открыть лог")
        self.act_log.triggered.connect(self.open_log)
        self.act_diag = QAction("Диагностика соединения...")
        self.act_diag.triggered.connect(self.show_diagnostics)
        self.act_quit = QAction("Закрыть")
        self.act_quit.triggered.connect(self.on_quit)

        self.menu.addAction(self.act_state)
        self.menu.addAction(self.act_start)
        self.menu.addSeparator()
        self.menu.addAction(self.act_pause)
        self.menu.addAction(self.act_settings)
        self.menu.addSeparator()
        self.menu.addAction(self.act_reload)
        self.menu.addAction(self.act_log)
        self.menu.addAction(self.act_diag)
        self.menu.addSeparator()
        self.menu.addAction(self.act_quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_activated)

        self.timer = QTimer()
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def show(self):
        self.tray.show()

    def refresh(self):
        try:
            st = client.status()
        except client.DaemonError as e:
            self.status = None
            self.tray.setIcon(self.icon_off)
            self.tray.setToolTip("Switcher: нет связи с демоном")
            self.act_state.setText("Демон не запущен")
            self.act_state.setToolTip(str(e))
            self.act_pause.setEnabled(False)
            self.act_start.setText("Запустить демон")
            self.act_start.setEnabled(True)
            if not self._warned:
                # Print once (not every 2 s) so the terminal shows WHY: the
                # message contains the socket path that was tried.
                self._warned = True
                print("switcher-tray: " + str(e), flush=True)
            return
        self._warned = False
        self.status = st
        self.act_pause.setEnabled(True)
        # Daemon is up: the same entry must now offer to stop it.
        self.act_start.setText("Остановить демон")
        self.act_start.setEnabled(True)
        paused = bool(st.get("paused"))
        self.act_pause.setChecked(paused)
        self.act_pause.setText("Продолжить" if paused else "Пауза")
        self.tray.setIcon(self.icon_pause if paused else self.icon_on)
        self.tray.setToolTip(
            "Switcher: " + ("пауза" if paused else "активно")
            + " | выучено " + str(st.get("words_learned"))
            + ", заблокировано " + str(st.get("words_blocked")))
        self.act_state.setText(
            "Раскладка: " + str(st.get("layout"))
            + " | выучено " + str(st.get("words_learned"))
            + " | заблокировано " + str(st.get("words_blocked")))

    def on_pause(self):
        if not self.status:
            return
        try:
            client.set_paused(bool(self.act_pause.isChecked()))
        except client.DaemonError as e:
            self.tray.showMessage("Switcher", str(e), QSystemTrayIcon.Warning)
        self.refresh()

    def reload_dicts(self):
        try:
            client.reload_dicts()
        except client.DaemonError as e:
            self.tray.showMessage("Switcher", str(e), QSystemTrayIcon.Warning)
        self.refresh()

    def open_settings(self):
        if self.window is None:
            self.window = SettingsWindow()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def open_log(self):
        try:
            subprocess.Popen(["xdg-open", cfg.paths.log])
        except OSError as e:
            self.tray.showMessage("Switcher", "Не удалось открыть лог: " + str(e),
                                  QSystemTrayIcon.Warning)

    def toggle_pause(self):
        if not self.status:
            return
        try:
            client.set_paused(not bool(self.status.get("paused")))
        except client.DaemonError as e:
            self.tray.showMessage("Switcher", str(e), QSystemTrayIcon.Warning)
        self.refresh()

    def toggle_daemon(self):
        """Single menu entry: start when the daemon is down, stop when it is up."""
        if self.status is None:
            self.start_daemon()
        else:
            self.stop_daemon()

    def stop_daemon(self):
        self.act_start.setEnabled(False)
        self.act_start.setText("Останавливаю демон...")
        try:
            client.shutdown()
        except client.DaemonError as e:
            # The daemon may have died between the last refresh and the click.
            # Report it, but still resync so the menu offers "Запустить".
            self.tray.showMessage("Switcher", "Не удалось остановить демон: " + str(e),
                                  QSystemTrayIcon.Warning)
        # The daemon answers, then closes the socket and ungrabs the keyboard;
        # give it a moment before refresh() puts the menu back to "start".
        QTimer.singleShot(900, self.refresh)

    def start_daemon(self):
        py = _daemon_python()
        try:
            probe = subprocess.run([py, "-c", "import evdev"],
                                   capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            QMessageBox.critical(None, "Switcher", "Не удалось проверить интерпретатор: " + str(e))
            return
        if probe.returncode != 0:
            QMessageBox.critical(
                None, "Switcher",
                "В интерпретаторе " + py + " нет evdev:\n"
                + probe.stderr.decode("utf-8", "replace")[-400:]
                + "\n\nУстанови: uv pip install --python .venv/bin/python evdev")
            return
        try:
            # start_new_session: the daemon must survive the tray (no SIGHUP),
            # and it owns the keyboard, so it must not be in our process group.
            subprocess.Popen([py, "-m", "switcher.main"], cwd=PROJECT_ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as e:
            QMessageBox.critical(None, "Switcher", "Не удалось запустить демон: " + str(e))
            return
        self.tray.setToolTip("Switcher: запускаю демон...")
        QTimer.singleShot(1800, self.refresh)

    def show_diagnostics(self):
        info = client.diagnose()
        lines = [
            "Сокет: " + str(info.get("socket")),
            "Файл существует: " + str(info.get("exists")),
            "XDG_RUNTIME_DIR: " + str(info.get("xdg")),
            "Каталог: " + str(info.get("runtime_dir")),
            "В каталоге: " + str(info.get("listing")),
            "Связь: " + ("OK" if info.get("ok") else str(info.get("error"))),
        ]
        QMessageBox.information(None, "Switcher - диагностика", chr(10).join(lines))

    def on_activated(self, reason):
        # Single left click = pause/resume. Settings live in the context menu:
        # a double click arrives as Trigger + DoubleClick and would toggle
        # twice, so it is deliberately not bound.
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_pause()

    def on_quit(self):
        self.app.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Switcher Linux")
    app.setQuitOnLastWindowClosed(False)

    # Ctrl+C must actually quit. Qt owns the event loop, so a bare
    # KeyboardInterrupt lands in a random slot (refresh) and the app keeps
    # running. Install handlers that call quit(), plus a cheap timer that
    # gives the Python interpreter a chance to run them: without the pulse,
    # a blocked exec() never returns to the bytecode loop.
    def _on_signal(signum, frame):
        app.quit()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    pulse = QTimer()
    pulse.setInterval(200)
    pulse.timeout.connect(lambda: None)
    pulse.start()
    app._pulse = pulse          # keep a reference so it is not garbage-collected
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("no system tray (StatusNotifierItem) available in this session")
        return 1
    print("switcher-tray: ipc socket " + cfg.paths.socket, flush=True)
    tray = Tray(app)
    tray.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
