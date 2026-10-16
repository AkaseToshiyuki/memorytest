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
        self.window = MainWindow(autodetect=False)
        self.window.language = "en"
        self.window.theme = "dark"
        self.window.retranslate()
        self.addCleanup(self.window.close)

    def drain_until(self, predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(predicate())

    def test_idle_has_no_worker_and_bounded_log(self):
        self.assertEqual(self.window.tabs.count(), 6)
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
        from PySide6.QtGui import QFontDatabase
        self.assertTrue(QFontDatabase.families(), "Qt has no fonts; configure QT_QPA_FONTDIR for offscreen Windows")
        self.window.module_status["inter_core"] = "unsupported"
        self.window.retranslate()
        self.window.resize(880, 720)
        self.window.show()
        self.app.processEvents()
        from PySide6.QtCore import QPoint
        for widget in (self.window.run_all, self.window.cancel, self.window.output,
                       self.window.progress, self.window.licenses):
            bottom_right = widget.mapTo(self.window, widget.rect().bottomRight())
            if not self.window.rect().contains(bottom_right):
                evidence = ROOT / "build/ui-failure"
                evidence.mkdir(parents=True, exist_ok=True)
                self.window.grab().save(str(evidence / "home.png"))
            self.assertTrue(self.window.rect().contains(bottom_right),
                            f"{widget.text() if hasattr(widget, 'text') else widget}: {bottom_right}, window={self.window.size()}")
            self.assertTrue(self.window.rect().contains(widget.mapTo(self.window, QPoint(0, 0))))

    def test_single_tab_runs_only_its_module(self):
        with patch.object(self.window, "begin") as begin:
            self.window.pages["cpu_alu"].run.click()
        begin.assert_called_once_with(["cpu_alu"])

    def test_architecture_is_independent_top_level_task(self):
        from benchmark_registry import SELECTION_GROUPS
        from result_model import GROUPS as REPORT_GROUPS
        from desktop_results import supported
        page = self.window.pages["cpu_architecture"]
        core = self.window.group_pages["core_group"]
        self.assertGreater(self.window.tabs.indexOf(core), 0)
        self.assertEqual(self.window.tabs.indexOf(page), self.window.tabs.indexOf(core) + 1)
        self.assertEqual(self.window.group_tabs["core_group"].indexOf(page), -1)
        self.assertTrue(self.window.tabs.tabBar().isTabVisible(self.window.tabs.indexOf(page)))
        self.assertFalse(self.window.tabs.tabBar().isTabVisible(self.window.tabs.indexOf(self.window.tools_page)))
        self.window.tabs.setCurrentWidget(page)
        self.window.retranslate()
        self.assertIs(self.window.tabs.currentWidget(), page)
        self.assertFalse(self.window.tools_button.isChecked())
        self.assertNotIn("cpu_architecture", SELECTION_GROUPS["core_group"])
        self.assertNotIn("cpu_architecture", REPORT_GROUPS["core_group"])
        self.assertEqual(REPORT_GROUPS["architecture_group"], ["cpu_architecture"])
        core_button = self.window.group_buttons[list(SELECTION_GROUPS).index("core_group")]
        with patch.object(self.window, "begin") as begin:
            core_button.click()
        begin.assert_called_once_with([name for name in SELECTION_GROUPS["core_group"] if supported(name)])
        with patch.object(self.window, "begin") as begin:
            page.run.click()
        begin.assert_called_once_with(["cpu_architecture"])

    def test_all_module_actions_are_equal_width_and_fill_the_row(self):
        self.window.show()
        for language in ("zh", "en"):
            self.window.language = language
            self.window.retranslate()
            for width in (880, 1400):
                self.window.resize(width, 850)
                for group, container in self.window.group_pages.items():
                    self.window.tabs.setCurrentWidget(container)
                    tabs = self.window.group_tabs.get(group)
                    pages = [tabs.widget(child) for child in range(tabs.count())] if tabs else [container]
                    for page in pages:
                        if tabs:
                            tabs.setCurrentWidget(page)
                        self.app.processEvents()
                        buttons = (page.run, page.cancel, page.open, page.log_button)
                        widths = [button.width() for button in buttons]
                        self.assertLessEqual(max(widths) - min(widths), 1, page.name)
                        self.assertEqual(buttons[0].x(), 0, page.name)
                        self.assertLessEqual(abs(buttons[-1].geometry().right() + 1 - page.width()), 1, page.name)

    def test_status_colors_reset_on_requeue_and_follow_actual_state(self):
        from PySide6.QtGui import QPalette
        from desktop_theme import apply_theme, set_tone
        from presentation import THEMES
        name = "cpu_float"
        widget = self.window.labels[name]
        for theme in ("dark", "light"):
            self.window.theme = theme
            apply_theme(theme)
            self.window.module_status[name] = "passed"
            set_tone(widget, "passed")
            with patch.object(self.window.process, "start"):
                self.window.begin([name])
            self.assertEqual(widget.property("tone"), "queued")
            widget.ensurePolished()
            self.assertEqual(widget.palette().color(QPalette.WindowText).name(), THEMES[theme]["muted"])
            for status, color in (("running", "success"), ("failed", "error"),
                                  ("unsupported", "warning"), ("queued", "muted")):
                self.window.module_status[name] = status
                self.window.retranslate()
                self.app.processEvents()
                self.assertEqual(widget.property("tone"), status)
                self.assertEqual(widget.palette().color(QPalette.WindowText).name(), THEMES[theme][color])
            self.window.set_running(False)
            self.window.control.cleanup()
            self.window.control = None

    def test_only_running_status_flashes_and_stops_on_completion(self):
        from PySide6.QtGui import QPalette
        from presentation import THEMES
        name = "cpu_alu"
        self.window.handle_event({"type": "module", "module": name, "status": "running"})
        widget = self.window.labels[name]
        self.assertTrue(self.window.status_timer.isActive())
        self.assertEqual(self.window.status_timer.interval(), 600)
        self.assertEqual(widget.palette().color(QPalette.WindowText).name(), THEMES[self.window.theme]["success"])
        self.window.pulse_status()
        self.assertEqual(widget.palette().color(QPalette.WindowText).name(), THEMES[self.window.theme]["muted"])
        self.assertEqual(self.window.pages[name].summary.property("pulse"), "off")
        self.window.retranslate()
        self.assertEqual(widget.property("pulse"), "off")
        self.window.pulse_status()
        self.assertEqual(widget.palette().color(QPalette.WindowText).name(), THEMES[self.window.theme]["success"])
        self.window.handle_event({"type": "module", "module": name, "status": "failed"})
        self.assertFalse(self.window.status_timer.isActive())
        self.assertEqual(widget.property("pulse"), "steady")
        self.assertEqual(widget.palette().color(QPalette.WindowText).name(), THEMES[self.window.theme]["error"])

    def test_cache_and_memory_selection_are_real_independent_tasks(self):
        from benchmark_registry import SUBTESTS
        self.assertNotIn("cache_hierarchy", self.window.modules)
        self.assertNotIn("memory_bandwidth", self.window.modules)
        self.assertEqual(self.window.group_tabs["cache_group"].count(), 3)
        self.assertEqual(self.window.group_tabs["memory_group"].count(), 2)
        for name in SUBTESTS:
            self.assertIn(name, self.window.modules)
            with patch.object(self.window, "begin") as begin:
                self.window.pages[name].run.click()
            begin.assert_called_once_with([name])

    def test_home_runs_all_supported_regardless_of_checkboxes(self):
        for check in self.window.modules.values():
            check.setChecked(False)
        with patch.object(self.window, "begin") as begin:
            self.window.run_all.click()
        selected = begin.call_args.args[0]
        from desktop_results import supported
        expected = {name for name in self.window.modules if supported(name)}
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
            self.assertIn("33,941", page.details.toPlainText())
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
        self.assertEqual(self.window.labels["cpu_multi"].text(), "Completed")

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
