"""Independent stress references and bounded supervisor tests; never full load."""
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmark_registry import ROOT
from i18n import ref
from run_stress import run_session, Child
from stress_common import Statistics, available_memory, memory_limits, validate_modes, MIB, MODES
from telemetry import Provider


CAPS = {"modes": dict.fromkeys(MODES, True), "threads": 2}


class StressDataTests(unittest.TestCase):
    def test_missing_optional_frequency_api_keeps_basic_monitoring(self):
        system = SimpleNamespace(cpu_percent=lambda interval: 12.5,
                                 virtual_memory=lambda: SimpleNamespace(percent=25))
        with tempfile.TemporaryDirectory() as directory, patch("telemetry.psutil", system), patch("telemetry.sys.platform", "darwin"):
            sample = Provider(bin_dir=Path(directory)).sample()
        self.assertEqual(sample["values"], {"cpu:usage": 12.5, "memory:usage": 25})
        self.assertNotIn("cpu:frequency", sample["catalog"])

    def test_all_valid_samples_not_visible_window_or_missing_zero(self):
        stats = Statistics()
        for value in [2, 8, None, float("nan"), True, 20]:
            stats.sample({"sensor": value})
        self.assertEqual(stats.rows["sensor"], dict(current=20, minimum=2, maximum=20, average=10, count=3))
        stats.sample({})
        self.assertIsNone(stats.rows["sensor"]["current"])
        self.assertEqual(stats.rows["sensor"]["average"], 10)
        for value in range(1200):
            stats.sample({"long": value})
        self.assertEqual(stats.rows["long"]["count"], 1200)
        self.assertEqual(stats.rows["long"]["average"], 599.5)

    def test_budget_counts_all_load_allocations(self):
        self.assertEqual(memory_limits(8 * 1024 * MIB, 16, True), (2048, 4031))
        self.assertEqual(memory_limits(400 * MIB, 16, True), (100, 135))
        self.assertEqual(memory_limits(64 * MIB, 16, True), (0, 0))
        self.assertEqual(memory_limits(0, 1), (0, 0))

    def test_cgroup_ancestor_available_budget_and_unlimited_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            group = root / "parent/child"
            group.mkdir(parents=True)
            membership = root / "membership"
            membership.write_text("0::/parent/child\n")
            (group / "memory.max").write_text("max")
            (group / "memory.current").write_text("0")
            (group.parent / "memory.max").write_text(str(512 * MIB))
            (group.parent / "memory.current").write_text(str(128 * MIB))
            self.assertEqual(available_memory(8 * 1024 * MIB, root, membership), 384 * MIB)
            self.assertEqual(available_memory(200 * MIB, root, membership), 200 * MIB)

    def test_unknown_and_unavailable_modes_rejected_no_fallback(self):
        for values in [["disk"], ["gpu"], "cpu"]:
            with self.assertRaises(ValueError):
                validate_modes(values, {"cpu": True})
        self.assertEqual(validate_modes([], CAPS["modes"]), [])

    def test_sensor_disconnect_and_permission_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node = root / "class/hwmon/hwmon0"
            node.mkdir(parents=True)
            sensor = node / "temp1_input"
            sensor.write_text("42000")
            provider = Provider(sysfs=root)
            values = {}
            self.assertFalse(provider.linux(values))
            key = next(iter(values))
            self.assertEqual(values[key], 42)
            sensor.unlink()
            with patch("telemetry.sys.platform", "linux"):
                sample = provider.sample()
            self.assertIsNone(sample["values"][key])
            self.assertFalse(sample["has_temperature"])
            with patch.object(Path, "read_text", side_effect=PermissionError):
                self.assertIsNone(provider.read_number(sensor))
            self.assertTrue(provider.denied)

    def test_critical_temperature_uses_reported_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            node = Path(directory) / "class/hwmon/hwmon0"
            node.mkdir(parents=True)
            (node / "temp1_input").write_text("100000")
            (node / "temp1_crit").write_text("99000")
            self.assertTrue(Provider(sysfs=Path(directory)).linux({}))


