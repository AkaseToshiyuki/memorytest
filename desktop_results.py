"""Native result cards, charts and tables backed by the same model as HTML."""
import json
import sys
from PySide6.QtCore import Qt, QAbstractTableModel, QSize
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (QLabel, QHBoxLayout, QVBoxLayout, QGridLayout, QProgressBar,
    QPushButton, QScrollArea, QTableView, QHeaderView, QWidget, QTabWidget, QSizePolicy)
from desktop_theme import card, label, set_tone
from i18n import tr, display
from presentation import plot_svg
from result_model import ModuleResult, build_module, perflevel_table, cache_parts

TITLES = {name: tr(name, "zh") for name in
          ("cache_hierarchy", "memory_bandwidth", "inter_core", "cpu_alu", "cpu_float", "cpu_branch", "cpu_multi")}
STATUS = {key: tr(key, "zh") for key in ("running", "passed", "failed", "unsupported", "interrupted", "skipped", "timeout", "build_failed", "invalid_report")}


def supported(name):
    return not (name == "inter_core" and sys.platform in {"darwin", "win32"})


class ResultsTableModel(QAbstractTableModel):
    def __init__(self, table, language, parent=None):
        super().__init__(parent)
        self.table, self.language = table, language

    def rowCount(self, parent=None):
        return len(self.table.rows)

    def columnCount(self, parent=None):
        return len(self.table.columns)

    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and role in (Qt.DisplayRole, Qt.ToolTipRole):
            return display(self.table.rows[index.row()][index.column()], self.language)
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignLeft | Qt.AlignVCenter)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Vertical:
            return str(section + 1)
        col = self.table.columns[section]
        return display(col["label"], self.language) + (" (" + col["unit"] + ")" if col["unit"] else "")


class SvgPlot(QSvgWidget):
    def __init__(self, chart, language, theme, parent=None):
        super().__init__(parent)
        self.load(plot_svg(chart, language, theme).encode("utf-8"))
        self.renderer().setAspectRatioMode(Qt.KeepAspectRatio)
        self.setMinimumHeight(300 if chart.kind == "heatmap" else max(220, len(chart.labels) * 28 if chart.kind == "bar" else 220))
        self.setMaximumHeight(760)
        self.setAccessibleName(tr(chart.title, language))

    def sizeHint(self):
        return QSize(720, self.minimumHeight())

    def resizeEvent(self, event):
        size = self.renderer().defaultSize()
        if size.width():
            self.setMinimumHeight(min(760, max(220, round(self.width() * size.height() / size.width()))))
        super().resizeEvent(event)


