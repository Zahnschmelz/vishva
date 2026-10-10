import re
import os
import sys
import html
import json
import queue
import shutil
import datetime
import tempfile
import threading
import subprocess
#from .paths import p, cfg_path
from .paths import p
from PySide6.QtCore import QTimer, Signal, Slot, Qt
from .scheduler import TaskScheduler, process_due_tasks

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
        QTextBrowser, QPlainTextEdit, QPushButton, QTabWidget, QLineEdit,
        QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox, QDoubleSpinBox,
        QCheckBox, QComboBox, QMessageBox, QScrollArea, QAbstractItemView, QInputDialog,
        QLabel)
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtGui import QTextCursor, QIcon, QColor
except ImportError:
    print("PySide6 fehlt. Installiere: pip install PySide6")
    sys.exit(1)
from .agent import AgentCore
try:
    # from .agent import _play_notification_sound
    from .cli import _play_notification_sound
except Exception:
    _play_notification_sound = None

class ChatInput(QPlainTextEdit):
    submitted = Signal(str)
    def __init__(self):
        super().__init__()
        self.setPlaceholderText("Nachricht oder /help … (Shift+Enter für neue Zeile, //text für Slash-Text)")
        self.setMinimumHeight(60)
        self.setMaximumHeight(140)
        self.tts_queue = queue.Queue()
        self.tts_worker = None

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)
            else:
                text = self.toPlainText().strip()
                if text:
                    self.submitted.emit(text)
                    self.clear()
        else:
            super().keyPressEvent(event)

class FunctionWorker(QThread):
    done = Signal(object)
    failed = Signal(str)
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
    def run(self):
        try:
            result = self.fn()
            self.done.emit(result)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")

