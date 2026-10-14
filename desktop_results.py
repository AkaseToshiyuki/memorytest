"""Native per-module results; Qt's text renderer, not a browser engine."""
import json
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QHBoxLayout, QProgressBar, QPushButton, QTextBrowser, QVBoxLayout, QWidget
from benchmark_registry import TESTS

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
        layout.addWidget(QLabel(f"<h2>{TITLES[name]}</h2>"))
        description = QLabel(TESTS[name]["description_zh"])
        description.setWordWrap(True)
        layout.addWidget(description)
        controls = QHBoxLayout()
        self.run = QPushButton("单独运行此模块")
        self.run.clicked.connect(lambda: window.begin([name]))
        self.cancel = QPushButton("取消当前任务")
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(window.request_cancel)
        self.open = QPushButton("在浏览器打开该次报告")
        self.open.setEnabled(False)
        self.open.clicked.connect(lambda: window.open_path(self.html))
        for button in [self.run, self.cancel, self.open]:
            controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.summary = QLabel("尚未运行")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        layout.addWidget(self.summary)
        if name == "cpu_multi":
            limitation = "评分：固定标量工作负载；比较时须匹配协议、编译器、线程数和调度策略。线程上限在主页设置。"
        else:
            limitation = "本模块为诊断测试，暂无经过验证的跨平台综合评分；以下展示实际测得的各项指标，不生成伪评分。"
        self.limitation = QLabel(limitation)
        self.limitation.setWordWrap(True)
        layout.addWidget(self.limitation)
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.setOpenExternalLinks(False)
        layout.addWidget(self.details, 1)
        if name == "inter_core" and sys.platform in {"win32", "darwin"}:
            self.run.setEnabled(False)
            self.summary.setText("当前系统后端尚无经过验证的核间延迟矩阵，不支持运行。")

    def started(self):
        self.html = None
        self.open.setEnabled(False)
        self.details.clear()
        self.summary.setText("运行中；本次完成前不显示上次结果。")
        self.progress.setRange(0, 0)  # engine supplies phases, not trustworthy ETA

    def completed(self, run_dir, data):
        status = data["status"]
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
            if self.name == "cpu_multi":
                from compute_score import compute_mark_score
                score = compute_mark_score(json.loads((path.parent / "compute_mark_v2.json").read_text(encoding="utf-8")))
                self.summary.setText(f"单线程：{score.single_points:,} 分　多线程：{score.points:,} 分（{score.threads} 线程）\n"
                                     f"整数 {score.integer_gops:.3f} Gop/s · FP64 {score.fp64_gflops:.3f} GFLOP/s\n"
                                     f"调度策略：{score.placement_policy} · 波动判定：{'稳定' if score.stable else '偏大，请复测'}\n"
                                     f"运行：{run_dir.name}")
        except (OSError, ValueError, KeyError) as exc:
            self.summary.setText(f"结果读取或评分校验失败：{exc}")
