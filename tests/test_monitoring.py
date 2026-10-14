"""Independent sensor fixtures and bounded read-only IPC; no elevation or load."""
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from benchmark_registry import ROOT
from i18n import display, ref
from monitor_access import MonitorAccess, authorization_command, normalize_windows, parse_powermetrics
from sensor_definitions import smc_label
from telemetry import Provider


class SensorContractTests(unittest.TestCase):
    def test_generation_specific_thermal_regions_not_core_numbers(self):
        self.assertIn("能效", display(smc_label("Tp09", "temperature", "Apple M1 Pro"), "zh"))
        self.assertIn("性能", display(smc_label("Tp09", "temperature", "Apple M2 Max"), "zh"))
        self.assertIn("Performance", display(smc_label("Tf04", "temperature", "Apple M3")))
        self.assertIn("性能", display(smc_label("Tp0V", "temperature", "Apple M4 Pro"), "zh"))
        unknown = display(smc_label("Tp09", "temperature", "Unknown Future CPU"))
        self.assertIn("unmapped", unknown)
        self.assertIn("Tp09", unknown)
        self.assertNotIn("Performance", unknown)

    def test_cluster_frequencies_keep_native_identity_units_and_no_nominal_guess(self):
        fixture = """E-Cluster HW active frequency: 972 MHz
P0-Cluster HW active frequency: 3.25 GHz
P1-Cluster HW active frequency: 4150 MHz
S2-Cluster HW active frequency: 4.40 GHz
GPU active frequency: 450 MHz
CPU 3 frequency: 2345 MHz
CPU Power: 1250 mW
GPU Power: 2.50 W
CPU Average frequency as fraction of nominal: 75%
Nominal frequency: 9000 MHz
"""
        sensors = {item["id"]: item for item in parse_powermetrics(fixture)}
        expected = {"E-Cluster": 972, "P0-Cluster": 3250, "P1-Cluster": 4150, "S2-Cluster": 4400,
                    "gpu_frequency": 450, "cpu3": 2345, "cpu_power": 1.25, "gpu_power": 2.50}
        self.assertEqual({key.removeprefix("powermetrics:"): item["value"] for key, item in sensors.items()}, expected)
        self.assertEqual(sensors["powermetrics:S2-Cluster"]["label"]["key"], "cluster_frequency")
        self.assertNotIn("powermetrics:cpu_active", sensors)
        intel = parse_powermetrics("CPU Average frequency as fraction of nominal: 62.5% (1500 MHz)")
        self.assertEqual(intel[0]["value"], 1500)

    def test_windows_provider_names_and_sensor_type_are_preserved(self):
        sensors = normalize_windows([
            {"Identifier": "/intelcpu/0/clock/0", "Name": "CPU Core #1", "SensorType": "Clock", "Value": 3100},
            {"Identifier": "/amd/0/temperature/0", "Name": "GPU Core", "SensorType": "Temperature", "Value": 42},
            {"Identifier": "bad", "SensorType": "Unknown", "Value": 100}])
        self.assertEqual(len(sensors), 2)
        self.assertEqual(sensors[0]["unit"], "MHz")
        self.assertIn("核心 1", display(sensors[0]["label"], "zh"))
        self.assertIn("/amd/0/temperature/0", display(sensors[1]["label"]))

    def test_linux_frequency_distinguishes_hardware_and_os_values_not_global_nominal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "devices/system/cpu/cpufreq/policy4"
            policy.mkdir(parents=True)
            (policy / "cpuinfo_cur_freq").write_text("2222000")
            (policy / "scaling_cur_freq").write_text("3333000")
            (policy / "affected_cpus").write_text("4 5 6 7")
            provider = Provider(sysfs=root)
            values = {}
            provider.linux(values)
            self.assertEqual(values["cpufreq:policy4"], 2222)
            self.assertIn("Hardware", display(provider.catalog["cpufreq:policy4"]["label"]))
            # A changed source must not keep its previous, stronger description.
            (policy / "cpuinfo_cur_freq").unlink()
            provider.linux(values)
            self.assertEqual(values["cpufreq:policy4"], 3333)
            self.assertIn("OS-reported", display(provider.catalog["cpufreq:policy4"]["label"]))

    def test_privileged_samples_expire_and_cannot_inflate_valid_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.json"
            provider = Provider(sysfs=Path(directory), privileged_samples=path)
            sensor = {"id": "extra", "kind": "frequency", "unit": "MHz", "value": 2468, "label": ref("cpu_active_frequency")}
            path.write_text(json.dumps({"sampled_at": time.monotonic(), "sensors": [sensor], "thermal_critical": True}))
            sample = provider.sample()
            self.assertEqual(sample["values"]["extra"], 2468)
            self.assertTrue(sample["thermal_critical"])
            path.write_text(json.dumps({"sampled_at": time.monotonic() - 10, "sensors": [sensor]}))
            self.assertIsNone(provider.sample()["values"]["extra"])
            path.write_text("[]")
            self.assertIsNone(provider.sample()["values"]["extra"])

    def test_authorization_uses_only_fixed_helper_and_numeric_session_arguments(self):
        helper = Path("/tmp/Memory Test's $() app/telemetry_privileged")
        command = authorization_command(helper, 12345, "ab" * 32, 1234, "darwin")
        self.assertEqual(command[0], "/usr/bin/osascript")
        self.assertEqual(shlex.split(command[-1]), [str(helper), "12345", "ab" * 32, "1234"])
        command = authorization_command(helper, 12345, "ab" * 32, 1234, "linux")
        self.assertEqual(command[:2], ["/usr/bin/pkexec", str(helper)])
        self.assertNotIn("python", " ".join(command).lower())

    def test_socket_setup_failure_leaves_no_authorization_spinner(self):
        access = MonitorAccess()
        self.addCleanup(access.close)
        access.status = "authorizing"
        with patch("monitor_access.socket.socket", side_effect=OSError("unavailable")):
            access._run(None)
        self.assertEqual(access.status, "error")
        self.assertIsNone(access.server)

    def test_invalid_sensor_shapes_do_not_become_measurements(self):
        access = MonitorAccess()
        self.addCleanup(access.close)
        self.assertEqual(access._linux(None), [])
        self.assertEqual(normalize_windows({"unexpected": "shape"}), [])
        self.assertEqual(access._linux([None, "bad", {"id": 1}, {"id": "x", "kind": "unknown"}]), [])

    def test_windows_hardware_card_preserves_cim_clock_units(self):
        import system_info
        fixture = {"cpu": [{"Name": " Example CPU ", "MaxClockSpeed": 3200}],
                   "memory": [{"ConfiguredClockSpeed": 6400}, {"ConfiguredClockSpeed": 0}],
                   "gpu": [{"Name": "示例 GPU"}]}
        with patch("system_info.sys.platform", "win32"), patch("system_info.command", return_value=json.dumps(fixture)):
            result = system_info.collect()
        self.assertEqual(result["cpu_model"], "Example CPU")
        self.assertEqual(result["nominal_frequency"], "3200 MHz")
        self.assertEqual(result["memory_rate"], "6400 MHz")
        self.assertEqual(result["gpu"], "示例 GPU")

    def test_apple_hardware_card_does_not_invent_unpublished_frequencies(self):
        import system_info
        values = {"machdep.cpu.brand_string": "Apple M4 Pro", "hw.perflevel0.name": "Performance",
                  "hw.perflevel0.physicalcpu": "8", "hw.perflevel1.name": "Efficiency", "hw.perflevel1.physicalcpu": "4"}
        def fixture(arguments, *_):
            if arguments[0].endswith("system_profiler"):
                return json.dumps({"SPDisplaysDataType": [{"sppci_model": "Apple M4 Pro"}], "SPMemoryDataType": []})
            return values.get(arguments[-1], "") if "-n" in arguments else ""
        with patch("system_info.sys.platform", "darwin"), patch("system_info.command", side_effect=fixture):
            result = system_info.collect()
        self.assertEqual((result["performance"], result["efficiency_cores"]), (8, 4))
        self.assertIsNone(result["nominal_frequency"])
        self.assertIsNone(result["memory_rate"])


class NativeMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="memorytest-monitor-test-")
        cls.binary = Path(cls.directory.name) / ("telemetry_privileged.exe" if os.name == "nt" else "telemetry_privileged")
        compiler = shlex.split(os.environ.get("CC", "cc"))
        subprocess.run(compiler + ["-O2", "-std=c11", str(ROOT / "src/telemetry_privileged.c"), "-o", str(cls.binary)] +
            (["-lws2_32", "-static-libgcc"] if os.name == "nt" else []), check=True, capture_output=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def launch(self, parent_pid=None):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0)); server.listen(1); server.settimeout(4)
        token = "01" * 32
        process = subprocess.Popen([str(self.binary), str(server.getsockname()[1]), token, str(parent_pid or os.getpid())],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.addCleanup(process.stdout.close); self.addCleanup(process.stderr.close)
        peer, _ = server.accept(); peer.settimeout(4)
        self.addCleanup(peer.close)
        stream = peer.makefile("rb")
        self.addCleanup(stream.close)
        self.assertEqual(stream.readline().strip(), ("MTMON/1 " + token).encode())
        self.assertEqual(stream.readline(), b"ready\n")
        return process, peer, stream

    def test_invalid_input_never_connects_or_spawns_commands(self):
        for arguments in (["1", "bad", "1"], ["65536", "ab" * 32, "123"], ["1", "ab" * 32, "0"]):
            self.assertEqual(subprocess.run([str(self.binary), *arguments], capture_output=True, timeout=3).returncode, 2)

    def test_idle_disconnect_stops_helper_without_sampling(self):
        process, peer, stream = self.launch()
        peer.sendall(b"0")
        stream.close(); peer.close()
        self.assertEqual(process.wait(timeout=4), 0)

    def test_arbitrary_ipc_command_is_rejected(self):
        process, peer, _ = self.launch()
        peer.sendall(b"/bin/sh")
        self.assertEqual(process.wait(timeout=4), 0)

    def test_requesting_parent_exit_stops_helper(self):
        parent = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(1)"])
        try:
            process, _, _ = self.launch(parent.pid)
            parent.wait(timeout=4)
            self.assertEqual(process.wait(timeout=4), 0)
        finally:
            if parent.poll() is None:
                parent.kill(); parent.wait()

    @unittest.skipUnless(sys.platform.startswith("linux"), "read-only sysfs bridge on Linux")
    def test_monitor_only_pause_and_cleanup_without_root(self):
        access = MonitorAccess(self.binary)
        self.addCleanup(access.close)
        access.set_active(True)
        access.start(launcher=lambda args: subprocess.Popen(args[1:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        deadline = time.monotonic() + 4
        while not access.samples_path.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(access.status, "ready")
        self.assertIsInstance(json.loads(access.samples_path.read_text())["sensors"], list)
        access.set_active(False)
        time.sleep(.25)
        self.assertEqual(json.loads(access.samples_path.read_text())["sensors"], [])
        process = access.process
        access.close()
        self.assertEqual(process.wait(timeout=3), 0)


if __name__ == "__main__":
    unittest.main()
