"""Desktop lifecycle tests. Fixtures only; no hardware benchmarks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from benchmark_registry import ROOT
from process_control import Cancellation
from run_benchmarks import run_command, run_suite


class ProcessTests(unittest.TestCase):
    def test_cancel_stops_process_tree(self):
        with tempfile.TemporaryDirectory(prefix="memorytest-cancel-") as directory:
            root = Path(directory)
            script = ("import subprocess,sys,time,pathlib; "
                      "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                      "pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(60)")
            def cancelled():
                return (root / "child.pid").exists()
            before = time.monotonic()
            with self.assertRaises(KeyboardInterrupt):
                run_command([sys.executable, "-c", script], root, root / "log", 10, cancelled)
            self.assertLess(time.monotonic() - before, 8)
            import psutil
            pid = int((root / "child.pid").read_text())
            # A killed adopted POSIX child may remain a zombie until init reaps it.
            def alive():
                try:
                    return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
                except psutil.NoSuchProcess:
                    return False
            self.assertFalse(alive(), f"descendant {pid} still running")

    def test_dead_parent_cancels_worker(self):
        with subprocess.Popen([sys.executable, "-c", "pass"]) as parent:
            cancel = Cancellation(parent_pid=parent.pid)
            parent.wait()
            self.assertTrue(cancel())

    def test_precancelled_run_reports_without_executing(self):
        with tempfile.TemporaryDirectory(prefix="memorytest-precancel-") as directory:
            root = Path(directory)
            events = []
            code = run_suite(["cpu_multi", "cpu_alu"], root, root / "no-binaries", True,
                             skip_charts=True, cancelled=lambda: True,
                             event=lambda **data: events.append(data))
            self.assertEqual(code, 130)
            manifest = json.loads(next(root.glob("runs/*/manifest.json")).read_text())
            self.assertEqual([item["status"] for item in manifest["modules"]], ["interrupted", "skipped"])
            self.assertTrue(Path(events[-1]["report"]).is_file())
            self.assertFalse(manifest["success"])


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QCoreApplication, QProcess
    from PySide6.QtWidgets import QApplication
    from desktop_window import MainWindow
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "install packaging/requirements-desktop.txt")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QCoreApplication.setApplicationName("MemoryTestTest")

    def setUp(self):
        self.window = MainWindow()
        self.addCleanup(self.window.close)

    def drain_until(self, predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(predicate())

    def test_idle_has_no_worker_and_bounded_log(self):
        self.assertEqual(self.window.tabs.count(), 8)
        self.assertEqual(self.window.process.state(), QProcess.NotRunning)
        self.assertFalse(self.window.cancel.isEnabled())
        for index in range(600):
            self.window.log.appendPlainText(str(index))
        self.assertLessEqual(self.window.log.document().blockCount(), 500)

    def test_default_theme_and_report_are_dark(self):
        from PySide6.QtGui import QPalette
        from desktop_theme import BACKGROUND, TEXT
        self.assertTrue(self.app.property("memorytestDarkTheme"))
        self.assertEqual(self.app.palette().color(QPalette.Window).name(), BACKGROUND)
        self.assertEqual(self.app.palette().color(QPalette.Text).name(), TEXT)
        self.assertLess(self.window.pages["cpu_multi"].details.palette().color(QPalette.Base).lightness(), 50)
        self.assertEqual(self.window.run_all.property("role"), "primary")
        from PySide6.QtWidgets import QLabel
        from app_version import RELEASE_TAG
        labels = self.window.findChildren(QLabel)
        self.assertFalse(any(item.text() == "MT" for item in labels))
        self.assertEqual([item for item in labels if item.text() == RELEASE_TAG], [self.window.version])

    def test_compact_layout_keeps_primary_controls_visible(self):
        self.window.resize(880, 720)
        self.window.show()
        self.app.processEvents()
        from PySide6.QtCore import QPoint
        for widget in (self.window.run_all, self.window.cancel, self.window.output,
                       self.window.progress, self.window.licenses):
            bottom_right = widget.mapTo(self.window, widget.rect().bottomRight())
            self.assertTrue(self.window.rect().contains(bottom_right), widget.text() if hasattr(widget, "text") else str(widget))
            self.assertTrue(self.window.rect().contains(widget.mapTo(self.window, QPoint(0, 0))))

    def test_single_tab_runs_only_its_module(self):
        with patch.object(self.window, "begin") as begin:
            self.window.pages["cpu_alu"].run.click()
        begin.assert_called_once_with(["cpu_alu"])

    def test_home_runs_all_supported_regardless_of_checkboxes(self):
        for check in self.window.modules.values():
            check.setChecked(False)
        with patch.object(self.window, "begin") as begin:
            self.window.run_all.click()
        selected = begin.call_args.args[0]
        expected = set(self.window.modules) - ({"inter_core"} if sys.platform in {"darwin", "win32"} else set())
        self.assertEqual(set(selected), expected)

    def test_tab_displays_tables_and_validated_cpu_points(self):
        from test_compute import fixture
        with tempfile.TemporaryDirectory(prefix="memorytest-tab-") as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "reports/cpu_multi_core_report.md").write_text(
                "# ComputeMark\n\n| Metric | Value |\n|---|---|\n| Integer | 24 |\n", encoding="utf-8")
            (root / "reports/compute_mark_v2.json").write_text(json.dumps(fixture()), encoding="utf-8")
            page = self.window.pages["cpu_multi"]
            page.started()
            self.assertEqual(page.progress.maximum(), 0)
            page.completed(root, {"status": "passed"})
            self.assertIn("33,941", page.summary.text())
            self.assertIn("Integer", page.details.toPlainText())
            self.assertEqual(page.progress.value(), 1)
            self.assertEqual(page.summary.property("tone"), "passed")
            page.queued()
            self.assertNotIn("33,941", page.summary.text())
            self.assertNotIn("Integer", page.details.toPlainText())
            # A failed rerun must not keep old points or old result tables visible.
            page.started()
            page.completed(root, {"status": "interrupted"})
            self.assertNotIn("33,941", page.summary.text())
            self.assertNotIn("Integer", page.details.toPlainText())

    def test_empty_selection_does_not_start(self):
        for check in self.window.modules.values():
            check.setChecked(False)
        self.window.begin()
        self.assertEqual(self.window.process.state(), QProcess.NotRunning)
        self.assertIsNone(self.window.control)

    def test_events_track_modules_without_claiming_time_percentage(self):
        self.window.progress.setMaximum(2)
        self.window.handle_event({"type": "module", "module": "cpu_multi", "status": "passed"})
        self.window.handle_event({"type": "module", "module": "cpu_multi", "status": "passed"})
        self.assertEqual(self.window.progress.value(), 1)
        self.assertEqual(self.window.labels["cpu_multi"].text(), "完成")

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture; Windows lifecycle uses native subprocess test")
    def test_qprocess_cancel_and_close_leave_no_worker(self):
        with tempfile.TemporaryDirectory(prefix="memorytest-window-") as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            fixture = root / "bin/test_cpu_multi"
            fixture.write_text(f"#!{sys.executable}\nimport pathlib,os,time\npathlib.Path('pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
            fixture.chmod(0o755)
            for name, check in self.window.modules.items():
                check.setChecked(name == "cpu_multi")
            self.window.output.setText(str(root / "reports"))
            with patch("desktop_window.ROOT", root):
                self.window.begin()
            self.drain_until(lambda: bool(list(root.glob("reports/runs/*/cpu_multi/pid"))))
            worker_pid = self.window.process.processId()
            pid_path = next(root.glob("reports/runs/*/cpu_multi/pid"))
            benchmark_pid = int(pid_path.read_text())
            self.window.close()
            self.drain_until(lambda: self.window.process.state() == QProcess.NotRunning)
            import psutil
            self.assertFalse(psutil.pid_exists(worker_pid))
            self.assertFalse(psutil.pid_exists(benchmark_pid))
            self.assertIsNone(self.window.control)
            self.assertTrue(self.window.report.is_file())
            manifest = json.loads((self.window.report.parent / "manifest.json").read_text())
            self.assertEqual(manifest["modules"][0]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
