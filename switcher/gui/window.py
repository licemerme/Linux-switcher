# gui/window.py - settings window: word lists, behaviour, diagnostics.
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QDialog,
    QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import procs
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


class AddExcludeDialog(QDialog):
    # Ask for a process name - typed by hand or picked from what is running -
    # and when the switcher must ignore it.
    def __init__(self, pattern="", mode="focus", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Игнорировать программу")
        self.setMinimumWidth(580)
        self._loading = False
        self._silent = False
        self._mode_touched = False
        # display text -> pattern, for the entries where they differ
        # ("Dota 2 (dota2) · Steam" -> "dota2"). Typed text falls through as is.
        self._labels = {}

        # Editable combo: typing a name by hand stays the primary path (the game
        # may be closed right now); the list is only a hint.
        self.pattern_box = QComboBox()
        self.pattern_box.setEditable(True)
        self.pattern_box.setInsertPolicy(QComboBox.NoInsert)
        self.pattern_box.lineEdit().setPlaceholderText(
            "имя процесса или класс окна, например dota2")
        self.pattern_box.setToolTip(
            "Можно вписать любое имя вручную: совпадение ищется подстрокой "
            "без учёта регистра по имени процесса, командной строке, пути к "
            "файлу и классу окна.")
        comp = self.pattern_box.completer()
        if comp is not None:
            comp.setCaseSensitivity(Qt.CaseInsensitive)
            comp.setFilterMode(Qt.MatchContains)
            comp.setCompletionMode(QCompleter.PopupCompletion)
        self.pattern_box.activated.connect(self._on_pick)
        self.pattern_box.editTextChanged.connect(lambda _t: self._update_hint())

        self.all_check = QCheckBox("показать все процессы пользователя")
        self.all_check.setToolTip(
            "Отключает фильтр «это приложение»: пригодится, когда обёртка "
            "(flatpak, bwrap, лаунчер) прячет настоящий процесс.")
        self.all_check.toggled.connect(self._reload)

        self.mode_box = QComboBox()
        self.mode_box.addItem("пока её окно активно (X11/XWayland, игры)", "focus")
        self.mode_box.addItem("пока процесс запущен (любое приложение)", "running")
        self.mode_box.currentIndexChanged.connect(self._mode_changed)

        self.mode_hint = QLabel("")
        self.mode_hint.setWordWrap(True)

        hint = QLabel(
            "Пока исключение в силе, демон отпускает клавиатуру: перехват, "
            "автокоррекция и хоткеи для этой программы не работают вообще.")
        hint.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Процесс", self.pattern_box)
        form.addRow("", self.all_check)
        form.addRow("Когда игнорировать", self.mode_box)
        form.addRow("", self.mode_hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("OK")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addLayout(form)
        lay.addWidget(buttons)

        self._reload()
        if pattern:
            self.pattern_box.setEditText(pattern)
        self._set_mode(mode)

    # -- list ---------------------------------------------------------------
    def _reload(self):
        self._loading = True
        try:
            keep = self.pattern_box.currentText().strip()
            self.pattern_box.clear()
            self._labels = {}
            try:
                items = procs.candidates(all_user=self.all_check.isChecked())
            except Exception:
                items = []      # no list is fine: the field is editable
            for ent in items:
                text = ent["label"]
                if ent["source"] == "steam":
                    text += "  ·  Steam"
                elif ent["source"] == "flatpak":
                    text += "  ·  flatpak"
                elif ent["x11"]:
                    text += "  ·  X11"
                self.pattern_box.addItem(text, ent)
                if text != ent["pattern"]:
                    self._labels[text] = ent["pattern"]
            self.pattern_box.setCurrentIndex(-1)
            self.pattern_box.setEditText(keep or "")
        finally:
            self._loading = False
        self._update_hint()

    def _on_pick(self, index):
        if self._loading or index < 0:
            return
        data = self.pattern_box.itemData(index)
        if not isinstance(data, dict):
            return
        # Known application: pick the mode that actually works for it. focus
        # needs an X11/XWayland window, running works for anything.
        if not self._mode_touched:
            self._set_mode("focus" if data.get("x11") else "running")
        self._update_hint()

    # -- mode ---------------------------------------------------------------
    def _set_mode(self, mode):
        idx = self.mode_box.findData(mode)
        if idx < 0:
            return
        self._silent = True
        try:
            self.mode_box.setCurrentIndex(idx)
        finally:
            self._silent = False
        self._update_hint()

    def _mode_changed(self, _index):
        if not self._silent:
            self._mode_touched = True
        self._update_hint()

    def _update_hint(self):
        if self.mode_box.currentData() == "focus":
            text = ("Отключусь, только пока в фокусе окно этой программы: "
                    "свернул игру — свитчер снова работает для остальных окон. "
                    "Работает для X11/XWayland-окон, а это все игры; нативному "
                    "Wayland-приложению композитор не отдаёт владельца окна — "
                    "для него выбирай «пока процесс запущен».")
        else:
            text = ("Отключусь сразу, как только процесс запустится, и включусь, "
                    "когда он закроется. Фокус не важен: подходит для любого "
                    "приложения, в том числе Wayland-нативного.")
        self.mode_hint.setText(text)

    def values(self):
        text = self.pattern_box.currentText().strip()
        return self._labels.get(text, text), self.mode_box.currentData()


class ExcludeTable(QWidget):
    # Processes the switcher must leave alone completely.
    MODES = (("focus", "окно активно"), ("running", "процесс запущен"))

    def __init__(self, owner=None, parent=None):
        super().__init__(parent)
        self.owner = owner
        self._rows = []
        self._active = None

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Когда игнорировать", "Программа", "Сейчас"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)

        add = QPushButton("Добавить...")
        add.clicked.connect(self._on_add)
        remove = QPushButton("Удалить выбранное")
        remove.clicked.connect(self._on_remove)

        head = QHBoxLayout()
        head.addWidget(add)
        head.addWidget(remove)
        head.addStretch(1)

        hint = QLabel(
            "Программа из этого списка для свитчера не существует: он отпускает "
            "клавиатуру, перехват и автокоррекция для неё выключены полностью "
            "(удобно для игр - никакой лишней задержки ввода). "
            "В «Добавить...» можно выбрать из запущенных приложений или вписать "
            "имя процесса вручную.")
        hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addLayout(head)
        lay.addWidget(self.table, 1)

    def set_data(self, status):
        foc = list(status.get("exclude_focus") or [])
        run = list(status.get("exclude_running") or [])
        self._active = status.get("excluded")
        active_mode = status.get("excluded_mode")
        rows = []
        for mode, label in self.MODES:
            for pat in (foc if mode == "focus" else run):
                now = "ДА" if (self._active and pat.lower() == str(self._active).lower()
                               and active_mode == mode) else ""
                rows.append((mode, label, pat, now))
        self._rows = rows
        self.table.setRowCount(len(rows))
        for i, (mode, label, pat, now) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(label))
            self.table.setItem(i, 1, QTableWidgetItem(pat))
            self.table.setItem(i, 2, QTableWidgetItem(now))

    def _selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rows):
            return None
        mode, _label, pat, _now = self._rows[row]
        return mode, pat

    def _refresh_owner(self):
        if self.owner is not None:
            self.owner.refresh()

    def _add(self, pattern, mode):
        if not pattern:
            QMessageBox.warning(self, "Switcher", "Имя процесса не может быть пустым")
            return
        try:
            client.exclude_add(pattern, mode)
        except client.DaemonError as e:
            QMessageBox.critical(self, "Switcher", "Демон: " + str(e))
            return
        self._refresh_owner()

    def _on_add(self):
        dlg = AddExcludeDialog(parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        pattern, mode = dlg.values()
        self._add(pattern, mode)

    def _on_remove(self):
        sel = self._selected()
        if not sel:
            QMessageBox.information(self, "Switcher", "Выберите строку")
            return
        mode, pat = sel
        try:
            client.exclude_del(pat, mode)
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

        # --- exclusions ---------------------------------------------------
        self.excl_tab = ExcludeTable(owner=self)

        # --- behaviour ----------------------------------------------------
        beh = QWidget()
        bl = QVBoxLayout(beh)
        form = QFormLayout()
        self.min_len = QSpinBox()
        self.min_len.setRange(1, 10)
        form.addRow("Минимальная длина слова", self.min_len)
        self.debug = QCheckBox("Логировать каждое слово (debug)")
        form.addRow("", self.debug)
        self.notify = QCheckBox(
            "Уведомлять, когда свитчер уступает программу из исключений")
        form.addRow("", self.notify)
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
        tabs.addTab(self.excl_tab, "Исключения")
        tabs.addTab(beh, "Поведение")
        tabs.addTab(diag, "Диагностика")
        self._tabs = tabs
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def select_tab(self, index):
        # Used by the tray ("Исключения...") to land on the right page.
        try:
            self._tabs.setCurrentIndex(int(index))
        except (AttributeError, TypeError, ValueError):
            pass

    def refresh(self):
        try:
            st = client.status()
        except client.DaemonError as e:
            self._status = None
            self.lbl_conn.setText("Демон не запущен: " + str(e))
            self.learned_tab.setEnabled(False)
            self.blocked_tab.setEnabled(False)
            self.excl_tab.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_apply.setEnabled(False)
            return
        self._status = st
        self.learned_tab.setEnabled(True)
        self.blocked_tab.setEnabled(True)
        self.excl_tab.setEnabled(True)
        self.btn_pause.setEnabled(True)
        self.btn_apply.setEnabled(True)
        self.learned_tab.set_data(st.get("learned") or {})
        self.blocked_tab.set_data(st.get("blocked") or {})
        self.excl_tab.set_data(st)
        c = st.get("config") or {}
        if not self.min_len.hasFocus():
            self.min_len.setValue(int(c.get("min_len", 3)))
        self.debug.setChecked(bool(c.get("debug", False)))
        self.notify.setChecked(bool(c.get("notify_exclude", True)))
        paused = bool(st.get("paused"))
        self.btn_pause.setText("Продолжить" if paused else "Пауза")
        state = "пауза" if paused else "активно"
        if st.get("excluded"):
            state = "игнорирую " + str(st.get("excluded"))
        self.lbl_state.setText("Состояние: " + state
                               + " | раскладка: " + str(st.get("layout")))
        self.lbl_conn.setText("Сокет: " + str(st.get("socket"))
                              + " | выучено " + str(st.get("words_learned"))
                              + ", заблокировано " + str(st.get("words_blocked"))
                              + " | исключений: "
                              + str(len(st.get("exclude_focus") or [])
                                    + len(st.get("exclude_running") or [])))

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
            client.config_set(min_len=self.min_len.value(),
                              debug=self.debug.isChecked(),
                              notify_exclude=self.notify.isChecked())
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
