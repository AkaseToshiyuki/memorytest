"""Signal-driven native UI: no webview, polling loop or benchmark work in Qt."""
import json
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QProcess, QStandardPaths, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget)
from benchmark_registry import ROOT, TESTS
from process_control import app_command
from desktop_results import ModulePage, STATUS, TITLES
from app_version import RELEASE_TAG

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"MemoryTest {RELEASE_TAG} — CPU 与内存测试")
        self.resize(920, 720)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.buffer = b""
        self.control = None
        self.closing = False
        self.report = None
        self.run_dir = None
        self.completed = set()
        self.active_modules = []
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        root = QWidget()
        self.tabs.addTab(root, "主页")
        layout = QVBoxLayout(root)
        title = QLabel("<h2>CPU 与内存测试</h2>选择模块 · 独立运行 · 本地报告")
        layout.addWidget(title)
        info = QLabel("测试时会占用 CPU / 内存；建议关闭其他重负载程序。只有 ComputeMark v2 提供固定工作负载评分。\n"
                      "其他模块属于诊断；不同调度策略、编译器和协议的结果不应混排行。")
        info.setWordWrap(True)
        layout.addWidget(info)
        grid = QGridLayout()
        self.modules = {}
        self.labels = {}
        for row, (name, spec) in enumerate(TESTS.items()):
            check = QCheckBox(spec["description_zh"])
            check.setChecked(True)
            # Known unsupported backends are visible, with no fabricated result.
            if name == "inter_core" and os.sys.platform in {"win32", "darwin"}:
                check.setChecked(False)
                check.setEnabled(False)
                check.setToolTip("当前系统后端未提供经过验证的核间矩阵")
            self.modules[name] = check
            self.labels[name] = QLabel("待运行" if check.isEnabled() else "当前平台不支持")
            grid.addWidget(check, row, 0)
            grid.addWidget(self.labels[name], row, 1)
        layout.addLayout(grid)
        options = QHBoxLayout()
        options.addWidget(QLabel("ComputeMark 线程："))
        self.threads = QSpinBox()
        self.threads.setRange(0, min(262144, os.cpu_count() or 1))
        self.threads.setSpecialValueText("全部可用")
        self.threads.setToolTip("仅控制 ComputeMark；其他诊断模块自行决定线程数")
        options.addWidget(self.threads)
        options.addStretch()
        layout.addLayout(options)
        output = QHBoxLayout()
        output.addWidget(QLabel("报告目录："))
        destination = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / "reports"
        self.output = QLineEdit(str(destination))
        self.browse = QPushButton("选择…")
        self.browse.clicked.connect(self.choose_directory)
        output.addWidget(self.output)
        output.addWidget(self.browse)
        layout.addLayout(output)
        controls = QHBoxLayout()
        self.start = QPushButton("运行所选")
        self.run_all = QPushButton("一键运行全部")
        self.run_all.clicked.connect(self.begin_all)
        self.cancel = QPushButton("取消")
        self.cancel.setEnabled(False)
        self.open_report = QPushButton("打开本次报告")
        self.open_report.setEnabled(False)
        self.open_folder = QPushButton("打开报告目录")
        self.licenses = QPushButton("许可")
        self.start.clicked.connect(lambda: self.begin())
        self.cancel.clicked.connect(self.request_cancel)
        self.open_report.clicked.connect(lambda: self.open_path(self.report))
        self.open_folder.clicked.connect(lambda: self.open_path(Path(self.output.text()).expanduser()))
        self.licenses.clicked.connect(lambda: self.open_path(ROOT / "licenses" if (ROOT / "licenses").exists() else ROOT / "LICENSE"))
        for button in [self.run_all, self.start, self.cancel, self.open_report, self.open_folder, self.licenses]:
            controls.addWidget(button)
        layout.addLayout(controls)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("已完成 %v / %m 个模块")
        layout.addWidget(self.progress)
        self.status = QLabel("就绪 · 空闲时不启动测试进程")
        layout.addWidget(self.status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        layout.addWidget(self.log, 1)
        self.pages = {}
        for name in TESTS:
            self.pages[name] = ModulePage(name, self)
            self.tabs.addTab(self.pages[name], TITLES[name])

    def open_path(self, path):
        if path and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.status.setText("无法打开；请从上方目录手动访问。")

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "选择报告目录", self.output.text())
        if path:
            self.output.setText(path)

    def set_running(self, active):
        self.start.setEnabled(not active)
        self.run_all.setEnabled(not active)
        self.cancel.setEnabled(active)
        self.output.setEnabled(not active)
        self.browse.setEnabled(not active)
        self.threads.setEnabled(not active)
        for name, check in self.modules.items():
            supported = not (name == "inter_core" and os.sys.platform in {"win32", "darwin"})
            check.setEnabled(not active and supported)
            self.pages[name].run.setEnabled(not active and supported)
            self.pages[name].cancel.setEnabled(active and name in self.active_modules)

    def begin_all(self):
        self.begin([name for name in TESTS if not (name == "inter_core" and os.sys.platform in {"win32", "darwin"})])

    def begin(self, modules=None):
        if self.process.state() != QProcess.NotRunning:
            return
        modules = modules if modules is not None else [name for name, check in self.modules.items() if check.isChecked()]
        if not modules:
            self.status.setText("请至少选择一个测试模块。")
            return
        try:
            output = Path(self.output.text()).expanduser().resolve()
            output.mkdir(parents=True, exist_ok=True)
            self.control = tempfile.TemporaryDirectory(prefix="memorytest-control-")
        except OSError as exc:
            self.status.setText(str(exc))
            return
        self.report = self.run_dir = None
        self.active_modules = list(modules)
        self.completed.clear()
        self.buffer = b""
        self.log.clear()
        self.open_report.setEnabled(False)
        self.progress.setRange(0, len(modules))
        self.progress.setFormat("已完成 %v / %m 个模块")
        self.progress.setValue(0)
        self.output.setText(str(output))
        for name in modules:
            self.labels[name].setText("排队中")
            self.pages[name].details.clear()
            self.pages[name].summary.setText("排队中")
            self.pages[name].progress.setRange(0, 1)
            self.pages[name].progress.setValue(0)
            self.pages[name].html = None
            self.pages[name].open.setEnabled(False)
        command = app_command("worker") + ["--skip-build", "--skip-charts", "--events-jsonl",
            "--output-dir", str(output), "--bin-dir", str(ROOT / "bin"),
            "--cancel-file", str(Path(self.control.name) / "cancel"), "--parent-pid", str(os.getpid())]
        for name in modules:
            command.extend(["--module", name])
        if self.threads.value():
            command.extend(["--cpu-threads", str(self.threads.value())])
        self.set_running(True)
        self.status.setText("运行中；详细输出写入各模块日志。")
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
                    self.log.appendPlainText("无法解析进度事件")
            else:
                self.log.appendPlainText(text)
        if len(self.buffer) > 65536:
            self.log.appendPlainText(self.buffer[:8192].decode("utf-8", errors="replace"))
            self.buffer = b""

    def handle_event(self, data):
        if data["type"] == "run":
            self.run_dir = Path(data["path"])
        elif data["type"] == "module":
            name, status = data["module"], data["status"]
            self.labels[name].setText(STATUS.get(status, status))
            if status == "running":
                self.pages[name].started()
                self.status.setText(f"正在测试：{TITLES[name]} · 已完成 {len(self.completed)} / {len(self.active_modules)}")
            else:
                self.completed.add(name)
                self.progress.setValue(len(self.completed))
                if self.run_dir:
                    self.pages[name].completed(self.run_dir, data)
        elif data["type"] == "finished":
            self.report = Path(data["report"])
            self.open_report.setEnabled(self.report.is_file())
            for name in self.active_modules:
                self.pages[name].html = self.report
                self.pages[name].open.setEnabled(self.report.is_file())

    def request_cancel(self):
        if self.control:
            Path(self.control.name, "cancel").touch()
            self.cancel.setEnabled(False)
            for page in self.pages.values():
                page.cancel.setEnabled(False)
            self.status.setText("正在停止测试进程并保存取消报告…")

    def process_error(self, error):
        if error == QProcess.FailedToStart:
            self.log.appendPlainText(self.process.errorString())
            self.finished(-1)

    def finished(self, code, *_):
        self.read_output()
        for name in self.active_modules:
            if name not in self.completed:
                self.pages[name].progress.setRange(0, 1)
                self.pages[name].progress.setValue(0)
                self.pages[name].summary.setText("任务未完成；工作进程异常退出，请查看主页日志。")
                self.labels[name].setText("未完成")
        self.set_running(False)
        self.status.setText("完成" if code == 0 else "已取消，部分数据已排除" if code == 130 else
                            f"任务结束（代码 {code}）；请查看日志与报告。")
        if self.control:
            self.control.cleanup()
            self.control = None
        if self.closing:
            self.close()

    def closeEvent(self, event):
        if self.process.state() != QProcess.NotRunning:
            self.closing = True
            self.request_cancel()
            event.ignore()  # asynchronous cleanup; never block the UI thread
        else:
            event.accept()