class MainWindow(QMainWindow):
    tool_call = Signal(str)
    tool_result = Signal(object)
    system_msg = Signal(str)
    assistant_msg = Signal(str)
    _confirm_signal = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.worker = None
        self.rag_worker = None
        self._orig_execute_tool = None
        self._suppress_compress_msg = False
        self._rag_loaded = False
        self.tts_queue = queue.Queue()
        self.tts_worker = None
        self._setup_ui()
        self._connect_signals()
        try:
            try:
                self.agent = AgentCore(
                    notifier=self.tool_call.emit,
                    enable_tts=True)
            except Exception as tts_error:
                self.append(
                    "System",
                    f"⚠️ TTS-Start fehlgeschlagen, starte ohne TTS: {tts_error}")
                self.agent = AgentCore(
                    notifier=self.tool_call.emit,
                    enable_tts=False)
        except Exception as e:
            self.append("Error", f"AgentCore konnte nicht gestartet werden: {e}")
            raise
        if getattr(self.agent, "tts_manager", None):
            try:
                self.agent.tts_manager.enabled = bool(
                    self.agent.config.get("tts_enabled", True))
            except Exception:
                pass
        self._install_agent_hooks()
        self.scheduler = TaskScheduler(config=self.agent.config)
        self.agent.scheduler = self.scheduler
        self.agent.interface = "gui"
        self.scheduler_timer = QTimer()
        self.scheduler_timer.timeout.connect(self._scheduler_tick)
        interval = self.scheduler.check_interval
        self.scheduler_timer.start(max(5, interval) * 1000)
        self._load_settings_ui()
        self._connect_settings_signals()
        self.statusBar().showMessage("Bereit")
        self._update_tokens()
        self.append("System", f"Session: `{self.agent.get_session_id()}`")
        self._confirm_event = threading.Event()
        self._confirm_result = None
        self._confirm_signal.connect(self._show_confirm_dialog, Qt.QueuedConnection)
        # nach Agent-Erzeugung:
        self.agent.tool_manager.set_confirm_handler(self._confirm_tool_gui)

    def _scheduler_tick(self):
        try:
            if not getattr(self.agent, "scheduler", None):
                return

            if not self.agent.scheduler.enabled:
                return

            if self._worker_running():
                return

            def local_send(response: str, task: dict):
                task_id = task.get("id", "?")

                try:
                    self.system_msg.emit(f"⏰ Scheduler-Task {task_id}")
                    self.assistant_msg.emit(response)
                except Exception:
                    pass

            def fn():
                return process_due_tasks(
                    self.agent,
                    self.agent.scheduler,
                    "gui",
                    local_send)

            def done(results):
                if results:
                    self._update_tokens()
                    self.statusBar().showMessage("Scheduler-Tasks ausgeführt")

            self.start_task(
                fn,
                status="Scheduler…",
                done_handler=done)

        except Exception as e:
            self.append("System", f"⚠️ Scheduler-Fehler: {e}")

    def _load_icon(self, *names):
        for name in names:
            path = name if os.path.isabs(name) else p(name)
            try:
                if os.path.exists(path):
                    icon = QIcon(path)
                    if not icon.isNull():
                        return icon
            except Exception:
                pass
        return QIcon()

    def _setup_ui(self):
        self.setWindowTitle("Vishva GUI")
        self.resize(1200, 820)

        app_icon = self._load_icon(
            "assets/vishva_gui.svg",
            "assets/vishva_gui.png",
            "assets/vishva.png",
            "assets/vishva.svg")
        if not app_icon.isNull():
            self.setWindowIcon(app_icon)
            if QApplication.instance():
                QApplication.instance().setWindowIcon(app_icon)
        self.tabs = QTabWidget()
        chat_tab = self._create_chat_tab()
        rag_tab = self._create_rag_tab()
        settings_tab = self._create_settings_tab()
        self.chat_tab = chat_tab
        self.rag_tab = rag_tab
        self.settings_tab = settings_tab
        chat_icon = self._load_icon("assets/vishva.svg", "assets/vishva.png")
        if not chat_icon.isNull():
            self.tabs.addTab(chat_tab, chat_icon, "Chat")
        else:
            self.tabs.addTab(chat_tab, "Chat")
        self.tabs.addTab(rag_tab, "RAG")
        self.tabs.addTab(settings_tab, "Settings")
        self.setCentralWidget(self.tabs)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #111827;
                color: #e5e7eb;}

            QPushButton {
                background-color: #1f2937;
                color: #e5e7eb;
                border: 1px solid #374151;
                border-radius: 4px;
                padding: 5px 10px;}

            QPushButton:hover {
                background-color: #374151;}

            QPushButton:disabled {
                background-color: #111827;
                color: #6b7280;}

            QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background-color: #1f2937;
                color: #e5e7eb;
                border: 1px solid #374151;
                border-radius: 4px;
                padding: 3px;}

            QTableWidget {
                background-color: #111827;
                color: #e5e7eb;
                gridline-color: #374151;
                border: 1px solid #374151;}

            QHeaderView::section {
                background-color: #1f2937;
                color: #e5e7eb;
                padding: 5px;
                border: none;}

            QTabWidget::pane {
                border: 1px solid #374151;}

            QTabBar::tab {
                background: #1f2937;
                color: #e5e7eb;
                padding: 7px 14px;
                border: none;}

            QTabBar::tab:selected {
                background: #374151;}

            QScrollBar:vertical {
                background: #111827;
                width: 12px;}

            QScrollBar::handle:vertical {
                background: #374151;
                border-radius: 5px;
                margin: 2px;}

            QScrollBar:horizontal {
                background: #111827;
                height: 12px;}

            QScrollBar::handle:horizontal {
                background: #374151;
                border-radius: 5px;
                margin: 2px;}""")

    def _create_chat_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(True)
        self.output.setReadOnly(True)
        self.output.setStyleSheet("""
            QTextBrowser {
                background-color: #111827;
                color: #e5e7eb;
                border: 1px solid #374151;}""")
        try:
            self.output.document().setDefaultStyleSheet("""
                body {
                    color: #e5e7eb;
                    background-color: #111827;}
                pre {
                    color: #e5e7eb;
                    background-color: #1f2937;
                    padding: 6px;
                    border-radius: 4px;}
                code {
                    color: #e5e7eb;
                    background-color: #374151;}
                a {
                    color: #60a5fa;}""")
        except Exception:
            pass
        layout.addWidget(self.output, stretch=1)
        btn_row = QHBoxLayout()
        self.btn_new = QPushButton("New Session")
        self.btn_zip = QPushButton("Compress")
        self.btn_tools = QPushButton("Tools")
        self.btn_history = QPushButton("History")
        self.btn_clear = QPushButton("Clear")
        self.btn_new.clicked.connect(lambda: self.command("/new"))
        self.btn_zip.clicked.connect(lambda: self.command("/zip"))
        self.btn_tools.clicked.connect(lambda: self.command("/tools"))
        self.btn_history.clicked.connect(lambda: self.command("/history"))
        self.btn_clear.clicked.connect(lambda: self.command("/clear"))
        for b in (self.btn_new, self.btn_zip, self.btn_tools, self.btn_history, self.btn_clear,):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)
        input_row = QHBoxLayout()
        self.input = ChatInput()
        self.input.submitted.connect(self._input_submitted)
        self.btn_send = QPushButton("Send")
        self.btn_send.clicked.connect(self._send_clicked)
        input_row.addWidget(self.input, stretch=1)
        input_row.addWidget(self.btn_send)
        layout.addLayout(input_row)
        return widget

    def _pause_spinner(self):
        st = getattr(self, "_active_status", None)
        if st:
            try:
                st.stop()
            except Exception:
                pass

    def _resume_spinner(self):
        st = getattr(self, "_active_status", None)
        if st:
            try:
                st.start()
            except Exception:
                pass

    def _confirm_tool_gui(self, tool_name: str, preview: str):
        # läuft im Worker-Thread → Dialog in GUI-Thread marshalen + warten
        self._pause_spinner()
        self._confirm_event.clear()
        self._confirm_result = None
        self._confirm_signal.emit(tool_name, preview)
        done = self._confirm_event.wait(timeout=600)
        self._resume_spinner()
        if not done or self._confirm_result is None:
            return {"allow": False, "reason": "confirmation timeout"}
        return self._confirm_result

    @Slot(str, str)
    def _show_confirm_dialog(self, tool_name: str, preview: str):
        # läuft im GUI-Thread
        msg = QMessageBox(self)
        msg.setWindowTitle(f"Tool-Bestätigung: {tool_name}")
        msg.setText(f"Tool '{tool_name}' ausführen?")
        msg.setInformativeText(preview[:300])
        allow_btn  = msg.addButton("Allow (y)", QMessageBox.AcceptRole)
        always_btn = msg.addButton("Always (a)", QMessageBox.AcceptRole)
        reason_btn = msg.addButton("Deny + Grund (r)", QMessageBox.RejectRole)
        deny_btn   = msg.addButton("Deny (n)", QMessageBox.RejectRole)
        msg.setDefaultButton(deny_btn)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is allow_btn:
            self._confirm_result = {"allow": True}
        elif clicked is always_btn:
            self._confirm_result = {"allow": True, "always": True}
        elif clicked is reason_btn:
            reason, ok = QInputDialog.getText(
                self, "Ablehnung begründen", f"Warum '{tool_name}' ablehnen?")
            self._confirm_result = {
                "allow": False,
                "reason": reason.strip() if ok and reason.strip() else "user denied"}
        else:
            self._confirm_result = {"allow": False, "reason": "user denied"}
        self._confirm_event.set()

    def _create_rag_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        self.RAG_COL_SCORE = 0
        self.RAG_COL_TEXT = 1
        self.RAG_COL_CATEGORY = 2
        self.RAG_COL_PRIORITY = 3
        self.RAG_COL_MAINTAINED = 4
        self.RAG_COL_SOURCE = 5
        self.RAG_COL_ACCESS = 6
        self.RAG_COL_AGE = 7
        self.RAG_COL_CREATED = 8
        self.RAG_COL_LAST_ACCESS = 9
        self.RAG_COL_ID = 10
        search_row = QHBoxLayout()
        self.rag_search_input = QLineEdit()
        self.rag_search_input.setPlaceholderText("RAG-Suchanfrage …")
        self.rag_search_input.returnPressed.connect(self._rag_search_async)
        search_row.addWidget(self.rag_search_input, stretch=1)
        search_row.addWidget(QLabel("Min Score:"))
        self.rag_min_score = QDoubleSpinBox()
        self.rag_min_score.setRange(0.0, 1.0)
        self.rag_min_score.setSingleStep(0.05)
        self.rag_min_score.setValue(0.0)
        self.rag_min_score.setDecimals(2)
        search_row.addWidget(self.rag_min_score)
        search_row.addWidget(QLabel("Top K:"))
        self.rag_top_k = QSpinBox()
        self.rag_top_k.setRange(0, 10000)
        self.rag_top_k.setValue(0)
        self.rag_top_k.setToolTip("0 = alle")
        search_row.addWidget(self.rag_top_k)
        self.rag_search_btn = QPushButton("Suchen")
        self.rag_search_btn.clicked.connect(self._rag_search_async)
        self.rag_show_all_btn = QPushButton("Alle anzeigen")
        self.rag_show_all_btn.clicked.connect(self._rag_show_all)
        self.rag_delete_btn = QPushButton("Löschen")
        self.rag_delete_btn.clicked.connect(self._rag_delete_selected)
        search_row.addWidget(self.rag_search_btn)
        search_row.addWidget(self.rag_show_all_btn)
        search_row.addWidget(self.rag_delete_btn)
        layout.addLayout(search_row)
        self.rag_table = QTableWidget()
        self.rag_table.setColumnCount(11)
        self.rag_table.setHorizontalHeaderLabels([
            "Score", "Text", "Kategorie", "Prio", "Maint.",
            "Quelle", "Zugriffe", "Alter", "Erstellt", "Letzter Zugriff", "ID"])
        self.rag_table.setColumnHidden(self.RAG_COL_ID, True)
        self.rag_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.rag_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rag_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.rag_table.setAlternatingRowColors(True)
        self.rag_table.verticalHeader().setVisible(False)
        header = self.rag_table.horizontalHeader()
        header.setSectionResizeMode(self.RAG_COL_TEXT, QHeaderView.Stretch)
        header.setSectionResizeMode(self.RAG_COL_SOURCE, QHeaderView.Interactive)
        self.rag_table.setColumnWidth(self.RAG_COL_SOURCE, 200)

        for col in (self.RAG_COL_SCORE, self.RAG_COL_CATEGORY, self.RAG_COL_PRIORITY,
                    self.RAG_COL_MAINTAINED, self.RAG_COL_ACCESS, self.RAG_COL_AGE,
                    self.RAG_COL_CREATED, self.RAG_COL_LAST_ACCESS,):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self._rag_current_results = []
        self._rag_sort_col = None
        self._rag_sort_asc = True
        self.rag_table.setSortingEnabled(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._rag_header_clicked)
        layout.addWidget(self.rag_table, stretch=1)
        add_label = QLabel("Neuen RAG-Eintrag hinzufügen")
        add_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(add_label)
        self.rag_text_edit = QPlainTextEdit()
        self.rag_text_edit.setPlaceholderText("Text für den neuen RAG-Eintrag …")
        self.rag_text_edit.setMinimumHeight(80)
        self.rag_text_edit.setMaximumHeight(140)
        layout.addWidget(self.rag_text_edit)
        add_row = QHBoxLayout()
        self.rag_category_edit = QLineEdit()
        self.rag_category_edit.setPlaceholderText("Kategorie (default: manual)")
        add_row.addWidget(self.rag_category_edit, stretch=1)
        self.rag_source_edit = QLineEdit()
        self.rag_source_edit.setPlaceholderText("Quelle (default: gui)")
        add_row.addWidget(self.rag_source_edit, stretch=1)
        add_row.addWidget(QLabel("Prio:"))
        self.rag_priority_spin = QSpinBox()
        self.rag_priority_spin.setRange(0, 3)
        self.rag_priority_spin.setValue(2)
        self.rag_priority_spin.setToolTip("Priorität: 0=irrelevant, 1=minor, 2=useful, 3=critical")
        add_row.addWidget(self.rag_priority_spin)
        self.rag_add_btn = QPushButton("Eintrag hinzufügen")
        self.rag_add_btn.clicked.connect(self._rag_add_entry)
        add_row.addWidget(self.rag_add_btn)
        layout.addLayout(add_row)
        self.rag_status_label = QLabel("RAG bereit.")
        self.rag_status_label.setStyleSheet("color: #9ca3af;")
        layout.addWidget(self.rag_status_label)
        return widget

    def _create_settings_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        outer_layout = QVBoxLayout(container)
        outer_layout.setContentsMargins(10, 10, 10, 10)
        general_label = QLabel("Agent / Features")
        general_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        outer_layout.addWidget(general_label)
        form_toggles = QFormLayout()
        self.set_tts = QCheckBox("TTS / Sprachausgabe aktivieren")
        self.set_sound = QCheckBox("Benachrichtigungssound")
        self.set_offloading = QCheckBox("Tool-Offloading")
        self.set_thinking = QCheckBox("Thinking anzeigen")
        self.set_frame = QCheckBox("CLI-Rahmen (betrifft Terminal-Ausgabe)")
        self.set_rag = QCheckBox("RAG aktivieren")
        self.set_rag_history = QCheckBox("RAG: Meta-Enrichment (Injection)")
        self.set_rag_auto_extract = QCheckBox("RAG: Meta-Extraction nach Antwort")
        form_toggles.addRow(self.set_tts)
        form_toggles.addRow(self.set_sound)
        form_toggles.addRow(self.set_offloading)
        form_toggles.addRow(self.set_thinking)
        form_toggles.addRow(self.set_frame)
        form_toggles.addRow(self.set_rag)
        form_toggles.addRow(self.set_rag_history)
        form_toggles.addRow(self.set_rag_auto_extract)
        outer_layout.addLayout(form_toggles)
        persona_label = QLabel("Persönlichkeit")
        persona_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        outer_layout.addWidget(persona_label)
        persona_row = QHBoxLayout()
        self.set_personality_combo = QComboBox()
        self.set_personality_btn = QPushButton("Persönlichkeit anwenden")
        persona_row.addWidget(self.set_personality_combo, stretch=1)
        persona_row.addWidget(self.set_personality_btn)
        outer_layout.addLayout(persona_row)
        limits_label = QLabel("Limits / Kontext / GUI")
        limits_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        outer_layout.addWidget(limits_label)
        form_limits = QFormLayout()
        self.set_context_size = QSpinBox()
        self.set_context_size.setRange(1024, 1000000)
        self.set_context_size.setSingleStep(1024)
        self.set_compression_threshold = QSpinBox()
        self.set_compression_threshold.setRange(0, 1000000)
        self.set_compression_threshold.setToolTip("0 = automatisch berechnen")
        self.set_max_tool_turns = QSpinBox()
        self.set_max_tool_turns.setRange(1, 100)
        self.set_max_tool_result = QSpinBox()
        self.set_max_tool_result.setRange(100, 100000)
        self.set_gui_history_limit = QSpinBox()
        self.set_gui_history_limit.setRange(0, 1000)
        self.set_gui_history_limit.setToolTip("0 = kein Limit")
        self.set_gui_tool_preview = QSpinBox()
        self.set_gui_tool_preview.setRange(0, 10000)
        self.set_stt_seconds = QSpinBox()
        self.set_stt_seconds.setRange(1, 600)
        self.set_stt_command = QLineEdit()
        self.set_stt_command.setPlaceholderText(
            "z.B. whisper {audio} --model tiny --language German --output_format txt --output_dir {dir}")
        form_limits.addRow("Context Size:", self.set_context_size)
        form_limits.addRow("Compression Threshold:", self.set_compression_threshold)
        form_limits.addRow("Max Tool Turns:", self.set_max_tool_turns)
        form_limits.addRow("Max Tool Result Length:", self.set_max_tool_result)
        form_limits.addRow("GUI History Limit:", self.set_gui_history_limit)
        form_limits.addRow("GUI Tool Result Preview:", self.set_gui_tool_preview)
        form_limits.addRow("STT Seconds:", self.set_stt_seconds)
        form_limits.addRow("STT Command:", self.set_stt_command)
        outer_layout.addLayout(form_limits)
        self.settings_save_btn = QPushButton("Settings speichern")
        outer_layout.addWidget(self.settings_save_btn)
        outer_layout.addStretch(1)
        scroll.setWidget(container)
        return scroll

    def _on_tab_changed(self, index: int):
        try:
            if self.tabs.widget(index) is self.rag_tab and not self._rag_loaded:
                self._rag_loaded = True
                self._rag_search_async()
        except Exception as e:
            self.rag_status_label.setText(f"⚠️ Tab-Fehler: {e}")

    def _connect_signals(self):
        self.tool_call.connect(self._append_tool_call)
        self.tool_result.connect(self._append_tool_result)
        self.system_msg.connect(lambda msg: self.append("System", msg))
        self.assistant_msg.connect(lambda msg: self.append("Assistant", msg))

    def _input_submitted(self, text: str):
        self._submit_text(text)

    def _send_clicked(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self._submit_text(text)

    def _submit_text(self, text: str):
        if not text:
            return
        if text.startswith("//"):
            self.run_agent(text[1:])
        elif text.startswith("/"):
            self.command(text)
        else:
            self.run_agent(text)

    def _install_agent_hooks(self):
        try:
            orig_execute_tool = self.agent.tool_manager.execute_tool
            self._orig_execute_tool = orig_execute_tool
            def execute_tool(name, args):
                try:
                    result = orig_execute_tool(name, args)
                    try:
                        preview = self._format_tool_result(name, result)
                    except Exception:
                        preview = str(result)[:1000]
                    self.tool_result.emit({
                        "name": name,
                        "preview": preview,})
                    return result
                except Exception as e:
                    self.tool_result.emit({
                        "name": name,
                        "error": f"{type(e).__name__}: {e}",})
                    raise

            self.agent.tool_manager.execute_tool = execute_tool
        except Exception as e:
            self.append("System", f"⚠️ Tool-Hook konnte nicht installiert werden: {e}")
        try:
            orig_compress = self.agent.compress_history
            def compress_history():
                result = orig_compress()
                if not self._suppress_compress_msg:
                    self.system_msg.emit(str(result))
                return result
            self.agent.compress_history = compress_history
        except Exception as e:
            self.append("System", f"⚠️ Compress-Hook konnte nicht installiert werden: {e}")
        try:
            if hasattr(self.agent, "_rescue_context"):
                orig_rescue = self.agent._rescue_context
                def rescue(history, attempt):
                    new_history, log = orig_rescue(history, attempt)
                    self.system_msg.emit(f"🆘 {log}")
                    return new_history, log
                self.agent._rescue_context = rescue
        except Exception as e:
            self.append("System", f"⚠️ Rescue-Hook konnte nicht installiert werden: {e}")
        try:
            orig_print_assistant = self.agent._print_assistant
            def print_assistant(text, title="Assistant"):
                try:
                    self.assistant_msg.emit(str(text))
                except Exception:
                    pass
                return orig_print_assistant(text, title)
            self.agent._print_assistant = print_assistant
        except Exception as e:
            self.append("System", f"⚠️ Assistant-Hook konnte nicht installiert werden: {e}")

    def append(self, role: str, text: str):
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(f"<b>{html.escape(str(role))}</b>:&nbsp;")
        try:
            cursor.insertMarkdown(str(text))
        except AttributeError:
            cursor.insertText(str(text))
        cursor.insertHtml("<br><br>")
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def append_pre(self, role: str, text: str, role_color: str = "#93c5fd", text_color: str = "#e5e7eb", bg_color: str = "#1f2937"):
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(
            f"<b style=\"color:{role_color};\">"
            f"{html.escape(str(role))}"
            f"</b>:<br>")
        safe = html.escape(str(text))
        cursor.insertHtml(
            f"<pre style=\"white-space:pre-wrap; "
            f"background-color:{bg_color}; "
            f"color:{text_color}; "
            f"padding:6px; "
            f"border-radius:4px;\">"
            f"{safe}"
            f"</pre><br>")
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def _append_tool_call(self, message: str):
        self.append_pre(
            "🔧 Tool",
            message,
            role_color="#60a5fa",
            text_color="#e5e7eb",
            bg_color="#1f2937")

    def _append_tool_result(self, evt):
        try:
            if not isinstance(evt, dict):
                self.append_pre(
                    "Tool",
                    str(evt),
                    role_color="#60a5fa",
                    text_color="#e5e7eb",
                    bg_color="#1f2937")
                return
            name = evt.get("name", "tool")
            if evt.get("error"):
                self.append_pre(
                    f"❌ {name} Fehler",
                    evt["error"],
                    role_color="#f87171",
                    text_color="#fecaca",
                    bg_color="#7f1d1d")
                return
            preview = evt.get("preview", "")
            self.append_pre(
                f"🔧 {name} Ergebnis",
                preview,
                role_color="#34d399",
                text_color="#e5e7eb",
                bg_color="#1f2937")
        except Exception as e:
            self.append_pre(
                "Tool Error",
                str(e),
                role_color="#f87171",
                text_color="#fecaca",
                bg_color="#7f1d1d")

    def _format_tool_result(self, name: str, result) -> str:
        try:
            limit = int(self.agent.config.get("gui_tool_result_preview", 800) or 800)
        except Exception:
            limit = 800
        try:
            if isinstance(result, str):
                raw = result
            else:
                raw = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except Exception:
            raw = str(result)
        if len(raw) > limit:
            return raw[:limit] + f"\n… [gekürzt, {len(raw)} Zeichen gesamt]"
        return raw

    def _load_history_to_view(self):
        try:
            history = self.agent.session_manager.load_session(self.agent.get_session_id())
            if not history:
                self.append("System", "📭 Keine History vorhanden.")
                return
            try:
                limit = int(self.agent.config.get("gui_history_limit", 50) or 0)
            except (TypeError, ValueError):
                limit = 50
            total_count = len(history)
            truncated = False
            if limit > 0 and len(history) > limit:
                history = history[-limit:]
                truncated = True
            count = 0
            for msg in history:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "system":
                    continue
                if isinstance(content, list):
                    content = " ".join(
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text")
                elif not isinstance(content, str):
                    content = str(content)
                if not content or not str(content).strip():
                    continue
                if role == "user":
                    self.append("User", content)
                    count += 1
                elif role == "assistant":
                    display = self.agent._strip_thinking(content)
                    self.append("Assistant", display)
                    count += 1
                elif role == "tool":
                    tool_name = msg.get("name", "tool")
                    preview = content[:150] + "…" if len(content) > 150 else content
                    self.append_pre(f"🔧 {tool_name}", preview)
                    count += 1
            if truncated:
                self.append(
                    "System",
                    f"📂 Letzte {count} von {total_count} Nachrichten geladen "
                    f"(Limit: {limit}).")
            else:
                self.append("System", f"📂 {count} Nachrichten aus der History geladen.")
            self._update_tokens()
        except Exception as e:
            self.append("System", f"⚠️ History konnte nicht geladen werden: {e}")

    def _update_tokens(self):
        try:
            tokens = self.agent.get_total_tokens()
            tps = ""
            if hasattr(self.agent, "last_tps") and self.agent.last_tps:
                tps = f" | {self.agent.last_tps:.1f} t/s"
            self.statusBar().showMessage(f"Bereit | 📊 Tokens: {tokens}{tps}")
        except Exception:
            self.statusBar().showMessage("Bereit")

    def _worker_running(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _rag_worker_running(self) -> bool:
        return self.rag_worker is not None and self.rag_worker.isRunning()

    def start_task(self, fn, status: str = "Working…", done_handler=None) -> bool:
        if self._worker_running():
            self.append("System", "⚠️ Agent arbeitet bereits.")
            return False
        self.statusBar().showMessage(status)
        self.worker = FunctionWorker(fn)
        self.worker.done.connect(done_handler or self._on_task_done_silent)
        self.worker.failed.connect(self._on_task_failed)
        self.worker.start()
        return True

    def _start_rag_task(self, fn, done_handler, status: str = "RAG läuft…") -> bool:
        if self._rag_worker_running():
            self.rag_status_label.setText("⚠️ RAG-Aktion läuft bereits.")
            return False
        self.statusBar().showMessage(status)
        self.rag_worker = FunctionWorker(fn)
        self.rag_worker.done.connect(done_handler)
        self.rag_worker.failed.connect(
            lambda err: self.rag_status_label.setText(f"❌ {err}"))
        self.rag_worker.start()
        return True

    def run_agent(self, text: str):
        if self._worker_running():
            self.append("System", "⚠️ Agent arbeitet bereits.")
            return
        self.append("User", text)
        self.start_task(
            lambda: self.agent.chat(text),
            status="Thinking…",
            done_handler=self.on_agent_done)

    def on_agent_done(self, response):
        self._update_tokens()
        self.append("Assistant", str(response or "(Leere Antwort)"))
        self.statusBar().showMessage("Bereit")
        self._speak_response(response)
        if (
            not self._tts_enabled()
            and getattr(self.agent, "sound_enabled", False)
            and _play_notification_sound):
            try:
                _play_notification_sound()
            except Exception:
                pass

    def _on_task_done_silent(self, _result):
        self._update_tokens()
        self.statusBar().showMessage("Bereit")

    def _on_task_failed(self, error: str):
        self.statusBar().showMessage("Fehler")
        self.append("Error", error)
        self._update_tokens()

    def _init_tts_manager(self, enabled: bool = True, quiet: bool = False) -> bool:
        """Initialisiert den TTSManager zur Laufzeit, falls nötig."""
        if getattr(self.agent, "tts_manager", None) is not None:
            try:
                self.agent.tts_manager.enabled = bool(enabled)
                self.agent.config["tts_enabled"] = bool(enabled)
                self.agent._save_config()
            except Exception:
                pass
            return True

        try:
            from .tts_manager import TTSManager
            self.agent.tts_manager = TTSManager()
            self.agent.tts_manager.enabled = bool(enabled)
            self.agent.config["tts_enabled"] = bool(enabled)
            if hasattr(self.agent.tool_manager, "tts_manager"):
                self.agent.tool_manager.tts_manager = self.agent.tts_manager
            self.agent._save_config()
            if not quiet:
                self.append("System", "✅ TTS initialisiert.")
            return True
        except Exception as e:
            if not quiet:
                self.append("Error", f"TTS konnte nicht initialisiert werden: {e}")
            return False

    def _tts_enabled(self) -> bool:
        mgr = getattr(self.agent, "tts_manager", None)
        return bool(mgr and getattr(mgr, "enabled", False))

    def _prepare_speech_text(self, text: str) -> str:
        text = str(text or "")
        text = re.sub(r"\[THINKING\].*?\[ANTWORT\]", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"\[THINKING\].*", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"```.*?```", " Code-Ausgabe ausgelassen. ", text, flags=re.DOTALL)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"https?://\S+", " Link ausgelassen. ", text)
        text = re.sub(r"[*_#>|`~]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        try:
            max_chars = int(self.agent.config.get("tts_max_chars", 2000) or 2000)
        except Exception:
            max_chars = 2000
        if len(text) > max_chars:
            text = text[:max_chars] + " Antwort wurde für die Sprachausgabe gekürzt."
        return text

    def _speak_response(self, text: str):
        if not self._tts_enabled():
            return
        prepared = self._prepare_speech_text(text)
        if not prepared:
            return
        if self.tts_queue.qsize() >= 2:
            return
        self.tts_queue.put(prepared)
        self._start_tts_worker()

    def _start_tts_worker(self):
        if self.tts_worker is not None and self.tts_worker.isRunning():
            return
        def fn():
            idle_ticks = 0
            while idle_ticks < 10:
                try:
                    text = self.tts_queue.get(timeout=0.2)
                except queue.Empty:
                    idle_ticks += 1
                    continue
                idle_ticks = 0
                mgr = getattr(self.agent, "tts_manager", None)
                if mgr and getattr(mgr, "enabled", False):
                    try:
                        mgr.speak(text)
                    except Exception as e:
                        try:
                            self.system_msg.emit(f"⚠️ TTS-Fehler: {e}")
                        except Exception:
                            pass
                self.tts_queue.task_done()
            return True
        self.tts_worker = FunctionWorker(fn)
        self.tts_worker.start()

    def command(self, text: str):
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/exit", "/bye"):
            self.close()
            return
        if cmd == "/help":
            self.append("Help", (
                "`/new` – Neue Session\n"
                "`/session <id>` – Session wechseln\n"
                "`/session` – Sessions auflisten\n"
                "`/sessions` – Sessions auflisten\n"
                "`/clear` – History löschen\n"
                "`/history` – History anzeigen\n"
                "`/zip`, `/shrink`, `/compress` – History komprimieren\n"
                "`/tokens` – Token-Anzahl\n"
                "`/threshold` – Compression-Threshold anzeigen\n"
                "`/tools`, `/listtools` – Tools anzeigen\n"
                "`/offloading [on|off]` – Tool-Offloading toggeln\n"
                "`/sound [on|off]` – Sound toggeln\n"
                "`/showThinking [on|off]` – Thinking-Anzeige toggeln\n"
                "`/voice [on|off]` – TTS steuern, falls verfügbar\n"
                "`/rag` – RAG Reindex\n"
                "`/config` – Config anzeigen\n"
                "`/info` – Session-/System-Info\n"
                "`/image <path> [prompt]` – Bild analysieren\n"
                "`/stt [seconds]` – Sprachaufnahme + Transkription\n"
                "`/personality <name>` – Persönlichkeit wechseln\n"
                "`/persona <name>` – Persönlichkeit wechseln\n"
                "`/frame [on|off]` – CLI-Rahmen toggeln\n"
                "`//text` – Text mit führender Slash senden\n"
                "`/help` – Diese Hilfe\n\n"
                "RAG und Settings sind jetzt in eigenen Tabs."))
            return

        if cmd == "/new":
            self.agent.start_new_session()
            self.output.clear()
            self.append("System", f"✨ Neue Session: `{self.agent.get_session_id()}`")
            self._update_tokens()
            return
        if cmd in ("/session", "/sessions"):
            if not arg:
                self._list_sessions()
                return
            try:
                self.agent.switch_to(arg)
                self.output.clear()
                self.append("System", f"🔄 Session gewechselt: `{arg}`")
                self._load_history_to_view()
            except ValueError as e:
                self.append("Error", str(e))
            return
        if cmd == "/clear":
            self.agent.clear_current()
            self.output.clear()
            self.append("System", "🧹 History gelöscht.")
            self._update_tokens()
            return
        if cmd == "/history":
            self.output.clear()
            self._load_history_to_view()
            return
        if cmd in ("/zip", "/shrink", "/compress"):
            self._compress_command()
            return
        if cmd == "/tokens":
            try:
                self.append("System", f"🔢 Tokens: {self.agent.get_total_tokens()}")
            except Exception as e:
                self.append("Error", str(e))
            return
        if cmd == "/threshold":
            try:
                ctx = self.agent.context_size
                thr = self.agent.compression_threshold
                source = (
                    p("config", "config.json")
                    if self.agent.config.get("compression_threshold") is not None
                    else "auto (ctx_size - reserve)")
                self.append(
                    "Threshold",
                    f"Context Size: `{ctx}`\n"
                    f"Compression Threshold: `{thr}`\n"
                    f"Quelle: `{source}`")
            except Exception as e:
                self.append("Error", str(e))
            return
        if cmd in ("/tools", "/listtools"):
            self._show_tools()
            return
        if cmd == "/offloading":
            self._toggle_attr(
                cmd_name="offloading",
                attr="offloading_enabled",
                key="offloading_enabled",
                arg=arg,
                label="💾 Offloading",
                on_info="Tool-Args/Results werden in Cache ausgelagert.",
                off_info="Tool-Args/Results bleiben stärker in der History.")
            return
        if cmd == "/sound":
            self._toggle_attr(
                cmd_name="sound",
                attr="sound_enabled",
                key="sound_enabled",
                arg=arg,
                label="🔔 Sound",
                on_info="Benachrichtigungssound wird abgespielt, wenn alarm.mp3/alarm.wav vorhanden ist.",
                off_info="Benachrichtigungssound ist stumm.")
            return
        if cmd in ("/showthinking", "/thinking"):
            self._toggle_attr(
                cmd_name="showThinking",
                attr="show_thinking",
                key="show_thinking",
                arg=arg,
                label="🧠 Thinking",
                on_info="Thinking-/Reasoning-Blöcke werden angezeigt, wenn das Modell sie liefert.",
                off_info="Thinking-/Reasoning-Blöcke werden gefiltert.")
            return
        if cmd == "/voice":
            self._cmd_voice(arg)
            return
        if cmd == "/info":
            self._show_info()
            return
        if cmd == "/config":
            self._show_config()
            return
        if cmd == "/rag":
            self._run_rag()
            return
        if cmd == "/image":
            self._run_image(arg)
            return
        if cmd == "/stt":
            self._run_stt(arg)
            return
        if cmd in ("/personality", "/persona"):
            self._cmd_personality(arg)
            return
        if cmd == "/frame":
            self._cmd_frame(arg)
            return
        self.append("System", f"❌ Unbekanntes Kommando: `{cmd}`")

    def _list_sessions(self):
        try:
            sessions = self.agent.session_manager.list_sessions()
            if not sessions:
                self.append("System", "Keine Sessions vorhanden.")
                return
            current = self.agent.get_session_id()
            entries = []
            for s in sessions:
                path = p("data", "sessions", f"{s}.json")
                if os.path.exists(path):
                    mtime = os.path.getmtime(path)
                    ts = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                else:
                    mtime = 0
                    ts = "unknown"
                entries.append((mtime, s, ts))
            entries.sort(reverse=True)
            lines = []
            for _, s, ts in entries:
                marker = " ← **aktiv**" if s == current else ""
                lines.append(f"- `{s}` ({ts}){marker}")
            self.append(
                p("data", "sessions"),
                "\n".join(lines) + "\n\nUsage: `/session <id>`")
        except Exception as e:
            self.append("Error", str(e))

    def _show_tools(self):
        try:
            tools = self.agent.get_active_tools_info()
            tools.sort(key=lambda t: (
                0 if t.get("active") else 1,
                str(t.get("name", "")).lower()))
            lines = []
            for t in tools:
                state = "🟢" if t.get("active") else "🔴"
                desc = (t.get("description") or "")[:120]
                lines.append(f"- {state} **{t.get('name', '?')}**: {desc}")
            self.append("Tools", "\n".join(lines) if lines else "Keine Tools")
        except Exception as e:
            self.append("Error", str(e))

    def _toggle_attr(self, cmd_name: str, attr: str, key: str, arg: str,
                     label: str, on_info: str, off_info: str):
        try:
            if not arg:
                val = bool(getattr(self.agent, attr, self.agent.config.get(key, False)))
                self.append("System", f"{label}: {'AN' if val else 'AUS'}")
                return
            if arg.lower() in ("on", "off"):
                val = arg.lower() == "on"
                setattr(self.agent, attr, val)
                self.agent.config[key] = val
                try:
                    self.agent._save_config()
                except Exception as e:
                    self.append("Error", f"Config speichern fehlgeschlagen: {e}")
                self.append("System", f"{label}: {'AN' if val else 'AUS'}")
                self.append("System", on_info if val else off_info)
                return
            self.append("System", f"❌ Usage: `/{cmd_name} [on|off]`")
        except Exception as e:
            self.append("Error", str(e))

    def _cmd_voice(self, arg: str):
        try:
            if not arg:
                if not self.agent.tts_manager:
                    self.append(
                        "Voice",
                        "TTS ist nicht initialisiert. Aktivieren mit `/voice on` "
                        "oder Settings → TTS.")
                    return
                status = "AN" if getattr(self.agent.tts_manager, "enabled", False) else "AUS"
                self.append("Voice", f"🔊 Voice Status: {status}")
                return
            if arg.lower() in ("on", "off"):
                enabled = arg.lower() == "on"
                if enabled:
                    if not self._init_tts_manager(enabled=True, quiet=False):
                        self.append("Error", "TTS konnte nicht aktiviert werden.")
                        return
                else:
                    if getattr(self.agent, "tts_manager", None):
                        self.agent.tts_manager.enabled = False
                    self.agent.config["tts_enabled"] = False
                    try:
                        self.agent._save_config()
                    except Exception:
                        pass
                status = "AN" if enabled else "AUS"
                self.append("Voice", f"🔊 Voice Status: {status}")
                try:
                    self.set_tts.blockSignals(True)
                    self.set_tts.setChecked(enabled)
                    self.set_tts.blockSignals(False)
                except Exception:
                    pass
                return
            if arg.lower() == "test":
                if not self._init_tts_manager(enabled=True, quiet=False):
                    self.append("Error", "TTS konnte für den Test nicht aktiviert werden.")
                    return
                self.append("Voice", "🔊 TTS-Test wird gesprochen …")
                self._speak_response("Hallo, das ist ein Test der Sprachausgabe.")
                return
            self.append("Error", "Usage: `/voice [on|off|test]`")
        except Exception as e:
            self.append("Error", str(e))

    def _show_info(self):
        try:
            sid = self.agent.get_session_id()
            tokens = self.agent.get_total_tokens()
            session_path = p("data", "sessions", f"{sid}.json")
            created_at = "unknown"
            last_access = "unknown"
            if os.path.exists(session_path):
                stat = os.stat(session_path)
                created_at = datetime.datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S")
                last_access = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            active = [
                t["function"]["name"]
                for t in self.agent.tool_manager.get_active_tools()]
            if self.agent.rag_manager:
                try:
                    rag_count = len(self.agent.rag_manager.entries)
                    rag_info = f"RAG-Einträge: {rag_count}"
                except Exception:
                    rag_info = "RAG: aktiviert"
            else:
                rag_info = "RAG: deaktiviert"
            info_text = (
                f"**Session ID:** `{sid}`\n"
                f"**Tokens:** {tokens}\n"
                f"**Erstellt:** {created_at}\n"
                f"**Letzter Zugriff:** {last_access}\n"
                f"**Aktive Tools:** {', '.join(active) if active else 'Keine'}\n"
                f"**RAG:** {rag_info}")
            self.append("Info", info_text)
        except Exception as e:
            self.append("Error", str(e))

    def _show_config(self):
        try:
            redacted = {}
            for k, v in self.agent.config.items():
                kl = str(k).lower()
                if any(x in kl for x in ("token", "secret", "key", "password", "api_key")):
                    redacted[k] = "***"
                else:
                    redacted[k] = v
            self.append(
                "Config",
                "```json\n"
                + json.dumps(redacted, ensure_ascii=False, indent=2, default=str)
                + "\n```")
        except Exception as e:
            self.append("Error", str(e))

    def _cmd_frame(self, arg: str):
        try:
            frame_on = bool(self.agent.config.get("cli_frame_enabled", True))
            if not arg:
                self.append("Frame", f"🖼️ CLI-Rahmen: {'AN' if frame_on else 'AUS'}")
                self.append("System", "Usage: `/frame [on|off]`")
                return
            if arg.lower() in ("on", "off"):
                frame_on = arg.lower() == "on"
                self.agent.config["cli_frame_enabled"] = frame_on
                try:
                    self.agent._save_config()
                except Exception as e:
                    self.append("Error", f"Config speichern fehlgeschlagen: {e}")
                self.append("Frame", f"🖼️ CLI-Rahmen: {'AN' if frame_on else 'AUS'}")
                self.append(
                    "System",
                    "Dies betrifft vor allem die CLI-Ausgabe in `agent.py`.")
                return
            self.append("Error", "Usage: `/frame [on|off]`")
        except Exception as e:
            self.append("Error", str(e))

    def _compress_command(self):
        if self._worker_running():
            self.append("System", "⚠️ Agent arbeitet bereits.")
            return
        self._suppress_compress_msg = True
        def fn():
            try:
                return self.agent.compress_history()
            finally:
                self._suppress_compress_msg = False
        def done(res):
            self.append("System", str(res))
            self._update_tokens()
            self.statusBar().showMessage("Bereit")
        if not self.start_task(fn, status="Compressing…", done_handler=done):
            self._suppress_compress_msg = False

    def _run_rag(self):
        execute = self._orig_execute_tool or self.agent.tool_manager.execute_tool
        def fn():
            return execute("rag_reindex", {})
        def done(res):
            try:
                out = json.dumps(res, ensure_ascii=False, indent=2, default=str)
            except Exception:
                out = str(res)
            self.append("RAG", "```json\n" + out + "\n```")
            self._update_tokens()
            self.statusBar().showMessage("Bereit")
        self.start_task(fn, status="RAG reindex…", done_handler=done)

    def _run_image(self, arg: str):
        if not arg:
            self.append("Error", "Usage: `/image <path> [optionaler Prompt]`")
            return
        if not bool(self.agent.config.get("vision_enabled", True)):
            self.append(
                "Error",
                "Vision ist deaktiviert. Setze `vision_enabled=true` in config.json.")
            return
        parts = arg.split(maxsplit=1)
        path = os.path.expanduser(parts[0])
        prompt = parts[1].strip() if len(parts) > 1 else "Beschreibe dieses Bild."
        if not os.path.exists(path):
            self.append("Error", f"Datei nicht gefunden: `{path}`")
            return
        if self._worker_running():
            self.append("System", "⚠️ Agent arbeitet bereits.")
            return
        b64_url = self.agent._process_image(path)
        if not b64_url:
            self.append("Error", "Bild konnte nicht verarbeitet werden.")
            return
        self.append("User", f"🖼️ `{path}` – {prompt}")
        self.start_task(
            lambda: self.agent.chat(prompt, b64_url),
            status="Vision…",
            done_handler=self.on_agent_done)

    def _run_stt(self, arg: str):
        try:
            seconds = int(arg.split()[0] if arg else self.agent.config.get("stt_seconds", 5))
        except Exception:
            seconds = 5
        if seconds < 1 or seconds > 600:
            self.append("Error", "STT-Dauer muss zwischen 1 und 600 Sekunden liegen.")
            return
        recorder = None
        if shutil.which("arecord"):
            recorder = "arecord"
        elif shutil.which("rec"):
            recorder = "rec"
        if not recorder:
            self.append(
                "Error",
                "Kein Audio-Recorder gefunden. Installiere `alsa-utils` für `arecord` "
                "oder `sox` für `rec`.")
            return
        if self._worker_running():
            self.append("System", "⚠️ Agent arbeitet bereits.")
            return
        self.append("User", f"🎤 Sprachnachricht ({seconds}s)")
        def task():
            tmp_dir = tempfile.mkdtemp(prefix="vishva_stt_")
            tmp_wav = os.path.join(tmp_dir, "recording.wav")
            try:
                if recorder == "arecord":
                    cmd = ["arecord", "-f", "cd", "-d", str(seconds), tmp_wav]
                else:
                    cmd = ["rec", "-c", "1", "-r", "16000", "-b", "16", tmp_wav, "trim", "0", str(seconds)]
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=seconds + 20)
                if proc.returncode != 0:
                    raise RuntimeError(f"Recorder returncode={proc.returncode}")
                if not os.path.exists(tmp_wav) or os.path.getsize(tmp_wav) == 0:
                    raise RuntimeError("Aufnahme fehlgeschlagen oder leer.")
                stt_cmd = str(self.agent.config.get("stt_command", "") or "")
                txt_dir = tempfile.mkdtemp(prefix="vishva_stt_txt_")
                transcript = ""
                try:
                    if not stt_cmd:
                        if shutil.which("whisper"):
                            stt_cmd = (
                                "whisper {audio} --model tiny --language German "
                                "--output_format txt --output_dir {dir}")
                        else:
                            raise RuntimeError(
                                "Kein STT konfiguriert. Setze `stt_command` in config.json "
                                "oder installiere `whisper`.")
                    formatted_cmd = stt_cmd.format(audio=tmp_wav, dir=txt_dir)
                    subprocess.run(
                        formatted_cmd,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=int(self.agent.config.get("stt_timeout", 300)))
                    txt_file = os.path.join(
                        txt_dir,
                        os.path.basename(tmp_wav).replace(".wav", ".txt"))
                    if os.path.exists(txt_file):
                        with open(txt_file, "r", encoding="utf-8") as f:
                            transcript = f.read().strip()
                finally:
                    shutil.rmtree(txt_dir, ignore_errors=True)
                if not transcript:
                    raise RuntimeError("Keine Transkription erhalten.")
                self.system_msg.emit(f"📝 Transkript: {transcript}")
                return self.agent.chat(transcript)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        self.start_task(task, status="STT…", done_handler=self.on_agent_done)

    def _cmd_personality(self, arg: str):
        personas_dir = p("personas")
        if not arg:
            if os.path.exists(personas_dir):
                files = sorted([
                    f.replace(".md", "")
                    for f in os.listdir(personas_dir)
                    if f.endswith(".md")])
                if files:
                    self.append(
                        "Persönlichkeiten",
                        "\n".join(f"- {p}" for p in files)
                        + "\n\nUsage: `/personality <name>`")
                else:
                    self.append("System", "Keine Persönlichkeiten in `personas/` gefunden.")
            else:
                self.append("Error", "Verzeichnis `personas/` nicht gefunden.")
            return
        name = arg.strip()
        if self._worker_running():
            self.append("System", "⚠️ Agent arbeitet bereits.")
            return
        self.append("System", f"🎭 Wechsel Persönlichkeit zu `{name}` …")
        def fn():
            return self.agent.set_personality(name)
        def done(res):
            if isinstance(res, dict):
                if res.get("success"):
                    self.append("System", f"✅ {res.get('message', 'OK')}")
                else:
                    self.append("Error", res.get("error", "Unbekannter Fehler"))
            else:
                self.append("System", str(res))
            self._update_tokens()
            self.statusBar().showMessage("Bereit")
        self.start_task(fn, status="Personality…", done_handler=done)

    def _rag_show_all(self):
        self.rag_search_input.clear()
        self.rag_min_score.setValue(0.0)
        self.rag_top_k.setValue(0)
        self._rag_search_async()

    def _rag_search_async(self):
        rag = getattr(self.agent, "rag_manager", None)
        if not rag:
            self._rag_populate_table([])
            self.rag_status_label.setText("❌ RAG ist deaktiviert oder nicht initialisiert.")
            return
        query = self.rag_search_input.text().strip()
        top_k = int(self.rag_top_k.value())
        min_score = float(self.rag_min_score.value())

        def fn():
            try:
                print(
                    f"[GUI-RAG] query={query!r} "
                    f"min_score={min_score} "
                    f"top_k={top_k} "
                    f"entries={len(getattr(rag, 'entries', []))} "
                    f"has_query_no_track={hasattr(rag, 'query_no_track')}")
            except Exception:
                pass
            if not query:
                entries = list(rag.entries)
                entries.sort(
                    key=lambda e: str(e.get("last_access") or e.get("created_at") or ""), reverse=True)
                if top_k > 0:
                    entries = entries[:top_k]
                return [(None, e) for e in entries]
            if hasattr(rag, "query_no_track"):
                res = rag.query_no_track(query, top_k=top_k, min_score=min_score)
                out = []
                for r in res.get("results", []):
                    score = float(r.get("score", 0.0))
                    # Voller Eintrag (enthält priority/maintained) statt Such-Duplikat
                    entry = None
                    if hasattr(rag, "_entry_by_id"):
                        entry = rag._entry_by_id.get(str(r.get("id", "")))
                    if not entry:
                        entry = {
                            "id": r.get("id", ""), "text": r.get("text", ""),
                            "source": r.get("source", ""), "category": r.get("category", ""),
                            "access_count": r.get("access_count", 0),
                            "age_cycles": r.get("age_cycles", 0),
                            "created_at": r.get("created_at", ""),
                            "last_access": r.get("last_access", "")}
                    out.append((score, entry))
                return out
            print("[GUI-RAG] ⚠️ rag.query_no_track fehlt. Bitte RAGManager aktualisieren.")
            return []

        def done(results):
            self._rag_sort_col = None
            try:
                self.rag_table.horizontalHeader().setSortIndicatorShown(False)
            except Exception:
                pass
            self._rag_populate_table(results)
            if query:
                self.rag_status_label.setText(
                    f"🔎 {len(results)} Ergebnisse für: {query}")
            else:
                self.rag_status_label.setText(f"📦 {len(results)} Einträge.")
            self.statusBar().showMessage("RAG fertig")
        self._start_rag_task(fn, done, status="RAG Suche…")

    def _rag_populate_table(self, results):
        self._rag_current_results = list(results)
        self._rag_fill_table()

    def _rag_fill_table(self):
        self.rag_table.setRowCount(0)
        for row, item in enumerate(self._rag_current_results):
            score, entry = item
            self.rag_table.insertRow(row)
            entry_id = str(entry.get("id", ""))
            text = str(entry.get("text", ""))
            preview = text[:300] + "…" if len(text) > 300 else text
            category = str(entry.get("category", ""))
            priority = entry.get("priority")
            prio_item = QTableWidgetItem(str(priority) if priority is not None else "-")
            if priority is not None:
                prio_item.setForeground(self._priority_color(int(priority)))
            prio_item.setTextAlignment(Qt.AlignCenter)
            self.rag_table.setItem(row, self.RAG_COL_PRIORITY, prio_item)

            maintained = bool(entry.get("maintained", False))
            maint_item = QTableWidgetItem("✓" if maintained else "")
            maint_item.setTextAlignment(Qt.AlignCenter)
            if maintained:
                maint_item.setForeground(QColor("#60a5fa"))
            self.rag_table.setItem(row, self.RAG_COL_MAINTAINED, maint_item)
            source = str(entry.get("source", ""))
            access_count = int(entry.get("access_count", 0) or 0)
            age_cycles = int(entry.get("age_cycles", 0) or 0)
            created = str(entry.get("created_at", ""))[:19]
            last_access = str(entry.get("last_access", ""))[:19]
            if score is None:
                score_text = "-"
                score_color = None
            else:
                score_text = f"{float(score) * 100:.1f}%"
                score_color = self._score_color(float(score))
            score_item = QTableWidgetItem(score_text)
            if score_color:
                score_item.setForeground(score_color)
            score_item.setTextAlignment(Qt.AlignCenter)
            self.rag_table.setItem(row, self.RAG_COL_SCORE, score_item)
            text_item = QTableWidgetItem(preview)
            text_item.setToolTip(text)
            self.rag_table.setItem(row, self.RAG_COL_TEXT, text_item)
            self.rag_table.setItem(
                row,
                self.RAG_COL_CATEGORY,
                QTableWidgetItem(category))
            source_item = QTableWidgetItem(source)
            source_item.setToolTip(source)
            self.rag_table.setItem(row, self.RAG_COL_SOURCE, source_item)
            access_item = QTableWidgetItem(str(access_count))
            access_item.setForeground(self._access_color(access_count))
            access_item.setTextAlignment(Qt.AlignCenter)
            self.rag_table.setItem(row, self.RAG_COL_ACCESS, access_item)
            age_item = QTableWidgetItem(str(age_cycles))
            age_item.setTextAlignment(Qt.AlignCenter)
            self.rag_table.setItem(row, self.RAG_COL_AGE, age_item)
            self.rag_table.setItem(
                row,
                self.RAG_COL_CREATED,
                QTableWidgetItem(created))

            self.rag_table.setItem(
                row,
                self.RAG_COL_LAST_ACCESS,
                QTableWidgetItem(last_access))

            id_item = QTableWidgetItem(entry_id)
            self.rag_table.setItem(row, self.RAG_COL_ID, id_item)

    def _rag_header_clicked(self, col):
        if col == getattr(self, "RAG_COL_ID", 8):
            return
        default_asc = {
            self.RAG_COL_TEXT: True,
            self.RAG_COL_CATEGORY: True,
            self.RAG_COL_SOURCE: True,
            self.RAG_COL_SCORE: False,
            self.RAG_COL_ACCESS: False,
            self.RAG_COL_AGE: False,
            self.RAG_COL_CREATED: False,
            self.RAG_COL_PRIORITY: False,
            self.RAG_COL_MAINTAINED: False,
            self.RAG_COL_LAST_ACCESS: False,}
        if self._rag_sort_col == col:
            self._rag_sort_asc = not self._rag_sort_asc
        else:
            self._rag_sort_col = col
            self._rag_sort_asc = default_asc.get(col, True)
        header = self.rag_table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(col, Qt.AscendingOrder if self._rag_sort_asc else Qt.DescendingOrder)
        self._rag_apply_sort()

    def _rag_apply_sort(self):
        col = getattr(self, "_rag_sort_col", None)
        if col is None:
            self._rag_fill_table()
            return
        asc = self._rag_sort_asc
        def sort_key(item):
            score, entry = item
            if col == self.RAG_COL_SCORE:
                return float(score) if score is not None else -1.0
            if col == self.RAG_COL_TEXT:
                return str(entry.get("text", "")).lower()
            if col == self.RAG_COL_CATEGORY:
                return str(entry.get("category", "")).lower()
            if col == self.RAG_COL_SOURCE:
                return str(entry.get("source", "")).lower()
            if col == self.RAG_COL_ACCESS:
                return int(entry.get("access_count", 0) or 0)
            if col == self.RAG_COL_AGE:
                return int(entry.get("age_cycles", 0) or 0)
            if col == self.RAG_COL_CREATED:
                return str(entry.get("created_at", "") or "")
            if col == self.RAG_COL_LAST_ACCESS:
                return str(entry.get("last_access", "") or "")
            if col == self.RAG_COL_PRIORITY:
                pv = entry.get("priority")
                return int(pv) if pv is not None else -1
            if col == self.RAG_COL_MAINTAINED:
                return 1 if entry.get("maintained") else 0
            return 0
        try:
            self._rag_current_results.sort(key=sort_key, reverse=not asc)
        except Exception:
            pass
        self._rag_fill_table()

    def _priority_color(self, prio: int):
        if prio >= 3:
            return QColor("#4ade80")
        if prio == 2:
            return QColor("#a3e635")
        if prio == 1:
            return QColor("#facc15")
        return QColor("#f87171")

    def _score_color(self, score: float):
        if score >= 0.7:
            return QColor("#4ade80")
        if score >= 0.4:
            return QColor("#facc15")
        return QColor("#f87171")

    def _access_color(self, count: int):
        if count == 0:
            return QColor("#f87171")
        if count <= 2:
            return QColor("#facc15")
        return QColor("#4ade80")

    def _rag_delete_selected(self):
        rag = getattr(self.agent, "rag_manager", None)
        if not rag:
            self.rag_status_label.setText("❌ RAG ist deaktiviert.")
            return
        if self._rag_worker_running():
            self.rag_status_label.setText("⚠️ RAG-Aktion läuft bereits.")
            return
        if self._worker_running():
            self.rag_status_label.setText("⚠️ Agent läuft gerade – RAG-Löschen blockiert.")
            return
        selected_rows = sorted({
            idx.row()
            for idx in self.rag_table.selectionModel().selectedRows()})
        if not selected_rows:
            self.rag_status_label.setText("⚠️ Keine Einträge ausgewählt.")
            return
        ids = []
        for row in selected_rows:
            item = self.rag_table.item(row, self.RAG_COL_ID)
            if item and item.text():
                ids.append(item.text())
        if not ids:
            self.rag_status_label.setText("⚠️ Keine löschbaren IDs gefunden.")
            return
        reply = QMessageBox.question(
            self,
            "RAG Einträge löschen",
            f"Wirklich {len(ids)} ausgewählte Einträge löschen?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        id_set = set(ids)
        try:
            meta = os.path.join(rag.db_dir, "meta.json")
            if os.path.exists(meta):
                shutil.copy2(meta, meta + ".bak")
        except Exception:
            pass
        before = len(rag.entries)
        rag.entries = [
            e for e in rag.entries
            if str(e.get("id", "")) not in id_set]
        deleted = before - len(rag.entries)
        try:
            rag._save()
        except Exception as e:
            self.rag_status_label.setText(f"❌ Speichern fehlgeschlagen: {e}")
            return
        self.rag_status_label.setText(f"✅ {deleted} Einträge gelöscht.")
        self._rag_search_async()

    def _rag_add_entry(self):
        rag = getattr(self.agent, "rag_manager", None)
        if not rag:
            self.rag_status_label.setText("❌ RAG ist deaktiviert.")
            return
        if self._rag_worker_running():
            self.rag_status_label.setText("⚠️ RAG-Aktion läuft bereits.")
            return
        if self._worker_running():
            self.rag_status_label.setText("⚠️ Agent läuft gerade – RAG-Hinzufügen blockiert.")
            return
        text = self.rag_text_edit.toPlainText().strip()
        if not text:
            self.rag_status_label.setText("⚠️ Bitte Text für den Eintrag eingeben.")
            return
        source = self.rag_source_edit.text().strip() or "gui"
        category = self.rag_category_edit.text().strip() or "manual"
        def fn():
            return rag.add_text(
                text,
                source=source,
                category=category,
                metadata={"priority": int(self.rag_priority_spin.value())},
                save=True)
        def done(res):
            if not isinstance(res, dict):
                self.rag_status_label.setText(str(res))
                return
            if res.get("success"):
                added = res.get("added", 0)
                self.rag_status_label.setText(f"✅ {added} Chunk(s) hinzugefügt.")
                self.rag_text_edit.clear()
                self.rag_search_input.clear()
                self.rag_min_score.setValue(0.0)
                self.rag_top_k.setValue(0)
                self._rag_search_async()
            else:
                self.rag_status_label.setText(f"❌ {res.get('error', 'Fehler')}")
        self._start_rag_task(fn, done, status="RAG Eintrag hinzufügen…")

    def _load_settings_ui(self):
        if not hasattr(self, "agent"):
            return
        checkboxes = [
            self.set_tts,
            self.set_sound,
            self.set_offloading,
            self.set_thinking,
            self.set_frame,
            self.set_rag,
            self.set_rag_history,
            self.set_rag_auto_extract,]
        for cb in checkboxes:
            cb.blockSignals(True)
        self.set_tts.setChecked(
            bool(self.agent.tts_manager and getattr(self.agent.tts_manager, "enabled", False)))
        self.set_sound.setChecked(bool(self.agent.sound_enabled))
        self.set_offloading.setChecked(bool(self.agent.offloading_enabled))
        self.set_thinking.setChecked(bool(self.agent.show_thinking))
        self.set_frame.setChecked(bool(self.agent.config.get("cli_frame_enabled", True)))
        self.set_rag.setChecked(
            bool(self.agent.config.get("rag_enabled", False)) and self.agent.rag_manager is not None)
        self.set_rag_history.setChecked(bool(self.agent.config.get("rag_meta_enrichment_enabled", True)))
        self.set_rag_auto_extract.setChecked(bool(self.agent.config.get("rag_meta_extraction_enabled", True)))
        for cb in checkboxes:
            cb.blockSignals(False)
        numeric_widgets = [
            self.set_context_size,
            self.set_compression_threshold,
            self.set_max_tool_turns,
            self.set_max_tool_result,
            self.set_gui_history_limit,
            self.set_gui_tool_preview,
            self.set_stt_seconds,]
        for w in numeric_widgets:
            w.blockSignals(True)
        try:
            self.set_context_size.setValue(int(self.agent.context_size))
            self.set_compression_threshold.setValue(int(self.agent.compression_threshold))
            self.set_max_tool_turns.setValue(int(self.agent.config.get("max_tool_turns", 15) or 15))
            self.set_max_tool_result.setValue(int(self.agent.config.get("max_tool_result_length", 3000) or 3000))
            self.set_gui_history_limit.setValue(int(self.agent.config.get("gui_history_limit", 50) or 50))
            self.set_gui_tool_preview.setValue(int(self.agent.config.get("gui_tool_result_preview", 800) or 800))
            self.set_stt_seconds.setValue(int(self.agent.config.get("stt_seconds", 5) or 5))
        except Exception:
            pass
        for w in numeric_widgets:
            w.blockSignals(False)
        self.set_stt_command.blockSignals(True)
        self.set_stt_command.setText(str(self.agent.config.get("stt_command", "") or ""))
        self.set_stt_command.blockSignals(False)
        self._populate_personality_combo()

    def _populate_personality_combo(self):
        self.set_personality_combo.blockSignals(True)
        self.set_personality_combo.clear()
        personas_dir = p("personas")
        try:
            if os.path.exists(personas_dir):
                files = sorted([
                    f.replace(".md", "")
                    for f in os.listdir(personas_dir)
                    if f.endswith(".md")
                ])
                self.set_personality_combo.addItems(files)
        except Exception:
            pass
        self.set_personality_combo.blockSignals(False)

    def _connect_settings_signals(self):
        self.set_tts.toggled.connect(self._settings_tts_toggled)
        self.set_sound.toggled.connect(
            lambda v: self._settings_toggle_simple(
                key="sound_enabled",
                value=v,
                attr="sound_enabled",
                label="🔔 Sound"))
        self.set_offloading.toggled.connect(
            lambda v: self._settings_toggle_simple(
                key="offloading_enabled",
                value=v,
                attr="offloading_enabled",
                label="💾 Offloading"))
        self.set_thinking.toggled.connect(
            lambda v: self._settings_toggle_simple(
                key="show_thinking",
                value=v,
                attr="show_thinking",
                label="🧠 Thinking"))
        self.set_frame.toggled.connect(
            lambda v: self._settings_toggle_config(
                key="cli_frame_enabled",
                value=v,
                label="🖼️ CLI-Rahmen"))
        self.set_rag.toggled.connect(self._settings_toggle_rag)
        self.set_rag_history.toggled.connect(
            lambda v: self._settings_toggle_config(key="rag_meta_enrichment_enabled", value=v, label="RAG Enrichment"))
        self.set_rag_auto_extract.toggled.connect(
            lambda v: self._settings_toggle_config(key="rag_meta_extraction_enabled", value=v, label="RAG Extraction"))
        self.settings_save_btn.clicked.connect(self._save_settings_numeric)
        self.set_personality_btn.clicked.connect(self._apply_personality_from_settings)

    def _apply_personality_from_settings(self):
        name = self.set_personality_combo.currentText().strip()
        if not name:
            QMessageBox.information(self, "Persönlichkeit", "Bitte zuerst eine Persönlichkeit auswählen.")
            return
        self._cmd_personality(name)

    def _settings_tts_toggled(self, checked: bool):
        if not hasattr(self, "agent"):
            return
        try:
            if checked:
                if not self._init_tts_manager(enabled=True, quiet=False):
                    raise RuntimeError("TTS Manager konnte nicht initialisiert werden.")
            else:
                if getattr(self.agent, "tts_manager", None):
                    self.agent.tts_manager.enabled = False
                self.agent.config["tts_enabled"] = False
                try:
                    self.agent._save_config()
                except Exception:
                    pass
            self.append("System", f"🔊 TTS: {'AN' if checked else 'AUS'}")
        except Exception as e:
            self.append("Error", f"TTS konnte nicht gesetzt werden: {e}")
            self.set_tts.blockSignals(True)
            self.set_tts.setChecked(False)
            self.set_tts.blockSignals(False)

    def _settings_toggle_simple(self, key: str, value: bool, attr: str = None, label: str = ""):
        if not hasattr(self, "agent"):
            return
        try:
            if attr:
                setattr(self.agent, attr, bool(value))
            self.agent.config[key] = bool(value)
            self.agent._save_config()
            self.append("System", f"{label or key}: {'AN' if value else 'AUS'}")
        except Exception as e:
            self.append("Error", f"Setting konnte nicht gespeichert werden: {e}")

    def _settings_toggle_config(self, key: str, value: bool, label: str = ""):
        if not hasattr(self, "agent"):
            return
        try:
            self.agent.config[key] = bool(value)
            self.agent._save_config()
            self.append("System", f"{label or key}: {'AN' if value else 'AUS'}")
        except Exception as e:
            self.append("Error", f"Setting konnte nicht gespeichert werden: {e}")

    def _settings_toggle_rag(self, checked: bool):
        if not hasattr(self, "agent"):
            return
        try:
            self.agent.config["rag_enabled"] = bool(checked)
            if checked:
                if not self.agent.rag_manager:
                    from .rag_manager import RAGManager
                    self.agent.rag_manager = RAGManager(self.agent.config)
                    self.agent.tool_manager.rag_manager = self.agent.rag_manager
                if self.agent.rag_manager:
                    self.agent.rag_manager.enabled = True
                self.append("System", "🧠 RAG: AN")
            else:
                if self.agent.rag_manager:
                    self.agent.rag_manager.enabled = False
                self.append("System", "🧠 RAG: AUS")
            self.agent._save_config()
            if self.tabs.currentWidget() is self.rag_tab:
                self._rag_loaded = True
                self._rag_search_async()
        except Exception as e:
            self.append("Error", f"RAG konnte nicht gesetzt werden: {e}")
            self.set_rag.blockSignals(True)
            self.set_rag.setChecked(False)
            self.set_rag.blockSignals(False)

    def _save_settings_numeric(self):
        if not hasattr(self, "agent"):
            return
        try:
            ctx = int(self.set_context_size.value())
            thr = int(self.set_compression_threshold.value())
            self.agent.context_size = ctx
            self.agent.config["context_size"] = ctx
            if thr == 0:
                self.agent.config["compression_threshold"] = None
                self.agent.compression_threshold = self.agent._calculate_compression_threshold(ctx)
            else:
                self.agent.config["compression_threshold"] = thr
                self.agent.compression_threshold = thr
            self.agent.config["max_tool_turns"] = int(self.set_max_tool_turns.value())
            self.agent.config["max_tool_result_length"] = int(self.set_max_tool_result.value())
            self.agent.config["gui_history_limit"] = int(self.set_gui_history_limit.value())
            self.agent.config["gui_tool_result_preview"] = int(self.set_gui_tool_preview.value())
            self.agent.config["stt_seconds"] = int(self.set_stt_seconds.value())
            self.agent.config["stt_command"] = self.set_stt_command.text().strip()
            self.agent._save_config()
            self._update_tokens()
            self.append("System", "✅ Settings gespeichert.")
            self.statusBar().showMessage("Settings gespeichert")
        except Exception as e:
            self.append("Error", f"Settings konnten nicht gespeichert werden: {e}")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.wait(3000)
        if self.rag_worker and self.rag_worker.isRunning():
            self.rag_worker.wait(2000)
        if getattr(self, "tts_worker", None) and self.tts_worker.isRunning():
            self.tts_worker.wait(2000)
        event.accept()

def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
