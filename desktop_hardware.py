"""Native inspector pages: lazy read-only workers and source-labelled snapshots."""
import copy
from datetime import datetime
import json
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
    QGridLayout, QHeaderView, QHBoxLayout, QPushButton, QScrollArea, QSizePolicy,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from desktop_theme import card, label, set_tone
from i18n import MESSAGES, tr
from process_control import app_command

MAX_OUTPUT = 2_000_000


def public_snapshot(snapshot, show_serials=False):
    result = copy.deepcopy(snapshot)
    operational = {"smartctl_missing", "smart_read_error", "hardware_read_failed", "hardware_no_devices"}
    result["notices"] = [key for key in result.get("notices", []) if key in operational]
    for dev in result.get("devices", []):
        dev["notices"] = [key for key in dev.get("notices", []) if key in operational]
    if not show_serials:
        for dev in result.get("devices", []):
            for group in dev.get("groups", []):
                for field in group.get("fields", []):
                    if field.get("private"):
                        field["value"] = None
    return result


class HardwarePage(QWidget):
    def __init__(self, kind, window):
        super().__init__()
        self.kind, self.window = kind, window
        self.snapshot = None
        self.buffer = bytearray()
        self.status_key = "hardware_not_read"
        self.stop_reason = None
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.setInterval(35000)
        self.deadline.timeout.connect(lambda: self.cancel("timeout"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 16, 0, 0)
        self.title = label("", "title")
        layout.addWidget(self.title)
        toolbar = QHBoxLayout()
        self.selector = QComboBox()
        self.selector.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.selector.currentIndexChanged.connect(self.render)
        toolbar.addWidget(self.selector, 2)
        self.refresh_button, self.cancel_button, self.copy_button, self.export_button = (QPushButton() for _ in range(4))
        self.refresh_button.setProperty("role", "primary")
        self.refresh_button.clicked.connect(self.refresh)
        self.cancel_button.clicked.connect(lambda: self.cancel("interrupted"))
        self.copy_button.clicked.connect(self.copy_snapshot)
        self.export_button.clicked.connect(self.export_snapshot)
        for button in (self.refresh_button, self.cancel_button, self.copy_button, self.export_button):
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            toolbar.addWidget(button, 1)
        layout.addLayout(toolbar)
        row = QHBoxLayout()
        self.status = label("", "status")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.serials = QCheckBox()
        self.serials.toggled.connect(self.render)
        row.addWidget(self.serials)
        layout.addLayout(row)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        layout.addWidget(self.scroll, 1)
        self.retranslate()

    def showEvent(self, event):
        super().showEvent(event)
        if self.snapshot is None and self.status_key == "hardware_not_read":
            self.refresh()

    def refresh(self):
        if self.process.state() != QProcess.NotRunning or self.window.closing:
            return
        self.buffer.clear()
        self.stop_reason = None
        self.status_key = "hardware_reading"
        # Clear stale values so a failed refresh cannot present an old health
        # reading as current. Selected device is recovered by stable identity.
        self.previous_device = self.selector.currentData()
        self.snapshot = None
        self.selector.clear()
        self.render()
        command = app_command("hardware-tool") + ["--kind", self.kind]
        self.process.start(command[0], command[1:])
        self.deadline.start()
        self.refresh_controls()

    def read_output(self):
        chunk = bytes(self.process.readAllStandardOutput())
        if len(self.buffer) + len(chunk) > MAX_OUTPUT:
            self.cancel("hardware_read_failed")
            return
        self.buffer.extend(chunk)

    def process_error(self, error):
        if error == QProcess.FailedToStart:
            self.deadline.stop()
            self.status_key = "hardware_read_failed"
            self.refresh_controls()
            self.render()

    def finished(self, code, *_):
        self.deadline.stop()
        self.read_output()
        self.status_key = self.stop_reason or "hardware_read_failed"
        if not self.stop_reason and code == 0:
            try:
                data = json.loads(bytes(self.buffer))
                if data.get("schema") != 1 or data.get("kind") != self.kind or not isinstance(data.get("devices"), list):
                    raise ValueError("invalid inspector output")
                # Worker is local, but reject incomplete shape before rendering.
                for dev in data["devices"]:
                    if not isinstance(dev, dict) or not all(key in dev for key in ("id", "name", "groups", "tables", "notices")):
                        raise ValueError("invalid device")
                self.snapshot = data
                self.status_key = "hardware_snapshot"
                self.selector.blockSignals(True)
                self.selector.clear()
                for dev in data["devices"]:
                    self.selector.addItem(self.local(dev["name"]), dev["id"])
                restored = self.selector.findData(getattr(self, "previous_device", None))
                if restored >= 0:
                    self.selector.setCurrentIndex(restored)
                self.selector.blockSignals(False)
            except (ValueError, TypeError, AttributeError):
                self.snapshot = None
        self.refresh_controls()
        self.render()

    def cancel(self, reason="interrupted"):
        self.stop_reason = reason
        self.deadline.stop()
        if self.process.state() != QProcess.NotRunning:
            self.window.stress_page.kill_tree(self.process)
        self.status_key = reason
        self.refresh_controls()

    def shutdown(self):
        self.cancel()
        if self.process.state() != QProcess.NotRunning:
            self.process.waitForFinished(1500)

    def local(self, key):
        lookup = "hw_" + str(key)
        return tr(lookup, self.window.language) if lookup in MESSAGES else str(key)

    def value(self, field):
        if field.get("private") and not self.serials.isChecked() and field.get("value") is not None:
            return tr("serial_hidden", self.window.language)
        value = field.get("value")
        return self.local(value) if value is not None else tr("hardware_unavailable", self.window.language)

    def refresh_controls(self):
        busy = self.process.state() != QProcess.NotRunning
        self.refresh_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.copy_button.setEnabled(self.snapshot is not None and not busy)
        self.export_button.setEnabled(self.snapshot is not None and not busy)
        message = tr(self.status_key, self.window.language)
        if self.snapshot:
            try:
                stamp = datetime.fromisoformat(self.snapshot.get("collected_at", "")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                stamp = self.snapshot.get("collected_at", "")
            message += " · " + stamp
        self.status.setText(message)

    def retranslate(self):
        self.title.setText(tr("tool_" + self.kind, self.window.language))
        for button, key in ((self.refresh_button, "refresh"), (self.cancel_button, "cancel"),
                            (self.copy_button, "copy"), (self.export_button, "export")):
            button.setText(tr(key, self.window.language))
        self.serials.setText(tr("show_serials", self.window.language))
        self.selector.setAccessibleName(tr("select_device", self.window.language))
        if self.snapshot:
            for index, dev in enumerate(self.snapshot["devices"]):
                self.selector.setItemText(index, self.local(dev["name"]))
        self.refresh_controls()
        self.render()

    def render(self, *_):
        position = self.scroll.verticalScrollBar().value()
        old = self.scroll.takeWidget()
        if old:
            old.deleteLater()
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 8, 8, 0)
        layout.setSpacing(14)
        devs = self.snapshot.get("devices", []) if self.snapshot else []
        index = self.selector.currentIndex()
        dev = devs[index] if 0 <= index < len(devs) else None
        notices = set((self.snapshot or {}).get("notices", []) + (dev.get("notices", []) if dev else []))
        for key in ("smartctl_missing", "smart_read_error", "hardware_read_failed"):
            if key not in notices:
                continue
            notice = label(tr(key, self.window.language), "status")
            notice.setWordWrap(True)
            layout.addWidget(notice)
        if dev:
            if self.kind == "disk":
                health = dev.get("health", "smart_unknown")
                metrics = {f["name"]: f for section in dev.get("groups", []) for f in section["fields"]}
                summary = QHBoxLayout()
                for name in ("health", "temperature", "power_on_hours"):
                    panel, body = card()
                    body.addWidget(label(self.local(name), "muted"))
                    value = label(self.local(health) if name == "health" else self.value(metrics.get(name, {})), "metric")
                    value.setWordWrap(True)
                    value.setMinimumWidth(0)
                    if name == "health":
                        value.setObjectName("disk-health")
                        set_tone(value, {"smart_passed": "passed", "smart_failed": "failed"}.get(health, "unsupported"))
                    body.addWidget(value)
                    summary.addWidget(panel, 1)
                layout.addLayout(summary)
            for section in dev.get("groups", []):
                panel, body = card()
                body.addWidget(label(self.local(section["title"]), "section"))
                grid = QGridLayout()
                grid.setHorizontalSpacing(18)
                grid.setVerticalSpacing(10)
                for n, field in enumerate(section["fields"]):
                    row, col = n // 2, (n % 2) * 2
                    key = label(self.local(field["name"]), "muted")
                    key.setWordWrap(True)
                    val = label(self.value(field), "value")
                    val.setMinimumWidth(0)
                    val.setWordWrap(True)
                    val.setTextFormat(Qt.PlainText)
                    val.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    val.setToolTip(field.get("source", ""))
                    grid.addWidget(key, row, col, Qt.AlignTop)
                    grid.addWidget(val, row, col + 1, Qt.AlignTop)
                grid.setColumnStretch(1, 1)
                grid.setColumnStretch(3, 1)
                body.addLayout(grid)
                layout.addWidget(panel)
            for spec in dev.get("tables", []):
                panel, body = card()
                body.addWidget(label(self.local(spec["title"]), "section"))
                view = QTableWidget(len(spec["rows"]), len(spec["columns"]))
                view.setHorizontalHeaderLabels([self.local(k) for k in spec["columns"]])
                view.setEditTriggers(QTableWidget.NoEditTriggers)
                view.setAlternatingRowColors(True)
                view.verticalHeader().hide()
                view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
                view.horizontalHeader().setStretchLastSection(True)
                for row, cells in enumerate(spec["rows"]):
                    for col, value in enumerate(cells[:len(spec["columns"])]):
                        view.setItem(row, col, QTableWidgetItem(self.local(value) if value is not None else "—"))
                view.setMinimumHeight(min(420, 80 + 30 * len(spec["rows"])))
                view.setToolTip(spec["source"])
                body.addWidget(view)
                layout.addWidget(panel)
        else:
            empty = label(tr("hardware_no_devices" if self.snapshot else self.status_key, self.window.language), "muted")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignCenter)
            layout.addWidget(empty, 1)
        layout.addStretch()
        self.scroll.setWidget(root)
        self.scroll.verticalScrollBar().setValue(position)

    def serialized(self):
        return json.dumps(public_snapshot(self.snapshot, self.serials.isChecked()), ensure_ascii=False, indent=2, allow_nan=False)

    def copy_snapshot(self):
        if self.snapshot:
            QApplication.clipboard().setText(self.serialized())

    def export_snapshot(self):
        if not self.snapshot:
            return
        destination, _ = QFileDialog.getSaveFileName(self, tr("export", self.window.language),
            str(Path(self.window.output.text()) / f"{self.kind}-parameters.json"), "JSON (*.json)")
        if destination:
            try:
                Path(destination).write_text(self.serialized() + "\n", encoding="utf-8")
            except (OSError, ValueError):
                self.status_key = "io_error"
                self.refresh_controls()
