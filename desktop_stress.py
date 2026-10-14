"""Native stress controls, bounded live plots and full-session sensor statistics."""
from collections import deque
import json
import math
import os
from pathlib import Path
import tempfile
import time

from PySide6.QtCore import Qt, QProcess, QTimer, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QHeaderView, QVBoxLayout, QWidget)

from benchmark_registry import ROOT
from desktop_theme import label
from desktop_widgets import ReadableSpinBox
from i18n import display, tr
from presentation import THEMES, curve_color
from process_control import app_command
from stress_common import MODES, Statistics


class LivePlot(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.samples = deque(maxlen=601)
        self.catalog, self.visible, self.colors = {}, set(), {}
        self.window_seconds = 300
        self.setMinimumHeight(230)

    def append(self, elapsed, values):
        self.samples.append((elapsed, dict(values)))
        while self.samples and elapsed - self.samples[0][0] > 600:
            self.samples.popleft()
        self.update()

    def refresh(self):
        units = {self.catalog[key]["unit"] for key in self.visible if key in self.catalog}
        self.setMinimumHeight(max(230, len(units) * 235))
        self.update()

    def set_window_minutes(self, minutes):
        self.window_seconds = min(10, max(1, int(minutes))) * 60
        self.update()

    def visible_samples(self):
        start = max(0, self.samples[-1][0] - self.window_seconds) if self.samples else 0
        return [(elapsed, values) for elapsed, values in self.samples if elapsed >= start]

    def panel_units(self):
        order = {"°C": 0, "%": 1, "MHz": 2, "W": 3, "V": 4, "RPM": 5}
        return sorted({self.catalog[key]["unit"] for key in self.visible if key in self.catalog}, key=lambda unit: (order.get(unit, 99), unit))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = THEMES[self.window.theme]
        painter.fillRect(self.rect(), QColor(palette["surface"]))
        selected = [key for key in self.catalog if key in self.visible]
        units = self.panel_units()
        if not units or not self.samples:
            painter.setPen(QColor(palette["muted"]))
            painter.drawText(self.rect(), Qt.AlignCenter, tr("no_data", self.window.language))
            return
        samples = self.visible_samples()
        start = max(0, self.samples[-1][0] - self.window_seconds)
        end = max(start + 1, self.samples[-1][0])
        for panel, unit in enumerate(units):
            if not event.rect().intersects(QRectF(0, panel * 235, self.width(), 235).toRect()):
                continue
            ids = [key for key in selected if self.catalog[key]["unit"] == unit]
            top, bottom = panel * 235 + 45, panel * 235 + 195
            left, right = 65, max(100, self.width() - 20)
            numbers = [values[key] for _, values in samples for key in ids if isinstance(values.get(key), (int, float)) and math.isfinite(values[key])]
            maximum = max(numbers, default=1) or 1
            minimum = min(0, min(numbers, default=0))
            painter.setPen(QColor(palette["text"]))
            title = tr(self.catalog[ids[0]]["kind"], self.window.language) + " · " + unit
            painter.drawText(QRectF(12, panel * 235 + 4, self.width() - 24, 25), Qt.AlignLeft, title)
            for i in range(4):
                y = bottom - (bottom - top) * i / 3
                painter.setPen(QColor(palette["border"]))
                painter.drawLine(QPointF(left, y), QPointF(right, y))
                painter.setPen(QColor(palette["muted"]))
                painter.drawText(QRectF(0, y - 10, left - 9, 20), Qt.AlignRight, f"{minimum + (maximum - minimum) * i / 3:.3g}")
            for i in range(5):
                seconds = start + (end - start) * i / 4
                x = left + (right - left) * i / 4
                painter.drawText(QRectF(x - 30, bottom + 8, 60, 20), Qt.AlignCenter, f"{int(seconds)//60}:{int(seconds)%60:02d}")
            for key in ids:
                painter.setPen(QPen(QColor(curve_color(self.colors[key], self.window.theme)), 1.8))
                path, connected = QPainterPath(), False
                previous_elapsed = None
                for elapsed, values in samples:
                    if previous_elapsed is not None and elapsed - previous_elapsed > 2.5:
                        connected = False
                    previous_elapsed = elapsed
                    value = values.get(key)
                    if not isinstance(value, (int, float)) or not math.isfinite(value):
                        connected = False
                        continue
                    x = left + (elapsed - start) / (end - start) * (right - left)
                    y = bottom - (value - minimum) / (maximum - minimum or 1) * (bottom - top)
                    if connected:
                        path.lineTo(x, y)
                    else:
                        path.moveTo(x, y)
                    connected = True
                painter.drawPath(path)


class StressPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.active = False
        self.generation = 0
        self.caps = None
        self.control = None
        self.run_dir = self.report = None
        self.status_key = "ready"
        self.has_temperature = False
        self.permission_denied = False
        self.access_status_key = "monitor_standard"
        self.catalog, self.stats = {}, {}
        self.preview_stats = Statistics()
        self.preview_counter = 0
        self.preview_started = time.monotonic()
        self.preview_restart = False
        self.bindings, self.buffers = [], {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 16, 0, 0)
        heading = QHBoxLayout()
        self.title = label("", "title")
        heading.addWidget(self.title)
        heading.addStretch()
        self.elapsed = label("0:00", "status")
        heading.addWidget(self.elapsed)
        layout.addLayout(heading)
        modes = QHBoxLayout()
        self.modes = {}
        for mode in MODES:
            check = QCheckBox()
            check.setChecked(mode == "cpu")
            check.setEnabled(False)
            check.toggled.connect(self.change_modes)
            self.modes[mode] = check
            modes.addWidget(check)
        modes.addStretch()
        layout.addLayout(modes)
        config = QHBoxLayout()
        self.threads, self.memory, self.duration = (ReadableSpinBox() for _ in range(3))
        self.threads.setRange(1, 256)
        self.memory.setRange(1, 2048)
        self.memory.setSuffix(" MiB")
        self.duration.setRange(0, 10080)
        for key, spin in (("threads", self.threads), ("memory_budget", self.memory), ("time_limit", self.duration)):
            item = label("", "muted")
            self.bindings.append((item, key))
            config.addWidget(item)
            config.addWidget(spin)
        layout.addLayout(config)
        control = QHBoxLayout()
        self.start_button, self.stop_button, self.export_button = QPushButton(), QPushButton(), QPushButton()
        self.start_button.setProperty("role", "primary")
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.export_button.clicked.connect(self.export)
        self.enhanced = QCheckBox()
        self.enhanced.toggled.connect(self.enhanced_changed)
        for widget in (self.start_button, self.stop_button, self.export_button, self.enhanced):
            control.addWidget(widget)
        control.addStretch()
        layout.addLayout(control)
        monitor = QHBoxLayout()
        window_label = label("", "muted")
        self.bindings.append((window_label, "plot_window"))
        monitor.addWidget(window_label)
        self.window_minutes = ReadableSpinBox()
        self.window_minutes.setRange(1, 10)
        self.window_minutes.setValue(5)
        self.window_minutes.setFixedWidth(170)
        monitor.addWidget(self.window_minutes)
        monitor.addStretch()
        layout.addLayout(monitor)
        self.status = label("", "status")
        layout.addWidget(self.status)
        self.warning = label("", "muted")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        self.access_status = label("", "muted")
        self.access_status.setWordWrap(True)
        layout.addWidget(self.access_status)
        splitter = QSplitter(Qt.Vertical)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.plot = LivePlot(window)
        self.window_minutes.valueChanged.connect(self.plot.set_window_minutes)
        scroll.setWidget(self.plot)
        splitter.addWidget(scroll)
        self.table = QTableWidget(0, 7)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setMinimumSectionSize(48)
        self.table.setColumnWidth(0, 52)
        self.columns_initialized = False
        self.columns_user_resized = False
        self.adjusting_columns = False
        self.table.horizontalHeader().sectionResized.connect(self.column_resized)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemChanged.connect(self.sensor_toggled)
        splitter.addWidget(self.table)
        splitter.setSizes([280, 220])
        layout.addWidget(splitter, 1)
        self.process, self.probe, self.preview = (QProcess(self) for _ in range(3))
        for process in (self.process, self.probe, self.preview):
            process.setProcessChannelMode(QProcess.MergedChannels)
            process.readyReadStandardOutput.connect(lambda p=process: self.read(p))
            self.buffers[process] = b""
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(lambda err: self.finished(-1) if err == QProcess.FailedToStart else None)
        self.probe.finished.connect(self.probe_finished)
        self.preview.finished.connect(self.preview_finished)
        self.retranslate()

    def retranslate(self):
        lang = self.window.language
        self.title.setText(tr("stress", lang))
        for item, key in self.bindings:
            item.setText(tr(key, lang))
        for mode, check in self.modes.items():
            check.setText(tr(mode, lang))
            check.setToolTip("" if check.isEnabled() else tr("unsupported", lang))
        for button, key in ((self.start_button, "start"), (self.stop_button, "stop"), (self.export_button, "export"), (self.enhanced, "enhanced")):
            button.setText(tr(key, lang))
        self.enhanced.setToolTip(tr("enhanced_hint", lang))
        self.duration.setSpecialValueText(tr("manual_stop", lang))
        self.duration.setSuffix(" " + tr("minutes", lang) if self.duration.value() else "")
        self.window_minutes.setSuffix(" " + tr("minutes", lang))
        self.status.setText(tr(self.status_key, lang))
        self.access_status.setText(tr(self.access_status_key, lang))
        self.access_status.setVisible(bool(self.access_status.text()))
        self.refresh_warning()
        self.table.setHorizontalHeaderLabels([tr(key, lang) for key in ("show", "sensors", "unit", "current", "minimum", "maximum", "average")])
        self.refresh_table()
        self.plot.refresh()
        self.refresh_controls()

    def refresh_controls(self):
        self.start_button.setEnabled(not self.active and not self.window.benchmark_active and self.caps is not None and any(self.caps["modes"].values()))
        self.stop_button.setEnabled(self.active)
        self.export_button.setEnabled(self.report is not None and not self.active)
        for widget in (self.threads, self.memory, self.duration, self.enhanced):
            widget.setEnabled(not self.active)

    def refresh_warning(self):
        lang = self.window.language
        notices = []
        if self.active and self.selected_modes():
            notices.append(tr("stress_warning", lang))
            if not self.has_temperature:
                notices.append(tr("no_temperature", lang))
        if self.permission_denied:
            notices.append(tr("sensor_denied", lang))
        self.warning.setText(" ".join(notices))
        self.warning.setVisible(bool(notices))

    def showEvent(self, event):
        super().showEvent(event)
        if not self.columns_initialized:
            QTimer.singleShot(0, self.equalize_columns)
        if self.caps is None and self.probe.state() == QProcess.NotRunning:
            command = app_command("stress-worker") + ["--probe", "--bin-dir", str(ROOT / "bin")]
            self.probe.start(command[0], command[1:])
        self.start_preview()
        self.sync_access()
        self.refresh_controls()

    def equalize_columns(self):
        if self.columns_initialized or self.table.viewport().width() < 100:
            return
        width = max(48, (self.table.viewport().width() - self.table.columnWidth(0)) // 6)
        self.adjusting_columns = True
        for column in range(1, 7):
            self.table.setColumnWidth(column, width)
        self.adjusting_columns = False
        self.columns_initialized = True

    def column_resized(self, section, *_):
        if section and self.columns_initialized and not self.adjusting_columns:
            self.columns_user_resized = True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "table") and not self.columns_user_resized:
            self.columns_initialized = False
            QTimer.singleShot(0, self.equalize_columns)

    def hideEvent(self, event):
        self.stop_preview()
        super().hideEvent(event)
        self.sync_access()

    def start_preview(self):
        if self.active or self.run_dir or self.preview.state() != QProcess.NotRunning or not self.isVisible():
            return
        command = app_command("sensor-worker") + ["--parent-pid", str(os.getpid()), "--bin-dir", str(ROOT / "bin")]
        if self.enhanced.isChecked():
            command.append("--enhanced")
            command += ["--privileged-samples", str(self.window.monitor_access.samples_path)]
        self.buffers[self.preview] = b""
        self.preview.start(command[0], command[1:])

    def stop_preview(self):
        if self.preview.state() != QProcess.NotRunning:
            self.preview.terminate()

    def enhanced_changed(self):
        if self.enhanced.isChecked():
            self.window.request_monitor_access()
        self.sync_access()
        self.preview_restart = self.preview.state() != QProcess.NotRunning
        if self.preview_restart:
            self.stop_preview()
        else:
            self.start_preview()

    def sync_access(self):
        if hasattr(self.window, "monitor_access"):
            self.window.monitor_access.set_active(self.enhanced.isChecked() and
                (self.active or (self.isVisible() and not self.run_dir)))

    def preview_finished(self, *_):
        if self.preview_restart:
            self.preview_restart = False
            self.start_preview()

    def probe_finished(self, *_):
        self.read(self.probe)
        if self.caps is None:
            self.status_key = "device_unavailable"
        self.retranslate()

    def read(self, process):
        self.buffers[process] += bytes(process.readAllStandardOutput())
        while b"\n" in self.buffers[process]:
            line, self.buffers[process] = self.buffers[process].split(b"\n", 1)
            try:
                raw = line.decode("utf-8")
                if process == self.preview:
                    data = json.loads(raw)
                    if not self.active:
                        self.preview_stats.sample(data["values"])
                        self.preview_counter += 1
                        self.sample(dict(data, statistics=self.preview_stats.rows, elapsed=time.monotonic() - self.preview_started))
                elif raw.startswith("@memorytest "):
                    self.handle(json.loads(raw[12:]))
            except (ValueError, KeyError, TypeError):
                pass
        if len(self.buffers[process]) > 262144:
            self.buffers[process] = b""

    def handle(self, data):
        if data["type"] == "stress_capabilities":
            self.caps = data
            for mode, check in self.modes.items():
                check.setEnabled(data["modes"].get(mode, False))
                if not check.isEnabled():
                    check.setChecked(False)
            self.threads.setMaximum(data["threads"])
            self.threads.setValue(data["threads"])
            self.memory.setMaximum(max(1, data["max_memory_mib"]))
            self.memory.setValue(max(1, data["default_memory_mib"]))
            self.retranslate()
        elif data["type"] == "stress_run":
            self.run_dir = Path(data["path"])
        elif data["type"] == "stress_sample":
            self.status_key = "running" if data.get("modes") else "idle_load"
            self.sample(data)
        elif data["type"] == "stress_finished":
            self.status_key = data["reason"]
            self.report = Path(data["report"]) if data.get("report") else None
            self.retranslate()

    def sample(self, data):
        old_keys = set(self.catalog)
        self.catalog.update(data.get("catalog", {}))
        self.stats = data.get("statistics", {})
        self.has_temperature = data.get("has_temperature", False)
        self.permission_denied = data.get("permission_denied", False)
        for key, item in self.catalog.items():
            if key not in self.plot.colors:
                self.plot.colors[key] = len(self.plot.colors)
                self.plot.visible.add(key)
        self.plot.catalog = self.catalog
        self.plot.append(data["elapsed"], data["values"])
        self.elapsed.setText(f'{int(data["elapsed"])//60}:{int(data["elapsed"])%60:02d}')
        # No widget relabelling, geometry negotiation or full retranslation at 1 Hz.
        self.status.setText(tr(self.status_key, self.window.language))
        self.refresh_warning()
        self.refresh_table()
        if old_keys != set(self.catalog):
            self.plot.refresh()

    def refresh_table(self):
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        if self.table.rowCount() != len(self.catalog):
            self.table.setRowCount(len(self.catalog))
        for row, (key, sensor) in enumerate(self.catalog.items()):
            checkbox = self.table.item(row, 0)
            if checkbox is None:
                checkbox = QTableWidgetItem()
                self.table.setItem(row, 0, checkbox)
            checkbox.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            checkbox.setData(Qt.UserRole, key)
            checkbox.setCheckState(Qt.Checked if key in self.plot.visible else Qt.Unchecked)
            values = [display(sensor["label"], self.window.language), sensor["unit"],
                      *[display(self.stats.get(key, {}).get(field), self.window.language) for field in ("current", "minimum", "maximum", "average")]]
            for column, value in enumerate(values, 1):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                if item.text() != value:
                    item.setText(value)
                if column == 1:
                    item.setToolTip(value + "\n" + key)
                if column == 1:
                    item.setForeground(QColor(curve_color(self.plot.colors.get(key, 0), self.window.theme)))
        self.table.blockSignals(False)
        self.table.setUpdatesEnabled(True)

    def sensor_toggled(self, item):
        if item.column() != 0:
            return
        key = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self.plot.visible.add(key)
        else:
            self.plot.visible.discard(key)
        self.plot.refresh()

    def selected_modes(self):
        return [mode for mode, check in self.modes.items() if check.isChecked() and check.isEnabled()]

    def command(self, **data):
        if not self.control:
            return
        path = Path(self.control.name) / "request.json"
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(data), encoding="utf-8")
        pending.replace(path)

    def change_modes(self):
        if self.active:
            try:
                self.command(modes=self.selected_modes())
            except OSError:
                self.stop()

    def start(self):
        if self.active or self.window.benchmark_active or self.caps is None:
            return
        self.stop_preview()
        try:
            self.control = tempfile.TemporaryDirectory(prefix="memorytest-stress-control-")
            output = Path(self.window.output.text()).expanduser().resolve()
            output.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.status_key = "io_error"
            self.retranslate()
            return
        self.run_dir = self.report = None
        self.stats = {}
        self.plot.samples.clear()
        self.buffers[self.process] = b""
        self.status_key = "running"
        self.active = True
        self.generation += 1
        self.window.set_stress_running(True)
        command = app_command("stress-worker") + ["--threads", str(self.threads.value()), "--memory-mib", str(self.memory.value()),
            "--duration", str(self.duration.value() * 60), "--control", str(Path(self.control.name) / "request.json"),
            "--parent-pid", str(os.getpid()), "--output-dir", str(output), "--bin-dir", str(ROOT / "bin"),
            "--language", self.window.language, "--theme", self.window.theme]
        selected = self.selected_modes()
        for mode in selected:
            command.extend(["--mode", mode])
        if not selected:
            command.append("--no-load")
        if self.enhanced.isChecked():
            command.append("--enhanced")
            command += ["--privileged-samples", str(self.window.monitor_access.samples_path)]
        self.sync_access()
        self.process.start(command[0], command[1:])
        self.retranslate()

    def stop(self):
        if self.active:
            self.status_key = "stopping"
            try:
                self.command(stop=True)
            except OSError:
                self.process.terminate()
            generation = self.generation
            QTimer.singleShot(8000, lambda: self.force_stop(generation))
            self.retranslate()

    @staticmethod
    def kill_tree(process):
        import psutil
        if process.state() == QProcess.NotRunning:
            return
        try:
            parent = psutil.Process(process.processId())
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            parent.kill()
        except psutil.Error:
            pass

    def force_stop(self, generation=None):
        if self.active and (generation is None or generation == self.generation):
            self.kill_tree(self.process)

    def finished(self, code, *_):
        self.read(self.process)
        self.active = False
        if self.status_key in {"running", "stopping", "idle_load"}:
            self.status_key = "backend_error" if code else "stopped"
        if self.control:
            self.control.cleanup()
            self.control = None
        self.window.set_stress_running(False)
        self.sync_access()
        self.retranslate()

    def export(self):
        if self.run_dir and not self.active:
            try:
                from stress_report import build_stress_report
                self.report = self.run_dir / "stress_report.html"
                build_stress_report(self.run_dir, self.report, self.window.language, self.window.theme,
                                    window_seconds=self.plot.window_seconds)
                self.window.open_path(self.report)
            except (OSError, ValueError):
                self.status_key = "io_error"
                self.retranslate()

    def shutdown(self):
        self.stop_preview()
        if self.probe.state() != QProcess.NotRunning:
            self.kill_tree(self.probe)
        # Only lightweight probe/monitor processes remain here; no load process.
        for process in (self.preview, self.probe):
            if process.state() != QProcess.NotRunning:
                process.waitForFinished(1000)
                if process.state() != QProcess.NotRunning:
                    self.kill_tree(process)
                    process.waitForFinished(1000)
