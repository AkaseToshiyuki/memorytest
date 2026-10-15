"""Archive lifecycle, deletion boundaries and desktop restoration (fixtures only)."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from result_storage import (OWNER, active_writer, application_directory, archive_identity, archives,
                            local_file, prune_archives, recent_archives, retention_lock,
                            sample_tail, writable_output)


def make_archive(root, number, kind="benchmark", owned=True):
    directory = root / ("runs" if kind == "benchmark" else "stress-runs") / f"20261015-093100-{number:08d}"
    directory.mkdir(parents=True)
    data = {"schema_version": 1, "run_id": directory.name, "kind": kind,
            "started_at": f"2026-10-15T09:{number:02d}:00+08:00",
            "finished_at": f"2026-10-15T09:{number:02d}:30+08:00",
            "modules": [{"module": "cache_hierarchy", "status": "passed", "log": "output.log"}],
            "selected_modules": ["cache_hierarchy"], "html_report": "report.html",
            "status": "stopped", "catalog": {}, "statistics": {}, "elapsed": 1}
    if owned:
        data["application"] = OWNER
    (directory / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    (directory / "report.html").write_text("fixture", encoding="utf-8")
    return directory, data


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memorytest-storage-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()  # macOS /var aliases /private/var.

    def test_application_directory_avoids_bundle_resources_and_working_directory(self):
        self.assertEqual(application_directory(self.root / "MemoryTest.app/Contents/MacOS/MemoryTest", True), self.root.resolve())
        self.assertEqual(application_directory(self.root / "portable/MemoryTest.exe", True), (self.root / "portable").resolve())
        from benchmark_registry import ROOT
        self.assertEqual(application_directory("/not-the-python-installation", False), ROOT)

    def test_automatic_output_and_readonly_fallback(self):
        first, second = self.root / "app/Outputs", self.root / "user/Outputs"
        self.assertEqual(writable_output(first, second), (first.resolve(), False))
        with patch("result_storage.tempfile.TemporaryFile", side_effect=[PermissionError("readonly"), tempfile.TemporaryFile()]):
            self.assertEqual(writable_output(first, second), (second.resolve(), True))

    def test_both_unwritable_raises_without_claiming_saved(self):
        with patch("result_storage.tempfile.TemporaryFile", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                writable_output(self.root / "a", self.root / "b")

    def test_ten_rounds_shared_between_benchmark_and_stress(self):
        paths = [make_archive(self.root, n, "stress" if n % 2 else "benchmark")[0] for n in range(14)]
        legacy, _ = make_archive(self.root, 15, owned=False)
        unrelated = self.root / "runs/my-notes"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("user file")
        removed, errors = prune_archives(self.root)
        self.assertEqual(set(removed), set(paths[:4]))
        self.assertEqual(errors, [])
        self.assertTrue(all(path.is_dir() for path in paths[4:]))
        self.assertTrue(legacy.is_dir())
        self.assertTrue((unrelated / "keep.txt").is_file())
        self.assertEqual(prune_archives(self.root), ([], []))

    def test_active_writer_protected_then_cleaned_after_completion(self):
        paths = [make_archive(self.root, n)[0] for n in range(12)]
        data = json.loads((paths[0] / "manifest.json").read_text())
        data.update(archive_identity("benchmark"))
        (paths[0] / "manifest.json").write_text(json.dumps(data))
        removed, _ = prune_archives(self.root, protect=[paths[1]])
        self.assertEqual(removed, [])
        data.pop("writer")
        (paths[0] / "manifest.json").write_text(json.dumps(data))
        self.assertEqual(set(prune_archives(self.root)[0]), set(paths[:2]))

    def test_cli_without_psutil_preserves_active_and_uncertain_writers(self):
        with patch.dict("sys.modules", {"psutil": None}):
            identity = archive_identity("benchmark")
            self.assertEqual(identity["writer"], {"pid": os.getpid()})
            self.assertTrue(active_writer(identity))
            self.assertFalse(active_writer({}))
            with patch("result_storage.os.name", "nt"), patch("result_storage.os.kill") as kill:
                self.assertTrue(active_writer(identity))
                kill.assert_not_called()
            with patch("result_storage.os.name", "posix"), \
                 patch("result_storage.os.kill", side_effect=ProcessLookupError):
                self.assertFalse(active_writer(identity))

    def test_corrupt_or_mismatched_manifest_is_never_deleted(self):
        paths = [make_archive(self.root, n)[0] for n in range(12)]
        (paths[0] / "manifest.json").write_text("broken {")
        data = json.loads((paths[1] / "manifest.json").read_text())
        data["run_id"] = "some-other-directory"
        (paths[1] / "manifest.json").write_text(json.dumps(data))
        self.assertEqual(prune_archives(self.root), ([], []))
        self.assertTrue(all(path.is_dir() for path in paths))

    def test_linked_tree_and_external_file_are_never_followed(self):
        directory, _ = make_archive(self.root, 0)
        external = self.root / "personal.txt"
        external.write_text("keep")
        try:
            (directory / "linked.txt").symlink_to(external)
        except OSError:
            self.skipTest("symlink creation not permitted on this builder")
        self.assertIsNone(local_file(directory, "../personal.txt"))
        self.assertIsNone(local_file(directory, str(external)))
        self.assertIsNone(local_file(directory, "linked.txt"))
        make_archive(self.root, 1)
        self.assertEqual(prune_archives(self.root, keep=1), ([], []))
        self.assertEqual(external.read_text(), "keep")
        self.assertTrue(directory.is_dir())

    def test_cleanup_lock_serializes_independent_open_handles(self):
        for n in range(12):
            make_archive(self.root, n)
        with retention_lock(self.root) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(prune_archives(self.root), ([], []))
        self.assertEqual(len(prune_archives(self.root)[0]), 2)

    def test_latest_is_timestamp_order_not_mtime_or_iteration_order(self):
        first, _ = make_archive(self.root, 1)
        latest, _ = make_archive(self.root, 8)
        os.utime(first / "manifest.json", (2000000000, 2000000000))
        self.assertEqual(recent_archives([self.root, self.root], "benchmark")[0][1], latest)
        self.assertEqual(len(archives(self.root)), 2)

    def test_tail_is_bounded_skips_partial_and_nonfinite_records(self):
        directory, _ = make_archive(self.root, 1, "stress")
        samples = [{"elapsed": n, "values": {"cpu": n}} for n in range(2000)]
        (directory / "samples.jsonl").write_text("\n".join(map(json.dumps, samples)) + '\n{"elapsed": NaN,"values":{}}\n{"partial"', encoding="utf-8")
        tail = sample_tail(directory)
        self.assertLessEqual(len(tail), 601)
        self.assertEqual(tail[-1]["elapsed"], 1999)


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import Qt, QProcess
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
    from desktop_window import MainWindow
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "Qt desktop dependencies")
class DesktopArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memorytest-restore-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def window(self, restore=True):
        window = MainWindow(autodetect=False, output_dir=self.root, restore=restore)
        self.addCleanup(window.close)
        return window

    def cache_archive(self, number):
        from test_presentation import MODERN_CACHE
        directory, data = make_archive(self.root, number)
        (directory / "reports").mkdir()
        (directory / "reports/cache_hierarchy_report.md").write_text(MODERN_CACHE)
        return directory, data

    def test_relaunch_restores_latest_valid_round_not_mixed_old_modules(self):
        first, _ = self.cache_archive(1)
        latest, _ = self.cache_archive(2)
        corrupt, _ = self.cache_archive(3)
        (corrupt / "reports/cache_hierarchy_report.md").write_text("not a report")
        window = self.window()
        self.assertEqual(window.run_dir, latest)
        self.assertEqual(window.pages["cache_hierarchy"].model.status, "passed")
        self.assertEqual(window.pages["cpu_multi"].model.status, "not_run")
        self.assertTrue(window.open_report.isEnabled())
        self.assertIn(latest.name, window.pages["cache_hierarchy"].summary.text())
        window.close()
        again = self.window()
        self.assertEqual(again.run_dir, latest)
        self.assertFalse(again.benchmark_active)
        self.assertEqual(again.process.state(), QProcess.NotRunning)

    def test_restore_stress_history_does_not_restart_load_or_monitor(self):
        directory, data = make_archive(self.root, 1, "stress")
        data.update(catalog={"cpu": {"label": "CPU", "unit": "%"}},
                    statistics={"cpu": {"current": 20, "minimum": 10, "maximum": 30, "average": 20}}, elapsed=2)
        (directory / "manifest.json").write_text(json.dumps(data))
        (directory / "samples.jsonl").write_text('{"elapsed":2,"values":{"cpu":20}}\n')
        window = self.window()
        page = window.stress_page
        self.assertEqual(page.run_dir, directory)
        self.assertEqual(list(page.plot.samples), [(2, {"cpu": 20})])
        self.assertEqual(page.stats["cpu"]["average"], 20)
        self.assertFalse(page.active)
        self.assertEqual(page.preview.state(), QProcess.NotRunning)
        self.assertEqual(page.process.state(), QProcess.NotRunning)

    def test_output_readonly_licenses_inline_and_short_title(self):
        window = self.window()
        self.assertTrue(window.output.isReadOnly())
        self.assertFalse(hasattr(window, "browse"))
        from app_version import RELEASE_TAG
        self.assertEqual(window.windowTitle(), f"MemoryTest {RELEASE_TAG}")
        with patch.object(window, "open_path") as open_path:
            window.licenses.click()
            dialog = window.license_dialog
            self.assertTrue(dialog.reader.isReadOnly())
            self.assertIn("Permission is hereby granted", dialog.reader.toPlainText())
            self.assertGreaterEqual(dialog.selector.count(), 2)
            self.assertFalse(open_path.called)
            dialog.close()
        self.app.processEvents()

    def test_cleanup_warning_survives_successful_worker_exit(self):
        directory, _ = self.cache_archive(1)
        window = self.window(restore=False)
        window.handle_event(dict(type="finished", report=str(directory / "report.html"),
                                 cleanup_errors=["fixture permission failure"]))
        window.finished(0)
        self.assertEqual(window.status_key, "cleanup_error")
        page = window.stress_page
        page.handle(dict(type="stress_finished", reason="duration", report=None,
                         cleanup_errors=["fixture permission failure"]))
        page.finished(0)
        self.assertEqual(window.status_key, "cleanup_error")
        self.assertEqual(page.status_key, "duration")

    def test_cache_controls_equal_resize_and_empty_result_centered(self):
        window = self.window(restore=False)
        window.tabs.setCurrentIndex(1)
        window.show()
        page = window.pages["cache_hierarchy"]
        buttons = [page.run, page.cancel, page.open, page.log_button]
        previous = None
        for width, height in ((880, 720), (1400, 1000)):
            window.resize(width, height)
            self.app.processEvents()
            widths = [button.width() for button in buttons]
            self.assertLessEqual(max(widths) - min(widths), 1)
            self.assertEqual(buttons[0].x(), 0)
            self.assertLessEqual(abs(buttons[-1].geometry().right() - page.width() + 1), 1)
            if previous is not None:
                self.assertGreater(widths[0], previous)
            previous = widths[0]
            for i in range(3):
                page.details.setCurrentIndex(i)
                self.app.processEvents()
                view = page.details.widget(i)
                empty = view.findChild(QLabel, "empty-result")
                self.assertEqual(empty.alignment(), Qt.AlignCenter)
                center = empty.mapTo(view.viewport(), empty.rect().center())
                self.assertLessEqual(abs(center.y() - view.viewport().rect().center().y()), 2)
                self.assertLessEqual(abs(center.x() - view.viewport().rect().center().x()), 2)
        evidence = Path(__file__).resolve().parents[1] / "build/ui-storage"
        evidence.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(evidence / "cache-empty.png"))

    def test_tools_container_exposes_stress_as_first_subtool(self):
        window = self.window()
        page = window.stress_page
        page.caps = {"modes": {"cpu": True}, "threads": 1}
        page.run_dir = self.root
        window.show()
        window.tools_button.click()
        self.app.processEvents()
        self.assertIs(window.tabs.currentWidget(), window.tools_page)
        self.assertIs(window.tools_tabs.widget(0), page)
        self.assertTrue(page.isVisible())
        self.assertTrue(window.tools_button.isChecked())
        from i18n import tr
        self.assertEqual(window.tools_button.text(), tr("tools", window.language))
        self.assertEqual(window.tools_tabs.tabText(0), tr("stress", window.language))
        self.assertEqual(page.process.state(), QProcess.NotRunning)


if __name__ == "__main__":
    unittest.main()
