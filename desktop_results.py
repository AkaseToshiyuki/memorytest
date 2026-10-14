"""Native per-module results; Qt's text renderer, not a browser engine."""
import json
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextTable
from PySide6.QtWidgets import QLabel, QHBoxLayout, QProgressBar, QPushButton, QTextBrowser, QVBoxLayout, QWidget
from benchmark_registry import TESTS
from desktop_theme import card, label, set_tone

TITLES = {"cache_hierarchy": "缓存层级", "memory_bandwidth": "内存带宽", "inter_core": "核间通信",
          "cpu_alu": "整数运算", "cpu_float": "浮点运算", "cpu_branch": "分支预测", "cpu_multi": "ComputeMark"}
STATUS = {"running": "运行中", "passed": "完成", "failed": "失败", "unsupported": "不支持",
          "interrupted": "已取消", "skipped": "已跳过", "timeout": "超时",
          "build_failed": "构建失败", "invalid_report": "报告无效"}


class ModulePage(QWidget):
    def __init__(self, name, window):
        super().__init__()
        self.name = name
        self.html = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 20, 0, 0)
        layout.setSpacing(14)
        heading = QHBoxLayout()
        title = label(TITLES[name], "title")
        title.setToolTip(TESTS[name]["description_zh"])
        heading.addWidget(title)
        heading.addStretch()
        layout.addLayout(heading)
        controls = QHBoxLayout()
        self.run = QPushButton("运行测试")
        self.run.setProperty("role", "primary")
        self.run.clicked.connect(lambda: window.begin([name]))
        self.cancel = QPushButton("取消")
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(window.request_cancel)
        self.open = QPushButton("查看报告")
        self.open.setEnabled(False)
        self.open.clicked.connect(lambda: window.open_path(self.html))
        for button in [self.run, self.cancel, self.open]:
            controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)
        summary_card, summary_layout = card()
        summary_layout.addWidget(label("测试摘要", "section"))
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        summary_layout.addWidget(self.progress)
        self.summary = QLabel("尚未运行")
        self.summary.setObjectName("resultSummary")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        summary_layout.addWidget(self.summary)
        if name == "cpu_multi":
            limitation = "固定标量评分 · 仅比较相同协议、编译器、线程数与调度策略的结果。"
        else:
            limitation = "诊断指标，不计综合分。"
        self.limitation = label(limitation, "muted")
        self.limitation.setWordWrap(True)
        summary_layout.addWidget(self.limitation)
        layout.addWidget(summary_card)
        detail_heading = QHBoxLayout()
        detail_heading.addWidget(label("详细结果", "section"))
        detail_heading.addStretch()
        layout.addLayout(detail_heading)
        self.details = QTextBrowser()
        self.details.setAccessibleName(f"{TITLES[name]}详细测试结果")
        self.details.setOpenLinks(False)
        self.details.setOpenExternalLinks(False)
        self.details.document().setDocumentMargin(18)
        self.details.setPlaceholderText("暂无结果")
        layout.addWidget(self.details, 1)
        if name == "inter_core" and sys.platform in {"win32", "darwin"}:
            self.run.setEnabled(False)
            self.summary.setText("当前系统后端尚无经过验证的核间延迟矩阵，不支持运行。")

    def queued(self):
        self.html = None
        self.open.setEnabled(False)
        self.details.clear()
        self.summary.setText("排队中 · 等待本次测试")
        set_tone(self.summary, "queued")
        self.progress.setRange(0, 1)
        self.progress.setValue(0)

    def started(self):
        self.html = None
        self.open.setEnabled(False)
        self.details.clear()
        self.summary.setText("运行中；本次完成前不显示上次结果。")
        set_tone(self.summary, "running")
        self.progress.setRange(0, 0)  # engine supplies phases, not trustworthy ETA

    def completed(self, run_dir, data):
        status = data["status"]
        set_tone(self.summary, status)
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if status == "passed" else 0)
        self.summary.setText(f"{STATUS.get(status, status)} · 运行 {run_dir.name}\n{data.get('message', '')}")
        if status != "passed":
            self.details.setPlainText("本次没有可用的完整结果。失败、取消或超时的部分数据不会生成评分。\n"
                                      f"日志：{run_dir / data.get('log', '')}")
            return
        path = run_dir / "reports" / TESTS[self.name]["report"]
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                markdown = stream.read(5_000_001)
            if len(markdown) > 5_000_000:
                markdown = markdown[:5_000_000] + "\n\n界面显示已截断；完整数据请打开该次报告。"
            self.details.setMarkdown(markdown)
            # Style real QTextTables directly: Markdown parsing does not apply
            # every HTML/CSS rule. Preserve native selection and copying.
            def style_tables(frame):
                for child in frame.childFrames():
                    if isinstance(child, QTextTable):
                        fmt = child.format()
                        fmt.setBorder(1)
                        fmt.setBorderBrush(QColor("#3b4e66"))
                        fmt.setCellPadding(8)
                        fmt.setCellSpacing(0)
                        child.setFormat(fmt)
                    style_tables(child)
            style_tables(self.details.document().rootFrame())
            if self.name == "cpu_multi":
                from compute_score import compute_mark_score
                score = compute_mark_score(json.loads((path.parent / "compute_mark_v2.json").read_text(encoding="utf-8")))
                self.summary.setText(f"单线程：{score.single_points:,} 分　多线程：{score.points:,} 分（{score.threads} 线程）\n"
                                     f"整数 {score.integer_gops:.3f} Gop/s · FP64 {score.fp64_gflops:.3f} GFLOP/s\n"
                                     f"调度策略：{score.placement_policy} · 波动判定：{'稳定' if score.stable else '偏大，请复测'}\n"
                                     f"运行：{run_dir.name}")
        except (OSError, ValueError, KeyError) as exc:
            self.summary.setText(f"结果读取或评分校验失败：{exc}")
            set_tone(self.summary, "invalid_report")
