"""Monitoring transitions without elevation, real stress loads or real sensors."""
import json
import os
from pathlib import Path
import tempfile
import time
import sys
import unittest
from unittest.mock import patch, Mock

from run_stress import run_session
from telemetry_stream import publish_snapshot, read_snapshot

SAMPLE = {"catalog": {"enhanced:power": {"label": "CPU package", "unit": "W", "kind": "power"}},
          "values": {"enhanced:power": 47}, "has_temperature": False, "thermal_critical": False}


class SharedStreamTests(unittest.TestCase):
    def test_atomic_snapshot_expiration_and_malformed_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.json"
            publish_snapshot(path, SAMPLE)
            self.assertEqual(read_snapshot(path)["values"], SAMPLE["values"])
            self.assertFalse(path.with_suffix(".tmp").exists())
            for data in ({**SAMPLE, "sampled_at": time.monotonic() - 5},
                         {**SAMPLE, "sampled_at": float("nan")}, [], {},
                         {**SAMPLE, "sampled_at": time.monotonic(), "catalog": {"s": None}}):
                path.write_text(json.dumps(data))
                self.assertIsNone(read_snapshot(path))

    def shared_session(self, thermal=False, stale=False):
        tick, created = [100.0], []
        with tempfile.TemporaryDirectory() as directory, \
             patch("time.monotonic", side_effect=lambda: tick[0]), \
             patch("time.sleep", side_effect=lambda value: tick.__setitem__(0, tick[0] + value)):
            root = Path(directory)
            snapshot = root / "live.json"
            publish_snapshot(snapshot, dict(SAMPLE, thermal_critical=thermal))
            class FakeChild:
                def __init__(self, name, command, folder, events):
                    self.name, self.process, self.stopped = name, self, False
                    created.append(self)
                def send(self, text):
                    pass
                def poll(self):
                    return None
                def stop(self, **kwargs):
                    self.stopped = True
            caps = {"modes": {"cpu": True}, "threads": 1}
            code = run_session(root, root, ["cpu"], 1, 1, duration=10 if stale else .2,
                               capability_data=caps, child_factory=FakeChild, sensor_samples=snapshot,
                               event=lambda **_: None)
            manifest = json.loads(next(root.glob("stress-runs/*/manifest.json")).read_text())
            self.assertTrue(all(child.stopped for child in created))
        return code, manifest, [child.name for child in created]

    def test_stress_reuses_enhanced_stream_without_spawning_second_provider(self):
        code, manifest, names = self.shared_session()
        self.assertEqual((code, names), (0, ["cpu"]))
        self.assertEqual(manifest["statistics"]["enhanced:power"]["average"], 47)
        self.assertEqual(manifest["statistics"]["enhanced:power"]["count"], 1)

    def test_shared_stream_thermal_and_staleness_stop_load(self):
        code, manifest, _ = self.shared_session(thermal=True)
        self.assertEqual((code, manifest["reason"]), (1, "thermal_stop"))
        code, manifest, _ = self.shared_session(stale=True)
        self.assertEqual((code, manifest["reason"]), (1, "backend_error"))
        self.assertGreaterEqual(manifest["elapsed"], 8)
        self.assertLess(manifest["elapsed"], 10)


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import Qt, QProcess, QByteArray
    from PySide6.QtWidgets import QApplication, QMessageBox
    from desktop_window import MainWindow
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "Qt desktop dependencies")
class MonitorTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memorytest-monitor-ui-")
        self.addCleanup(self.temp.cleanup)
        self.window = MainWindow(autodetect=False, output_dir=self.temp.name, restore=False)
        self.addCleanup(self.window.close)
        self.page = self.window.stress_page
        self.page.caps = {"modes": {"cpu": True}, "threads": 1}
        self.page.threads.setValue(1)
        self.page.modes["cpu"].setEnabled(True)

    def seed(self):
        self.page.sample(dict(SAMPLE, elapsed=1, statistics={"enhanced:power": {"current": 47}}))

    def test_start_preserves_plot_selection_and_authorized_sampler(self):
        self.seed()
        self.page.plot.visible.clear()
        access = self.window.monitor_access
        access.authorized, access.status = True, "ready"
        self.page.enhanced_enabled = True
        access.active = True
        before = list(self.page.plot.samples)
        with patch.object(self.page.preview, "state", return_value=QProcess.Running), \
             patch.object(self.page.preview, "terminate") as terminate, \
             patch.object(self.page.process, "start") as start:
            self.page.start()
            self.assertEqual(list(self.page.plot.samples), before)
            self.assertEqual(self.page.plot.visible, set())
            self.assertTrue(access.active)
            self.assertTrue(self.page.enhanced_enabled)
            self.assertFalse(hasattr(self.page, "enhanced"))
            terminate.assert_not_called()
            args = start.call_args.args[1]
            self.assertIn("--sensor-samples", args)
            self.assertEqual(args[args.index("--sensor-samples") + 1], str(self.page.sensor_samples))
            self.page.active = False
            self.window.set_stress_running(False)
            self.page.control.cleanup()
            self.page.control = None

    def test_preview_updates_continue_during_stress_without_worker_time_reset(self):
        self.seed()
        self.page.active = True
        self.page.preview_started = time.monotonic() - 20
        raw = QByteArray((json.dumps(SAMPLE) + "\n").encode())
        with patch.object(self.page.preview, "readAllStandardOutput", return_value=raw):
            self.page.read(self.page.preview)
        self.page.handle(dict(type="stress_sample", elapsed=.5, modes=["cpu"]))
        self.assertEqual(len(self.page.plot.samples), 2)
        self.assertGreaterEqual(self.page.plot.samples[-1][0], 20)
        self.assertEqual(self.page.plot.samples[-1][1]["enhanced:power"], 47)
        self.page.active = False

    def test_restored_report_does_not_block_idle_monitor_or_mix_statistics(self):
        directory = Path(self.temp.name)
        data = dict(catalog=SAMPLE["catalog"], elapsed=900,
                    statistics={"enhanced:power": {"count": 100, "average": 999}})
        self.assertTrue(self.page.restore_archive(directory, data))
        self.assertEqual(self.page.preview_stats.rows, {})
        with patch.object(self.page, "isVisible", return_value=True), \
             patch.object(self.page.preview, "start") as start:
            self.page.start_preview()
            start.assert_called_once()
        raw = QByteArray((json.dumps(SAMPLE) + "\n").encode())
        with patch.object(self.page.preview, "readAllStandardOutput", return_value=raw):
            self.page.read(self.page.preview)
        self.assertFalse(self.page.active)
        self.assertEqual(self.page.process.state(), QProcess.NotRunning)
        self.assertEqual(len(self.page.plot.samples), 1)
        self.assertEqual(self.page.stats["enhanced:power"]["count"], 1)
        self.assertEqual(self.page.stats["enhanced:power"]["average"], 47)
        self.assertLess(self.page.plot.samples[0][0], 900)

    def test_finished_load_preserves_visible_monitor_and_access(self):
        self.page.active = True
        self.page.run_dir = Path(self.temp.name)
        self.page.enhanced_enabled = True
        with patch.object(self.page, "isVisible", return_value=True), \
             patch.object(self.page.preview, "state", return_value=QProcess.Running), \
             patch.object(self.page, "stop_preview") as stop, \
             patch.object(self.window.monitor_access, "set_active") as access:
            self.page.finished(0)
            stop.assert_not_called()
            access.assert_called_with(True)
        self.assertFalse(self.page.active)

    def test_quick_hide_show_restarts_after_old_monitor_exits(self):
        with patch.object(self.page.preview, "state", return_value=QProcess.Running), \
             patch.object(self.page.preview, "terminate"), \
             patch.object(self.page, "isVisible", return_value=True):
            self.page.stop_preview()
            self.page.start_preview()
            self.assertTrue(self.page.preview_restart)
        with patch.object(self.page, "isVisible", return_value=True), \
             patch.object(self.page.preview, "start") as start:
            self.page.preview_finished()
            start.assert_called_once()
        self.page.shutting_down = True
        self.page.preview_restart = True
        with patch.object(self.page.preview, "start") as start:
            self.page.preview_finished()
            start.assert_not_called()

    def test_idle_chart_receives_async_worker_samples_after_restore(self):
        self.page.restore_archive(Path(self.temp.name), dict(catalog=SAMPLE["catalog"], statistics={}, elapsed=20))
        script = "import json,time; sample=" + repr(SAMPLE) + "; " + \
                 "print(json.dumps(sample),flush=True); time.sleep(.15); print(json.dumps(sample),flush=True)"
        with patch("desktop_stress.app_command", return_value=[sys.executable, "-u", "-c", script]), \
             patch.object(self.page.process, "start") as load:
            self.window.show()
            self.window.tools_button.click()
            deadline = time.monotonic() + 5
            while len(self.page.plot.samples) < 2 and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
            self.assertGreaterEqual(len(self.page.plot.samples), 2)
            self.assertGreater(self.page.plot.samples[-1][0], self.page.plot.samples[0][0])
            self.assertEqual(self.page.stats["enhanced:power"]["count"], 2)
            self.assertEqual(self.page.stats["enhanced:power"]["average"], 47)
            self.assertFalse(self.page.active)
            load.assert_not_called()

    def test_all_checkbox_controls_existing_and_new_sensors_not_statistics(self):
        self.seed()
        self.assertEqual(self.page.all_sensors.checkState(), Qt.Checked)
        self.page.all_sensors.click()
        self.assertEqual(self.page.plot.visible, set())
        self.assertEqual(self.page.stats["enhanced:power"]["current"], 47)
        sample = dict(SAMPLE, catalog={**SAMPLE["catalog"], "temperature": {"label": "CPU", "unit": "°C"}},
                      values={**SAMPLE["values"], "temperature": 42}, elapsed=2, statistics={})
        self.page.sample(sample)
        self.assertEqual(self.page.plot.visible, set())
        self.page.table.item(0, 0).setCheckState(Qt.Checked)
        self.assertEqual(self.page.all_sensors.checkState(), Qt.PartiallyChecked)
        self.page.all_sensors.click()
        self.assertEqual(self.page.plot.visible, set(self.page.catalog))
        self.assertEqual(self.page.all_sensors.checkState(), Qt.Checked)

    def test_standard_startup_never_prompts_or_reveals_manual_entry(self):
        self.window.administrator = False
        self.page.enhanced_enabled = False
        self.assertFalse(hasattr(self.page, "enhanced"))
        self.window.settings.setValue("monitorPermissionAskedV2", True)
        with patch("desktop_window.QMessageBox.question", return_value=QMessageBox.No) as prompt, \
             patch.object(self.window.monitor_access, "start") as elevate:
            self.window.start_monitor_access()
            self.window.start_monitor_access()
            prompt.assert_not_called()
            elevate.assert_not_called()
        self.assertFalse(self.page.enhanced_enabled)
        self.assertFalse(hasattr(self.page, "retry_monitor"))

    def test_hide_during_stress_does_not_pause_monitoring(self):
        self.page.active = True
        self.page.enhanced_enabled = True
        with patch.object(self.page, "stop_preview") as stop:
            from PySide6.QtGui import QHideEvent
            self.page.hideEvent(QHideEvent())
            stop.assert_not_called()
        self.assertTrue(self.window.monitor_access.active)
        self.page.active = False


if __name__ == "__main__":
    unittest.main()