class SupervisorTests(unittest.TestCase):
    def session(self, failure=None, thermal=False, callback=None, parent_pid=None):
        created, sent, emitted = [], [], []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "control.json"
            class FakeChild:
                def __init__(self, name, command, folder, events):
                    self.name, self.stopped = name, False
                    self.process = self
                    created.append(self)
                    if name == "cpu" and failure:
                        events.put((name, "error " + failure, time.monotonic()))
                    if name == "sensors":
                        events.put((name, json.dumps({"catalog": {"s": {"label": ref("cpu_usage"), "unit": "%", "kind": "utilization"}},
                                    "values": {"s": 12}, "thermal_critical": thermal, "permission_denied": True}), time.monotonic()))
                def send(self, value):
                    sent.append(value)
                def poll(self):
                    return None
                def stop(self, graceful=False):
                    self.stopped = True
            def event(**data):
                emitted.append(data)
                if callback:
                    callback(data, control)
            code = run_session(root, root, ["cpu"], 1, 1, duration=2 if callback else .16, control=control, parent_pid=parent_pid,
                               capability_data=CAPS, child_factory=FakeChild, event=event)
            manifest = json.loads(next(root.glob("stress-runs/*/manifest.json")).read_text())
            self.assertTrue(all(child.stopped for child in created))
            self.assertTrue(next(root.glob("stress-runs/*/stress_report.html")).is_file())
        return code, manifest, sent, emitted, created

    def test_hot_switch_monitor_only_gpu_and_stop_with_same_file_timestamp(self):
        def change(data, control):
            if data.get("event") == "mode_change":
                modes = data["modes"]
                request = {"modes": []} if modes == ["cpu"] else {"modes": ["ram", "gpu"]} if not modes else {"stop": True}
                control.write_text(json.dumps(request))
                os.utime(control, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        code, manifest, sent, events, children = self.session(callback=change)
        self.assertEqual(code, 0)
        self.assertEqual(sent, ["1", "0", "8"])
        self.assertEqual([child.name for child in children], ["cpu", "sensors", "gpu"])
        self.assertEqual(manifest["reason"], "user_stop")
        self.assertEqual([e["modes"] for e in manifest["events"] if "modes" in e], [["cpu"], [], ["ram", "gpu"]])

    def test_calculation_failure_stops_every_child(self):
        code, manifest, *_ = self.session(failure="calculation_error")
        self.assertEqual(code, 1)
        self.assertEqual(manifest["reason"], "calculation_error")

    def test_hot_switch_does_not_double_count_owned_ram(self):
        def change(data, control):
            if data.get("event") == "mode_change":
                selected = data["modes"]
                request = {"modes": ["ram"]} if selected == ["cpu"] else {"modes": ["cpu", "ram"]} if selected == ["ram"] else {"stop": True}
                control.write_text(json.dumps(request))
        with patch("run_stress.available_memory", side_effect=[4 * MIB, 4 * MIB, int(1.5 * MIB)]):
            code, manifest, sent, *_ = self.session(callback=change)
        self.assertEqual((code, manifest["reason"]), (0, "user_stop"))
        self.assertEqual(sent, ["1", "8", "9"])

    def test_thermal_stop_and_duration_stop(self):
        code, manifest, *_ = self.session(thermal=True)
        self.assertEqual((code, manifest["reason"]), (1, "thermal_stop"))
        code, manifest, *_ = self.session()
        self.assertEqual((code, manifest["reason"]), (0, "duration_stop"))
        self.assertEqual(manifest["statistics"]["s"]["average"], 12)

    def test_parent_exit_stops_every_child(self):
        with subprocess.Popen([sys.executable, "-c", "pass"]) as parent:
            parent.wait()
        code, manifest, *_ = self.session(parent_pid=parent.pid)
        self.assertEqual((code, manifest["reason"]), (0, "parent_exit"))

    def test_malformed_hot_request_fails_closed(self):
        for request in ['{"modes":["disk"]}', '[]', '{', ' ' * 65537]:
            with self.subTest(request=request[:30]):
                def bad(data, control):
                    if data.get("event") == "mode_change":
                        control.write_text(request)
                code, manifest, *_ = self.session(callback=bad)
                self.assertEqual((code, manifest["status"]), (1, "failed"))


class NativeStressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-stress-test-")
        cls.binary = Path(cls.temp.name) / ("stress_cpu.exe" if os.name == "nt" else "stress_cpu")
        command = shlex.split(os.environ.get("CC", "cc")) + ["-O2", "-std=c11", "-pthread", "-ffp-contract=off",
            str(ROOT / "src/stress_cpu.c"), "-lm", "-o", str(cls.binary)]
        subprocess.run(command, capture_output=True, check=True, timeout=45)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_independent_big_integer_and_decimal_closed_form(self):
        result = subprocess.run([str(self.binary), "--self-test"], capture_output=True, text=True, check=True, timeout=5)
        measured = json.loads(result.stdout)
        value, mask = 0x123456789abcdef0, (1 << 64) - 1
        for _ in range(65536):
            value ^= (value << 13) & mask
            value ^= value >> 7
            value ^= (value << 17) & mask
        self.assertEqual(measured["integer"], f"{value:016x}")
        with localcontext() as context:
            context.prec = 60
            # Closed-form affine recurrence, not a copy of the C loop.
            factor = Decimal("1.0000001") ** 65536
            expected = Decimal("6.5") * factor - 4
        self.assertLess(abs(Decimal(str(measured["float"])) - expected), Decimal("1e-9"))

    def test_one_thread_one_mib_hot_switch_and_eof_cleanup(self):
        process = subprocess.Popen([str(self.binary), "1", "1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for mask in [1, 2, 4, 8, 15, 0]:
                process.stdin.write(str(mask) + "\n")
                process.stdin.flush()
                time.sleep(.08)
            stdout, stderr = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertNotIn("error", stdout)
            self.assertIn("modes 0", stdout)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_real_supervisor_duration_cleans_workers(self):
        import psutil
        pids = []
        def factory(*args):
            child = Child(*args)
            pids.append(child.process.pid)
            return child
        with tempfile.TemporaryDirectory() as directory:
            code = run_session(directory, self.binary.parent, ["cpu"], 1, 1, duration=1.2,
                               capability_data=CAPS, child_factory=factory, event=lambda **_: None)
            self.assertEqual(code, 0)
        self.assertTrue(pids)
        self.assertTrue(all(not psutil.pid_exists(pid) for pid in pids))


if __name__ == "__main__":
    unittest.main()
