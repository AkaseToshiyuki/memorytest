"""Signal-driven native UI. Benchmark, stress and telemetry work stay off the GUI thread."""
from collections import deque
import json
import os
from pathlib import Path
import tempfile
from PySide6.QtCore import Qt, QProcess, QSettings, QStandardPaths, QUrl, QTranslator, QLibraryInfo, QTimer
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget, QGridLayout, QMessageBox)
from benchmark_registry import ROOT, TASKS as TESTS, SELECTION_GROUPS as GROUPS, SUBTESTS
from process_control import app_command
from desktop_results import ModulePage, supported
from desktop_theme import apply_theme, card, label, set_tone
from desktop_widgets import ReadableSpinBox, ElidingLabel
from i18n import tr, display, default_language
from result_model import ModuleResult, build_module, split_legacy_result
from app_version import RELEASE_TAG
from result_storage import (application_directory, writable_output, recent_archives,
                            local_file, prune_archives)
from runtime_permissions import is_administrator


class MainWindow(QMainWindow):
    def __init__(self, autodetect=True, output_dir=None, restore=True):
        super().__init__()
        icon_path = ROOT / "icons/memorytest.svg"
        if not icon_path.is_file():
            icon_path = ROOT / "packaging/memorytest.svg"
        self.setWindowIcon(QIcon(str(icon_path)))
        self.settings = QSettings()
        language = self.settings.value("language", default_language())
        theme = self.settings.value("theme", "dark")
        self.language = language if language in {"zh", "en"} else "en"
        self.theme = theme if theme in {"dark", "light"} else "dark"
        self.translator = None
        apply_theme(self.theme)
        self.resize(1120, 850)
        self.setMinimumSize(880, 720)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.buffer = b""
        self.control = None
        self.closing = False
        self.report = self.run_dir = None
        self.completed = set()
        self.active_modules = []
        self.benchmark_active = False
        self.stress_active = False
        self.administrator = is_administrator()
        self.monitor_started = False
        self.status_key = "ready"
        self.module_status = {name: "not_run" if supported(name) else "unsupported" for name in TESTS}
        self.status_highlight = True
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(600)
        self.status_timer.timeout.connect(self.pulse_status)
        self.bindings, self.group_buttons, self.log_events = [], [], deque(maxlen=500)
        shell = QWidget()
        shell.setObjectName("shell")
        outer = QVBoxLayout(shell)
        outer.setContentsMargins(24, 18, 24, 12)
        outer.setSpacing(12)
        brand = QHBoxLayout()
        brand.addWidget(label("MemoryTest", "brand"))
        brand.addStretch()
        self.language_combo, self.theme_combo = QComboBox(), QComboBox()
        self.language_combo.addItem("", "zh")
        self.language_combo.addItem("", "en")
        self.theme_combo.addItem("", "dark")
        self.theme_combo.addItem("", "light")
        self.language_combo.setCurrentIndex(self.language_combo.findData(self.language))
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self.theme))
        brand.addWidget(self.language_combo)
        brand.addWidget(self.theme_combo)
        outer.addLayout(brand)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs, 1)
        footer = QHBoxLayout()
        self.version = label(RELEASE_TAG, "version")
        footer.addWidget(self.version)
        footer.addStretch()
        self.license_dialog = None
        self.licenses = self.button("licenses", self.show_licenses)
        self.licenses.setProperty("role", "quiet")
        footer.addWidget(self.licenses)
        outer.addLayout(footer)
        self.setCentralWidget(shell)
        home, layout = self.scroll_page()
        self.tabs.addTab(home, "")
        heading = QHBoxLayout()
        heading.addWidget(self.text("overview", "title"))
        heading.addStretch()
        layout.addLayout(heading)
        self.hardware_info = {}
        self.hardware_card, hardware_body = card()
        hardware_body.setSpacing(4)
        hardware_body.addWidget(self.text("system", "section"))
        hardware_grid = QGridLayout()
        hardware_grid.setHorizontalSpacing(18)
        hardware_grid.setVerticalSpacing(5)
        self.hardware_labels = {}
        for index, key in enumerate(("cpu_model", "physical", "memory_total", "nominal_frequency", "memory_rate", "gpu")):
            column, row = index % 3, (index // 3) * 2
            hardware_grid.addWidget(self.text(key, "muted"), row, column)
            value = ElidingLabel()
            value.setMinimumWidth(0)
            self.hardware_labels[key] = value
            hardware_grid.addWidget(value, row + 1, column)
            hardware_grid.setColumnStretch(column, 1)
        hardware_body.addLayout(hardware_grid)
        layout.addWidget(self.hardware_card)
        self.hardware_process = QProcess(self)
        self.hardware_process.finished.connect(self.hardware_finished)
        self.hardware_process.errorOccurred.connect(lambda *_: self.refresh_hardware())
        columns = QHBoxLayout()
        module_card, module_layout = card()
        self.modules, self.labels = {}, {}
        for group, names in GROUPS.items():
            row = QHBoxLayout()
            row.addWidget(self.text(group, "section"))
            row.addStretch()
            button = self.button("run_group", lambda checked=False, ns=names: self.begin([n for n in ns if supported(n)]))
            button.setProperty("role", "quiet")
            self.group_buttons.append(button)
            row.addWidget(button)
            module_layout.addLayout(row)
            for name in names:
                row = QHBoxLayout()
                check = QCheckBox()
                check.setChecked(supported(name))
                check.setEnabled(supported(name))
                self.modules[name] = check
                self.labels[name] = label("", "status")
                row.addWidget(check)
                row.addStretch()
                row.addWidget(self.labels[name])
                module_layout.addLayout(row)
        columns.addWidget(module_card, 3)
        config_card, config = card()
        config.addWidget(self.text("config", "section"))
        config.addWidget(self.text("compute_threads", "muted"))
        self.threads = ReadableSpinBox()
        self.threads.setRange(0, min(262144, os.cpu_count() or 1))
        config.addWidget(self.threads)
        config.addWidget(self.text("output", "muted"))
        self.data_directory = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
        preferred = Path(output_dir) if output_dir is not None else application_directory() / "Outputs"
        self.output_fallback = False
        try:
            destination, self.output_fallback = writable_output(preferred, self.data_directory / "Outputs")
        except OSError:
            destination = preferred
        self.output = QLineEdit(str(destination))
        self.output.setReadOnly(True)
        self.output.setMinimumWidth(0)
        self.output.setCursorPosition(0)
        config.addWidget(self.output)
        self.storage_note = self.text("auto_output", "muted")
        self.storage_note.setWordWrap(True)
        config.addWidget(self.storage_note)
        self.saved_run = label("", "muted")
        self.saved_run.setWordWrap(True)
        self.saved_run.setTextFormat(Qt.PlainText)
        config.addWidget(self.saved_run)
        self.run_all = self.button("run_all", self.begin_all)
        self.run_all.setProperty("role", "primary")
        config.addWidget(self.run_all)
        controls = QHBoxLayout()
        self.start, self.cancel = self.button("run_selected", lambda: self.begin()), self.button("cancel", self.request_cancel)
        controls.addWidget(self.start)
        controls.addWidget(self.cancel)
        config.addLayout(controls)
        links = QHBoxLayout()
        self.open_report = self.button("open_report", lambda: self.open_path(self.report))
        self.open_folder = self.button("open_folder", lambda: self.open_path(Path(self.output.text()).expanduser()))
        for button in (self.open_report, self.open_folder):
            button.setProperty("role", "quiet")
            links.addWidget(button)
        config.addLayout(links)
        config.addStretch()
        columns.addWidget(config_card, 2)
        layout.addLayout(columns)
        progress_card, progress_layout = card()
        row = QHBoxLayout()
        row.addWidget(self.text("status", "section"))
        row.addStretch()
        self.status = label("", "status")
        self.status.setWordWrap(True)
        self.status.setMaximumWidth(230)
        footer.insertWidget(1, self.status)
        progress_layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFixedWidth(200)
        footer.insertWidget(2, self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMaximumHeight(90)
        progress_layout.addWidget(self.log)
        layout.addWidget(progress_card)
        layout.addStretch()
        self.pages, self.group_tabs = {}, {}
        for group, names in GROUPS.items():
            page = QWidget()
            body = QVBoxLayout(page)
            children = QTabWidget()
            children.setDocumentMode(True)
            children.setTabBarAutoHide(True)
            body.addWidget(children)
            self.group_tabs[group] = children
            self.tabs.addTab(page, "")
            for name in names:
                self.pages[name] = ModulePage(name, self)
                children.addTab(self.pages[name], "")
        from desktop_stress import StressPage
        self.stress_page = StressPage(self)
        self.tools_page = QWidget()
        tools_layout = QVBoxLayout(self.tools_page)
        self.tools_tabs = QTabWidget()
        self.tools_tabs.setDocumentMode(True)
        self.tools_tabs.addTab(self.stress_page, "")
        from desktop_hardware import HardwarePage
        self.hardware_tools = {kind: HardwarePage(kind, self) for kind in ("cpu", "gpu", "dram", "disk")}
        for page in self.hardware_tools.values():
            self.tools_tabs.addTab(page, "")
        tools_layout.addWidget(self.tools_tabs)
        self.tabs.addTab(self.tools_page, "")
        self.tabs.tabBar().setTabVisible(4, False)
        self.tools_button = self.button("tools", lambda: self.tabs.setCurrentWidget(self.tools_page))
        self.tools_button.setCheckable(True)
        self.tools_button.setProperty("role", "stress-tab")
        self.tabs.setCornerWidget(self.tools_button, Qt.TopRightCorner)
        self.tabs.currentChanged.connect(lambda index: self.tools_button.setChecked(index == 4))
        self.stress_button = self.tools_button  # Existing integrations use this widget handle.
        from monitor_access import MonitorAccess
        self.monitor_access = MonitorAccess()
        self.monitor_state_timer = QTimer(self)
        self.monitor_state_timer.timeout.connect(self.monitor_access_changed)
        self.open_report.setEnabled(False)
        self.language_combo.currentIndexChanged.connect(self.preferences_changed)
        self.theme_combo.currentIndexChanged.connect(self.preferences_changed)
        self.retranslate()
        self.refresh_controls()
        if restore:
            previous_output = self.settings.value("archiveOutput", "") if output_dir is None else ""
            roots = [destination]
            if output_dir is None:
                roots.extend([self.data_directory / "Outputs", self.data_directory / "reports"])
                if previous_output:
                    roots.append(Path(previous_output))
            self.restore_results(roots)
        if autodetect:
            QTimer.singleShot(0, self.load_hardware)

    def start_monitor_access(self):
        if self.closing or self.monitor_started:
            return
        self.monitor_started = True
        if not self.administrator:
            return
        if not self.monitor_access.available():
            self.stress_page.access_status_key = "monitor_unavailable"
            self.stress_page.retranslate()
            return
        self.monitor_access.start()
        self.stress_page.sync_access()
        self.monitor_state_timer.start(200)

    def monitor_access_changed(self):
        status = self.monitor_access.status
        if status == "starting":
            return
        if self.closing:
            self.monitor_state_timer.stop()
            return
        key = "monitor_standard" if status == "ready" else "monitor_failed"
        if status == "ready":
            self.monitor_state_timer.setInterval(1000)
        else:
            self.monitor_state_timer.stop()
        if self.stress_page.access_status_key == key:
            return
        self.stress_page.access_status_key = key
        self.stress_page.sync_access()
        self.stress_page.retranslate()

    def load_hardware(self):
        if self.closing:
            return
        command = app_command("hardware-info")
        self.hardware_process.start(command[0], command[1:])

    def hardware_finished(self, *_):
        try:
            data = bytes(self.hardware_process.readAllStandardOutput())
            self.hardware_info = json.loads(data[:65536])
        except (ValueError, TypeError):
            self.hardware_info = {}
        self.refresh_hardware()

    def refresh_hardware(self):
        for key, widget in self.hardware_labels.items():
            value = self.hardware_info.get(key)
            text = str(value) if value is not None else tr("hardware_missing", self.language)
            if key == "physical" and value is not None:
                text = tr("core_summary", self.language, physical=value, logical=self.hardware_info.get("logical", "—"))
                if "performance" in self.hardware_info or "efficiency_cores" in self.hardware_info:
                    text += " · " + tr("hybrid_summary", self.language, p=self.hardware_info.get("performance", "—"), e=self.hardware_info.get("efficiency_cores", "—"))
            elif key == "memory_total" and value is not None:
                text += " GiB"
            widget.setText(text)
            widget.setToolTip(text)

    def text(self, key, role):
        widget = label("", role)
        self.bindings.append((widget, key))
        return widget

    def button(self, key, callback):
        widget = QPushButton()
        self.bindings.append((widget, key))
        widget.clicked.connect(callback)
        return widget

    @staticmethod
    def scroll_page():
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)
        scroll.setWidget(root)
        return scroll, layout

    def preferences_changed(self):
        self.language = self.language_combo.currentData()
        self.theme = self.theme_combo.currentData()
        self.settings.setValue("language", self.language)
        self.settings.setValue("theme", self.theme)
        apply_theme(self.theme)
        self.retranslate()

    def retranslate(self):
        app = QApplication.instance()
        if self.translator:
            app.removeTranslator(self.translator)
        self.translator = QTranslator(self)
        if self.language == "zh" and self.translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
            app.installTranslator(self.translator)
        self.setWindowTitle(f"MemoryTest {RELEASE_TAG}")
        for widget, key in self.bindings:
            widget.setText(tr(key, self.language))
        for combo, keys, title in ((self.language_combo, ("zh", "en"), "language"),
                                   (self.theme_combo, ("dark", "light"), "theme")):
            combo.setAccessibleName(tr(title, self.language))
            combo.setToolTip(tr(title, self.language))
            for index, key in enumerate(keys):
                combo.setItemText(index, tr(key, self.language))
        self.threads.setSpecialValueText(tr("all_available", self.language))
        self.output.setAccessibleName(tr("output", self.language))
        self.output.setToolTip(self.output.text())
        if self.output_fallback:
            self.storage_note.setText(tr("output_fallback", self.language) + " · " + tr("auto_output", self.language))
        self.tabs.setTabText(0, tr("home", self.language))
        for index, (group, names) in enumerate(GROUPS.items(), 1):
            self.tabs.setTabText(index, tr(group, self.language))
            for child, name in enumerate(names):
                self.group_tabs[group].setTabText(child, tr(name, self.language))
        self.tabs.setTabText(4, tr("tools", self.language))
        self.tools_tabs.setTabText(0, tr("stress", self.language))
        for index, (kind, page) in enumerate(self.hardware_tools.items(), 1):
            self.tools_tabs.setTabText(index, tr("tool_" + kind, self.language))
            page.retranslate()
        for name, check in self.modules.items():
            check.setText(tr(name, self.language))
            status = self.module_status[name]
            self.labels[name].setText(tr("unavailable" if status == "unsupported" else status, self.language))
            self.labels[name].setToolTip(tr(status, self.language))
            set_tone(self.labels[name], status)
        self.progress.setFormat(tr("progress", self.language, done="%v", total="%m"))
        self.status.setText(tr(self.status_key, self.language))
        self.log.setPlainText("\n".join(tr(name, self.language) + ": " + tr(status, self.language) for name, status in self.log_events))
        for page in self.pages.values():
            page.retranslate()
        self.stress_page.retranslate()
        self.refresh_hardware()
        self.sync_status_indicator()

    def sync_status_indicator(self):
        running = any(self.module_status[name] == "running" for name in self.labels)
        stress = getattr(self, "stress_page", None)
        running = running or bool(stress and stress.active and stress.status_key == "running")
        if running and not self.closing:
            if not self.status_timer.isActive():
                self.status_highlight = True
                self.status_timer.start()
        else:
            self.status_timer.stop()
            self.status_highlight = True
        targets = [(widget, self.module_status[name]) for name, widget in self.labels.items()]
        targets += [(page.summary, page.model.status) for page in self.pages.values()]
        if stress:
            tone = {"backend_error": "failed", "io_error": "failed", "device_unavailable": "unsupported"}.get(stress.status_key, stress.status_key)
            targets.append((stress.status, "not_run" if tone == "running" and not stress.active else tone))
        for widget, status in targets:
            phase = "on" if status == "running" and self.status_highlight else "off" if status == "running" else "steady"
            if widget.property("pulse") != phase or widget.property("tone") != status:
                widget.setProperty("pulse", phase)
                set_tone(widget, status)

    def pulse_status(self):
        self.status_highlight = not self.status_highlight
        self.sync_status_indicator()

    def open_path(self, path):
        if path and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.set_status("open_error")

    def show_licenses(self):
        if self.license_dialog is None:
            from desktop_licenses import LicenseDialog
            self.license_dialog = LicenseDialog(self)
            self.license_dialog.destroyed.connect(lambda: setattr(self, "license_dialog", None))
        self.license_dialog.show()
        self.license_dialog.raise_()
        self.license_dialog.activateWindow()

    def restore_results(self, roots):
        # Apply retention before selecting a result, so restored report buttons
        # cannot immediately point at an archive this launch just removed.
        _, cleanup_errors = prune_archives(Path(self.output.text()))
        for _, directory, data in recent_archives(roots, "benchmark"):
            try:
                rows = data["modules"]
                restored = []
                # Do not mix independent rounds or expose paths outside the archive.
                for row in rows:
                    name = row["module"]
                    path = local_file(directory, "reports/" + TESTS[name]["report"])
                    if row.get("status") == "passed" and path is None:
                        raise ValueError("missing report")
                    if name == "cpu_multi" and row.get("status") == "passed":
                        if local_file(directory, "reports/compute_mark_v2.json") is None:
                            raise ValueError("missing score data")
                    model = build_module(name, directory / "reports", row)
                    if row.get("status") == "passed" and model.status != "passed":
                        raise ValueError("invalid result")
                    restored.extend((row, part) for part in split_legacy_result(model))
                self.run_dir = directory
                self.report = local_file(directory, data.get("html_report"))
                self.completed.clear()
                self.progress.setRange(0, len(restored))
                for row, model in restored:
                    row = dict(row)
                    row["module"] = model.name
                    if row.get("status") in {"running", "queued"}:
                        model.status = "interrupted"
                    row["status"] = model.status
                    row["log"] = str(local_file(directory, row.get("log")) or "")
                    self.module_status[model.name] = model.status
                    self.labels[model.name].setText(tr(model.status, self.language))
                    set_tone(self.labels[model.name], model.status)
                    self.completed.add(model.name)
                    self.progress.setValue(len(self.completed))
                    page = self.pages[model.name]
                    page.completed(directory, row, model=model)
                    page.html = self.report
                    page.open.setEnabled(self.report is not None)
                    page.saved_run_id = directory.name
                    page.retranslate()
                self.open_report.setEnabled(self.report is not None)
                self.saved_run.setText(directory.name)
                self.set_status("restored")
                break
            except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
                continue
        for _, directory, data in recent_archives(roots, "stress"):
            if self.stress_page.restore_archive(directory, data):
                break
        # Only the current output gets retention; legacy directories are read-only.
        if cleanup_errors:
            self.set_status("cleanup_error")

    def set_status(self, key):
        self.status_key = key
        self.status.setText(tr(key, self.language))

    def refresh_controls(self):
        busy = self.benchmark_active or self.stress_active
        for widget in [self.start, self.run_all, self.threads, *self.group_buttons]:
            widget.setEnabled(not busy)
        self.cancel.setEnabled(self.benchmark_active)
        for name, check in self.modules.items():
            check.setEnabled(not busy and supported(name))
            self.pages[name].run.setEnabled(not busy and supported(name))
            self.pages[name].cancel.setEnabled(self.benchmark_active and name in self.active_modules)
        if hasattr(self, "stress_page"):
            self.stress_page.refresh_controls()

    def set_running(self, active):
        self.benchmark_active = active
        self.refresh_controls()
        self.sync_status_indicator()

    def set_stress_running(self, active):
        self.stress_active = active
        self.refresh_controls()
        self.sync_status_indicator()
        if self.closing and not active:
            self.close()

    def begin_all(self):
        self.begin([name for name in self.modules if supported(name)])

    def begin(self, modules=None):
        if self.benchmark_active or self.stress_active:
            self.set_status("busy")
            return
        modules = list(modules) if modules is not None else [name for name, check in self.modules.items() if check.isChecked()]
        if not modules:
            self.set_status("no_selection")
            return
        try:
            output = Path(self.output.text()).expanduser().resolve()
            output.mkdir(parents=True, exist_ok=True)
            self.control = tempfile.TemporaryDirectory(prefix="memorytest-control-")
        except OSError:
            self.set_status("io_error")
            return
        self.report = self.run_dir = None
        self.active_modules = modules
        self.completed.clear()
        self.log_events.clear()
        self.buffer = b""
        self.log.clear()
        self.open_report.setEnabled(False)
        self.progress.setRange(0, len(modules))
        self.progress.setValue(0)
        self.output.setText(str(output))
        self.output.setToolTip(str(output))
        self.settings.setValue("archiveOutput", str(output))
        self.saved_run.clear()
        for name in modules:
            self.module_status[name] = "queued"
            self.labels[name].setText(tr("queued", self.language))
            set_tone(self.labels[name], "queued")
            self.pages[name].queued()
        command = app_command("worker") + ["--skip-build", "--skip-charts", "--events-jsonl",
            "--output-dir", str(output), "--bin-dir", str(ROOT / "bin"),
            "--cancel-file", str(Path(self.control.name) / "cancel"), "--parent-pid", str(os.getpid()),
            "--language", self.language, "--theme", self.theme]
        for name in modules:
            command.extend(["--module", name])
        if self.threads.value():
            command.extend(["--cpu-threads", str(self.threads.value())])
        self.set_running(True)
        self.set_status("running")
        self.process.setWorkingDirectory(str(output))
        self.process.start(command[0], command[1:])

    def read_output(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace")[:8192]
            if text.startswith("@memorytest "):
                try:
                    self.handle_event(json.loads(text[len("@memorytest "):]))
                except (ValueError, KeyError, TypeError):
                    self.set_status("read_error")
            elif self.run_dir:
                try:
                    with (self.run_dir / "desktop-worker.log").open("a", encoding="utf-8") as stream:
                        stream.write(text + "\n")
                except OSError:
                    pass
        if len(self.buffer) > 65536:
            self.buffer = b""

    def handle_event(self, data):
        if data["type"] == "run":
            self.run_dir = Path(data["path"])
        elif data["type"] == "module":
            name, status = data["module"], data["status"]
            if name not in self.pages:
                return
            self.module_status[name] = status
            self.labels[name].setText(tr(status, self.language))
            set_tone(self.labels[name], status)
            self.log_events.append((name, status))
            self.log.appendPlainText(tr(name, self.language) + ": " + tr(status, self.language))
            if status == "running":
                self.pages[name].started()
            else:
                self.completed.add(name)
                self.progress.setValue(len(self.completed))
                if self.run_dir:
                    self.pages[name].completed(self.run_dir, data)
                else:
                    self.pages[name].model = ModuleResult(name, status)
                    self.pages[name].progress.setRange(0, 1)
                    self.pages[name].progress.setValue(1 if status == "passed" else 0)
                    self.pages[name].retranslate()
            self.sync_status_indicator()
        elif data["type"] == "finished":
            self.report = Path(data["report"])
            self.open_report.setEnabled(self.report.is_file())
            for name in self.active_modules:
                self.pages[name].html = self.report
                self.pages[name].open.setEnabled(self.report.is_file())
            if self.run_dir:
                self.saved_run.setText(self.run_dir.name)
            if data.get("cleanup_errors"):
                self.set_status("cleanup_error")

    def request_cancel(self):
        if self.control:
            Path(self.control.name, "cancel").touch()
            self.cancel.setEnabled(False)
            for page in self.pages.values():
                page.cancel.setEnabled(False)
            self.set_status("stopping")

    def process_error(self, error):
        if error == QProcess.FailedToStart:
            self.finished(-1)

    def finished(self, code, *_):
        self.read_output()
        for name in self.active_modules:
            if name not in self.completed:
                self.pages[name].model = ModuleResult(name, "failed", reason="worker_failed")
                self.pages[name].progress.setRange(0, 1)
                self.pages[name].progress.setValue(0)
                self.pages[name].retranslate()
                self.module_status[name] = "failed"
                self.labels[name].setText(tr("failed", self.language))
                set_tone(self.labels[name], "failed")
        self.set_running(False)
        self.set_status("cleanup_error" if code == 0 and self.status_key == "cleanup_error" else
                        "passed" if code == 0 else "interrupted" if code == 130 else "incomplete")
        if self.control:
            self.control.cleanup()
            self.control = None
        if self.closing:
            self.close()

    def closeEvent(self, event):
        self.closing = True
        self.status_timer.stop()
        for page in self.hardware_tools.values():
            page.shutdown()
        if self.hardware_process.state() != QProcess.NotRunning:
            self.stress_page.kill_tree(self.hardware_process)
            self.hardware_process.waitForFinished(1000)
        if self.benchmark_active or self.stress_active:
            self.closing = True
            if self.benchmark_active:
                self.request_cancel()
            if self.stress_active:
                self.stress_page.stop()
            event.ignore()
        else:
            self.monitor_state_timer.stop()
            self.monitor_access.close()
            self.stress_page.shutdown()
            event.accept()
