# gui/window.py - settings window: word lists, behaviour, diagnostics.
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import client


class AddWordDialog(QDialog):
    # Ask for layout, the word as typed, and (for learned) an optional target.
    def __init__(self, kind="learned", parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setWindowTitle("Добавить слово" if kind == "learned" else "Заблокировать слово")
        self.setMinimumWidth(430)

        self.layout_box = QComboBox()
        self.layout_box.addItem("us - английская раскладка", "us")
        self.layout_box.addItem("ru - русская раскладка", "ru")

        self.word_edit = QLineEdit()
        self.word_edit.setPlaceholderText("слово в том виде, как оно напечатано")
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("цель (пусто = вычислить автоматически)")

        form = QFormLayout()
        form.addRow("Раскладка", self.layout_box)
        form.addRow("Слово", self.word_edit)
        if kind == "learned":
            form.addRow("Цель", self.target_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("OK")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def values(self):
        return (
            self.layout_box.currentData(),
            self.word_edit.text().strip(),
            self.target_edit.text().strip() or None,
        )


class WordTable(QWidget):
    # One list (learned or blocked) with a filter and add/remove buttons.
    def __init__(self, kind, owner=None, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.owner = owner
        self._rows = []

        self.search = QLineEdit()
        self.search.setPlaceholderText("Фильтр по слову...")
        self.search.textChanged.connect(self._apply_filter)

        cols = 3 if kind == "learned" else 2
        self.table = QTableWidget(0, cols)
        if kind == "learned":
            self.table.setHorizontalHeaderLabels(["Раскладка", "Напечатано", "Исправляется на"])
        else:
            self.table.setHorizontalHeaderLabels(["Раскладка", "Слово"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)

        add = QPushButton("Добавить...")
        add.clicked.connect(self._on_add)
        remove = QPushButton("Удалить выбранное")
        remove.clicked.connect(self._on_remove)

        head = QHBoxLayout()
        head.addWidget(self.search, 1)
        head.addWidget(add)
        head.addWidget(remove)

        lay = QVBoxLayout(self)
        lay.addLayout(head)
        lay.addWidget(self.table, 1)

    def set_data(self, data):
        # data: {layout: {word: target_or_true}}
        rows = []
        for layout in ("us", "ru"):
            for word, target in sorted((data.get(layout) or {}).items()):
                rows.append((layout, word, "" if target is True else str(target)))
        self._rows = rows
        self._apply_filter()

    def _apply_filter(self):
        needle = self.search.text().strip().lower()
        rows = [r for r in self._rows if not needle or needle in r[1].lower()]
        self.table.setRowCount(len(rows))
        for i, (layout, word, target) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(layout))
            self.table.setItem(i, 1, QTableWidgetItem(word))
            if self.kind == "learned":
                self.table.setItem(i, 2, QTableWidgetItem(target))

    def _selected(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).text(), self.table.item(row, 1).text()

    def _refresh_owner(self):
        if self.owner is not None:
            self.owner.refresh()

    def _on_add(self):
        dlg = AddWordDialog(self.kind, self)
        if dlg.exec() != QDialog.Accepted:
            return
        layout, word, target = dlg.values()
        if not word:
            QMessageBox.warning(self, "Switcher", "Слово не может быть пустым")
            return
        try:
            if self.kind == "learned":
                client.learned_add(layout, word, target)
            else:
                client.blocked_add(layout, word)
        except client.DaemonError as e:
            QMessageBox.critical(self, "Switcher", "Демон: " + str(e))
            return
        self._refresh_owner()

    def _on_remove(self):
        sel = self._selected()
        if not sel:
            QMessageBox.information(self, "Switcher", "Выберите строку")
            return
        layout, word = sel
        try:
            if self.kind == "learned":
                client.learned_del(layout, word)
            else:
                client.blocked_del(layout, word)
        except client.DaemonError as e:
            QMessageBox.critical(self, "Switcher", "Демон: " + str(e))
            return
        self._refresh_owner()


class SettingsWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Switcher Linux - настройки")
        self.resize(780, 540)
        self._status = None

        # --- dictionaries -------------------------------------------------
        self.learned_tab = WordTable("learned", owner=self)
        self.blocked_tab = WordTable("blocked", owner=self)
        dicts = QWidget()
        dl = QVBoxLayout(dicts)
        hint = QLabel("Правки применяются сразу, перезапуск не нужен. "
                      "Те же действия доступны хоткеями: Ctrl+Alt+T - учить, "
                      "Ctrl+Alt+B - банить, Ctrl+Alt+Z - откат.")
        hint.setWordWrap(True)
        dl.addWidget(hint)
        sub = QTabWidget()
        sub.addTab(self.learned_tab, "Выученные")
        sub.addTab(self.blocked_tab, "Заблокированные")
        dl.addWidget(sub, 1)

        # --- behaviour ----------------------------------------------------
        beh = QWidget()
        bl = QVBoxLayout(beh)
        form = QFormLayout()
        self.min_len = QSpinBox()
        self.min_len.setRange(1, 10)
        form.addRow("Минимальная длина слова", self.min_len)
        self.debug = QCheckBox("Логировать каждое слово (debug)")
        form.addRow("", self.debug)
        bl.addLayout(form)
        self.btn_pause = QPushButton("Пауза")
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_apply = QPushButton("Применить")
        self.btn_apply.clicked.connect(self._apply_config)
        row = QHBoxLayout()
        row.addWidget(self.btn_pause)
        row.addWidget(self.btn_apply)
        row.addStretch(1)
        bl.addLayout(row)
        self.lbl_state = QLabel("")
        bl.addWidget(self.lbl_state)
        bl.addStretch(1)

        # --- diagnostics --------------------------------------------------
        diag = QWidget()
        gl = QVBoxLayout(diag)
        self.lbl_conn = QLabel("")
        gl.addWidget(self.lbl_conn)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        gl.addWidget(self.log_view, 1)
        self.btn_log = QPushButton("Обновить лог")
        self.btn_log.clicked.connect(self._refresh_log)
        gl.addWidget(self.btn_log)

        tabs = QTabWidget()
        tabs.addTab(dicts, "Словари")
        tabs.addTab(beh, "Поведение")
        tabs.addTab(diag, "Диагностика")
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        try:
            st = client.status()
        except client.DaemonError as e:
            self._status = None
            self.lbl_conn.setText("Демон не запущен: " + str(e))
            self.learned_tab.setEnabled(False)
            self.blocked_tab.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_apply.setEnabled(False)
            return
        self._status = st
        self.learned_tab.setEnabled(True)
        self.blocked_tab.setEnabled(True)
        self.btn_pause.setEnabled(True)
        self.btn_apply.setEnabled(True)
        self.learned_tab.set_data(st.get("learned") or {})
        self.blocked_tab.set_data(st.get("blocked") or {})
        c = st.get("config") or {}
        if not self.min_len.hasFocus():
            self.min_len.setValue(int(c.get("min_len", 3)))
        self.debug.setChecked(bool(c.get("debug", False)))
        paused = bool(st.get("paused"))
        self.btn_pause.setText("Продолжить" if paused else "Пауза")
        self.lbl_state.setText("Состояние: " + ("пауза" if paused else "активно")
                               + " | раскладка: " + str(st.get("layout")))
        self.lbl_conn.setText("Сокет: " + str(st.get("socket"))
                              + " | выучено " + str(st.get("words_learned"))
                              + ", заблокировано " + str(st.get("words_blocked")))

    def _toggle_pause(self):
        if not self._status:
            return
        try:
            client.set_paused(not bool(self._status.get("paused")))
        except client.DaemonError as e:
            QMessageBox.critical(self, "Switcher", str(e))
        self.refresh()

    def _apply_config(self):
        try:
            client.config_set(min_len=self.min_len.value(), debug=self.debug.isChecked())
        except client.DaemonError as e:
            QMessageBox.critical(self, "Switcher", str(e))
        self.refresh()

    def _refresh_log(self):
        try:
            data = client.log_tail(200)
        except client.DaemonError as e:
            self.log_view.setPlainText(str(e))
            return
        self.log_view.setPlainText(chr(10).join(data.get("lines") or []))
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())