class ResultView(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.model = None
        self.language, self.theme = "en", "dark"

    def render(self, model, language, theme):
        position = self.verticalScrollBar().value()
        self.model, self.language, self.theme = model, language, theme
        previous = self.takeWidget()
        if previous:
            previous.deleteLater()
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        if model.status != "passed":
            empty = label(tr(model.reason or "no_data", language), "muted")
            empty.setObjectName("empty-result")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignCenter)
            layout.addWidget(empty, 1)
        else:
            if model.notice:
                layout.addWidget(label(tr(model.notice, language), "muted"))
            if model.kpis:
                grid = QGridLayout()
                for i, item in enumerate(model.kpis):
                    widget, content = card()
                    heading = label(display(item["label"], language), "muted")
                    heading.setWordWrap(True)
                    content.addWidget(heading)
                    content.addWidget(label(display(item["value"], language), "metric"))
                    unit = tr("points", language) if item["unit"] == "points" else item["unit"]
                    content.addWidget(label(unit + " " + item.get("detail", ""), "muted"))
                    grid.addWidget(widget, i // 4, i % 4)
                layout.addLayout(grid)
            if model.conditions:
                conditions = label(" · ".join(display(k, language) + ": " + display(v, language) for k, v in model.conditions), "muted")
                conditions.setWordWrap(True)
                conditions.setTextFormat(Qt.PlainText)
                layout.addWidget(conditions)
            for chart in model.charts:
                widget, content = card()
                content.addWidget(label(tr(chart.title, language), "section"))
                content.addWidget(SvgPlot(chart, language, theme))
                layout.addWidget(widget)
            for table in model.tables:
                widget, content = card()
                content.addWidget(label(tr(table.title, language), "section"))
                view = QTableView()
                view.setModel(ResultsTableModel(table, language, view))
                view.setAccessibleName(tr(table.title, language))
                view.setAlternatingRowColors(True)
                view.verticalHeader().hide()
                view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
                view.horizontalHeader().setStretchLastSection(True)
                view.setMinimumHeight(min(360, 75 + 32 * len(table.rows)))
                content.addWidget(view)
                layout.addWidget(widget)
            layout.addStretch()
        self.setWidget(root)
        self.verticalScrollBar().setValue(position)

    def toPlainText(self):
        # Useful to accessibility/acceptance checks; includes only localized display data.
        if self.model is None:
            return ""
        m, lang = self.model, self.language
        values = [tr(m.name, lang), tr(m.status, lang)]
        values += [display(k["label"], lang) + " " + display(k["value"], lang) for k in m.kpis]
        for table in m.tables:
            values.extend(display(column["label"], lang) for column in table.columns)
            values.extend(display(value, lang) for row in table.rows for value in row)
        return "\n".join(values)


class CacheResultView(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDocumentMode(True)
        self.model = None
        for _ in range(3):
            self.addTab(ResultView(), "")

    def render(self, model, language, theme):
        self.model = model
        for index, part in enumerate(cache_parts(model)):
            self.setTabText(index, tr(part.name, language))
            self.widget(index).render(part, language, theme)

    def toPlainText(self):
        return "\n".join(self.widget(i).toPlainText() for i in range(self.count()))


class ModulePage(QWidget):
    def __init__(self, name, window):
        super().__init__()
        self.name, self.window = name, window
        self.html = self.log_path = None
        self.saved_run_id = None
        self.model = ModuleResult(name, "not_run" if supported(name) else "unsupported")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 16, 0, 0)
        self.title = label("", "title")
        layout.addWidget(self.title)
        controls = QHBoxLayout()
        self.run, self.cancel, self.open, self.log_button = (QPushButton() for _ in range(4))
        self.run.setProperty("role", "primary")
        self.run.clicked.connect(lambda: window.begin([name]))
        self.cancel.clicked.connect(window.request_cancel)
        self.open.clicked.connect(lambda: window.open_path(self.html))
        self.log_button.clicked.connect(lambda: window.open_path(self.log_path))
        self.run.setEnabled(supported(name))
        for button in (self.cancel, self.open, self.log_button):
            button.setEnabled(False)
        for button in (self.run, self.cancel, self.open, self.log_button):
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            controls.addWidget(button, 1)
        layout.addLayout(controls)
        self.summary = label("", "status")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.summary)
        layout.addWidget(self.progress)
        self.details = CacheResultView() if name == "cache_hierarchy" else ResultView()
        layout.addWidget(self.details, 1)
        self.retranslate()

    def retranslate(self):
        lang = self.window.language
        self.title.setText(tr(self.name, lang))
        for button, key in ((self.run, "run"), (self.cancel, "cancel"), (self.open, "open_report"), (self.log_button, "open_log")):
            button.setText(tr(key, lang))
        self.summary.setText(tr(self.model.status, lang) +
            (f" · {self.model.duration:.3f} s" if self.model.duration is not None else "") +
            (" · " + self.saved_run_id if self.saved_run_id else ""))
        set_tone(self.summary, self.model.status)
        self.details.render(self.model, lang, self.window.theme)

    def queued(self):
        self.html = self.log_path = None
        self.saved_run_id = None
        self.open.setEnabled(False)
        self.log_button.setEnabled(False)
        self.model = ModuleResult(self.name, "queued")
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.retranslate()

    def started(self):
        self.model = ModuleResult(self.name, "running")
        self.progress.setRange(0, 0)
        self.retranslate()

    def completed(self, run_dir, data, model=None):
        self.log_path = run_dir / data.get("log", "")
        self.log_button.setEnabled(self.log_path.is_file())
        self.model = model if model is not None else build_module(self.name, run_dir / "reports", data)
        if self.name in {"cache_hierarchy", "cache_detection"} and self.model.status == "passed":
            try:
                levels = perflevel_table(json.loads((run_dir / "manifest.json").read_text()))
                if levels.rows:
                    self.model.tables.insert(0, levels)
            except (OSError, ValueError):
                pass
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if self.model.status == "passed" else 0)
        self.retranslate()
