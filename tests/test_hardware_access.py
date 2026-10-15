"""Independent platform data and bounded helper IPC. Never requests elevation."""
import json
import importlib.util
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from hardware_access import InspectionFailure, InspectorAccess, operation
from hardware_inspector import Reader, cpu_info, gpu_info, dram_info, disk_info
from monitor_access import authorization_command

ROOT = Path(__file__).resolve().parent.parent


def fields(dev):
    return {f["name"]: f for section in dev["groups"] for f in section["fields"]}


class PlatformInventoryTests(unittest.TestCase):
    def test_arm_features_and_performance_cache_domains_are_not_intel_fields(self):
        reader = Mock(privileged=False)
        reader.run.return_value = (b"machdep.cpu.brand_string: Apple M4 Pro\nhw.cpufamily: 12345\nhw.cpusubtype: 2\n"
            b"hw.optional.arm.FEAT_AES: 1\nhw.optional.arm.FEAT_SME: 0\nhw.perflevel0.name: Performance\n"
            b"hw.perflevel0.physicalcpu: 10\nhw.perflevel0.l2cachesize: 16777216\nhw.perflevel0.cpusperl2: 5\n"
            b"hw.perflevel1.name: Efficiency\nhw.perflevel1.physicalcpu: 4\nhw.perflevel1.l2cachesize: 4194304\n", 1)
        with patch("sys.platform", "darwin"):
            dev = cpu_info(reader)[0]
        data = fields(dev)
        self.assertEqual(data["vendor"]["value"], "Apple")
        self.assertEqual(data["family"]["value"], "12345")
        self.assertEqual(data["instructions"]["value"], "arm.FEAT_AES")
        self.assertIsNone(data["nominal_frequency"]["value"])
        self.assertEqual(data["nominal_frequency"]["status"], "not_exposed")
        rows = dev["tables"][0]["rows"]
        self.assertEqual([r[5] for r in rows], ["16 MiB", "4 MiB"])
        self.assertEqual(rows[0][6], "5")
        self.assertEqual(dev["groups"][3]["title"], "cache_lowest_performance")
        self.assertEqual(dev["tables"][0]["title"], "performance_levels")

    def test_performance_levels_follow_os_count_not_two_core_type_assumption(self):
        reader = Mock(privileged=False)
        reader.run.side_effect = [(b"machdep.cpu.brand_string: Apple Example\nhw.nperflevels: 3\nhw.l2cachesize: 4194304\n", 0),
                                  (b"hw.perflevel0.name: Fast\nhw.perflevel1.name: Middle\nhw.perflevel2.name: Slow\nhw.perflevel2.l2cachesize: 4194304\n", 0)]
        with patch("sys.platform", "darwin"):
            dev = cpu_info(reader)[0]
        query = reader.run.call_args.args[0]
        self.assertIn("hw.perflevel2.cpusperl3", query)
        self.assertNotIn("hw.perflevel3.name", query)
        self.assertEqual([r[0] for r in dev["tables"][0]["rows"]], ["Fast", "Middle", "Slow"])
        self.assertEqual(fields(dev)["l2"]["value"], "4 MiB")

    def test_metal_native_memory_is_not_dedicated_vram(self):
        reader = Mock(privileged=False)
        reader.profiler.return_value = [{"sppci_model": "Apple M4 Pro", "spdisplays_vendor": "sppci_vendor_Apple",
            "spdisplays_metalfamily": "spdisplays_metal3", "sppci_bus": "sppci_bus_builtin", "sppci_cores": "20"}]
        metal = [{"name": "Apple M4 Pro", "registry_id": "1001", "unified": True,
                  "recommended_working_set": 32 * 2**30, "max_buffer_length": 16 * 2**30, "families": ["Metal 3"]}]
        with patch("sys.platform", "darwin"), patch("hardware_inspector.mac_metal", return_value=metal), patch("hardware_inspector.find_tool", return_value=None):
            dev = gpu_info(reader)[0]
        data = fields(dev)
        self.assertEqual(dev["id"], "metal:1001")
        self.assertEqual(data["vendor"]["value"], "Apple")
        self.assertEqual(data["bus"]["value"], "integrated")
        self.assertEqual(data["recommended_working_set"]["value"], "32 GiB")
        self.assertEqual(data["vram"]["status"], "not_applicable")
        self.assertEqual(data["shared_memory"]["status"], "dynamic_shared")
        self.assertIsNone(data["vram"]["value"])

    def test_metal_same_name_devices_do_not_cross_merge(self):
        reader = Mock(privileged=False)
        reader.profiler.return_value = [{"sppci_model": "Identical GPU"}] * 2
        rows = [{"name": "Identical GPU", "registry_id": str(i), "unified": False} for i in (1, 2)]
        with patch("sys.platform", "darwin"), patch("hardware_inspector.mac_metal", return_value=rows), patch("hardware_inspector.find_tool", return_value=None):
            devices = gpu_info(reader)
        self.assertEqual(len(devices), 4)
        self.assertNotIn("registry_id", fields(devices[0]))
        self.assertEqual([d["id"] for d in devices[2:]], ["metal:1", "metal:2"])

    def test_unified_dram_unexposed_fields_keep_null_values(self):
        reader = Mock()
        reader.profiler.return_value = [{"SPMemoryDataType": "48 GB", "dimm_type": "LPDDR5", "dimm_manufacturer": "Hynix"}]
        with patch("sys.platform", "darwin"):
            data = fields(dram_info(reader)[1])
        self.assertEqual(data["vendor"]["value"], "Hynix")
        self.assertEqual(data["configured_speed"]["status"], "not_exposed")
        self.assertIsNone(data["configured_speed"]["value"])

    def test_smart_permission_denial_is_not_component_missing_or_health_pass(self):
        reader = Mock()
        reader.plist.side_effect = [{"WholeDisks": ["disk0"]}, {"SMARTStatus": "Verified"}]
        reader.json.side_effect = [({"devices": []}, 0), ({"smartctl": {"messages": [{"string": "Operation not permitted"}]}}, 2)]
        with patch("sys.platform", "darwin"), patch("hardware_inspector.find_tool", return_value="/usr/sbin/smartctl"):
            dev = disk_info(reader)[0]
        self.assertEqual(dev["health"], "smart_unknown")
        self.assertIn("hardware_permission_denied", dev["notices"])
        self.assertNotIn("smartctl_missing", dev["notices"])

    def test_reader_distinguishes_permission_failure_without_exporting_stderr(self):
        reader = Reader()
        with patch("hardware_inspector.subprocess.run", return_value=Mock(returncode=1, stdout=b"", stderr=b"Permission denied: SECRET")):
            reader.run(["/usr/sbin/dmidecode", "--type", "17"])
        self.assertEqual(reader.sources[0]["status"], "permission_denied")
        self.assertNotIn("SECRET", json.dumps(reader.sources))

    def test_authorized_reader_retains_permission_and_missing_component_status(self):
        for raw, code, expected in ((b"Permission denied: SECRET", 1, "permission_denied"),
                                    (b"", -1, "unavailable")):
            reader = Reader(access=lambda *args: (raw, code))
            reader.run(["/usr/sbin/dmidecode", "--type", "17"])
            self.assertEqual(reader.sources[0]["status"], expected)
            self.assertNotIn("SECRET", json.dumps(reader.sources))

    def test_fixed_operation_mapping_does_not_forward_commands_or_write_options(self):
        self.assertEqual(operation(["/usr/sbin/smartctl", "-a", "-j", "-n", "standby", "/dev/disk0"]), "smart /dev/disk0")
        for args in (["smartctl", "-t", "long", "/dev/sda"], ["sh", "-c", "echo bad"],
                     ["smartctl", "-a", "-j", "-n", "standby", "/dev/sda;echo bad"], ["dmidecode", "--dump-bin", "/tmp/data"]):
            self.assertIsNone(operation(args))

    def test_os_authorization_preserves_arguments_and_uses_inspection_mode(self):
        for system in ("darwin", "linux", "win32"):
            args = authorization_command("/tmp/space dir/reader", 1234, "a" * 64, 123, platform=system, inspection=True)
            unpacked = shlex.split(args[-1]) if system == "darwin" else args
            self.assertEqual(unpacked[-1], "--inspect")
            self.assertIn(str(Path("/tmp/space dir/reader").resolve()), unpacked)


class InspectionSessionTests(unittest.TestCase):
    def test_cancel_prevents_even_unelevated_fallback_reads(self):
        access = InspectorAccess()
        access.close()
        reader = Reader(access=access.request)
        with patch("hardware_inspector.subprocess.run") as run, self.assertRaises(InspectionFailure) as error:
            reader.run(["lscpu", "-J"])
        self.assertEqual(error.exception.status, "cancelled")
        run.assert_not_called()

    def test_partial_or_invalid_reply_aborts_session_not_partial_snapshot(self):
        for reply in ([b""], [b"0 2000001\n"], [b"invalid\n"], [b"0 3\n", b"x", b""]):
            peer = Mock()
            # Headers arrive one byte at a time, as on the real socket.
            header, *payload = reply
            peer.recv.side_effect = ([bytes([c]) for c in header] if header else [b""]) + payload
            access = InspectorAccess()
            access.peer = peer
            with self.subTest(reply=reply), self.assertRaises(InspectionFailure) as error:
                Reader(access=access.request).run(["dmidecode", "--type", "17"])
            self.assertEqual(error.exception.status, "error")
            peer.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_transport_timeout_uses_whole_reply_deadline(self):
        access = InspectorAccess()
        access.peer = Mock()
        access.peer.recv.return_value = b"0"
        with patch("hardware_access.time.monotonic", side_effect=[0, 0, .01, .06]), self.assertRaises(InspectionFailure) as error:
            access.request(["dmidecode", "--type", "17"], .05)
        self.assertEqual(error.exception.status, "timeout")
        self.assertTrue(all(0 < call.args[0] <= .05 for call in access.peer.settimeout.call_args_list))
        access.peer.shutdown.assert_called_once()

    def test_native_deadline_does_not_report_success(self):
        access = InspectorAccess()
        access.peer = Mock()
        access.peer.recv.side_effect = [bytes([c]) for c in b"-2 0\n"]
        with self.assertRaises(InspectionFailure) as error:
            access.request(["dmidecode", "--type", "17"], 7)
        self.assertEqual(error.exception.status, "timeout")

    def test_smartctl_health_bitmasks_are_not_bridge_error_codes(self):
        for code in (124, 125, 127):
            access = InspectorAccess()
            access.peer = Mock()
            access.peer.recv.side_effect = [bytes([c]) for c in f"{code} 2\n".encode()] + [b"{}"]
            reader = Reader(access=access.request)
            self.assertEqual(reader.run(["smartctl", "-a", "-j", "-n", "standby", "/dev/disk0"]), (b"{}", code))
            self.assertEqual(reader.sources[0]["status"], "partial")
            access.peer.shutdown.assert_not_called()

    def test_authorization_timeout_is_distinct_and_reaps_launcher(self):
        process = Mock(poll=lambda: None)
        access = InspectorAccess(Path(__file__))
        with patch("hardware_access.AUTHORIZATION_TIMEOUT", .01):
            access.start("cpu", launcher=lambda _: process)
            access.thread.join(timeout=3)
        self.assertFalse(access.thread.is_alive())
        self.assertEqual(access.status, "timeout")
        self.assertIsNone(access.snapshot)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)

    def test_cancel_during_launcher_does_not_resume_collection(self):
        entered, released = threading.Event(), threading.Event()
        process = Mock(poll=lambda: None)
        def launch(_):
            entered.set()
            released.wait(timeout=2)
            return process
        access = InspectorAccess(Path(__file__))
        with patch("hardware_access.collect") as collect:
            access.start("cpu", launcher=launch)
            try:
                self.assertTrue(entered.wait(timeout=2))
                access.close()
            finally:
                released.set()
                access.thread.join(timeout=3)
            collect.assert_not_called()
        self.assertFalse(access.thread.is_alive())
        self.assertEqual(access.status, "cancelled")
        self.assertIsNone(access.snapshot)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)


class NativeInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-inspect-protocol-")
        cls.binary = Path(cls.temp.name) / ("reader.exe" if os.name == "nt" else "reader")
        compiler = shlex.split(os.environ.get("CC", "cc"))
        subprocess.run(compiler + ["-O2", "-std=c11", str(ROOT / "src/telemetry_privileged.c"), "-o", str(cls.binary)] +
                       (["-lws2_32"] if os.name == "nt" else []), check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_native_rejects_commands_paths_and_write_requests_then_exits_on_disconnect(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0)); server.listen(); server.settimeout(3)
            process = subprocess.Popen([str(self.binary), str(server.getsockname()[1]), "a" * 64, str(os.getpid()), "--inspect"])
            try:
                peer, _ = server.accept()
                with peer, peer.makefile("rb") as stream:
                    self.assertEqual(stream.readline(), ("MTINSPECT/1 " + "a" * 64 + "\n").encode())
                    for query in ("sh -c id", "smart /etc/passwd", "smart /dev/sda;id", "smart /dev/nvme0n", "smart /dev/sda -t long"):
                        peer.sendall((query + "\n").encode())
                        self.assertEqual(stream.readline(), b"-1 0\n")
                self.assertEqual(process.wait(timeout=3), 0)
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()

    def test_authorized_transport_collects_without_elevating_test_process(self):
        access = InspectorAccess(self.binary)
        processes = []
        def command(helper, port, token, parent, **_):
            return [str(helper), str(port), token, str(parent), "--inspect"]
        def launch(args):
            processes.append(subprocess.Popen(args))
            return processes[-1]
        try:
            with patch("hardware_access.authorization_command", side_effect=command):
                access.start("cpu", launcher=launch)
                access.thread.join(timeout=25)
            self.assertFalse(access.thread.is_alive())
            self.assertEqual(access.status, "ready")
            self.assertEqual(access.snapshot["kind"], "cpu")
            self.assertTrue(access.snapshot["devices"])
        finally:
            access.close()
            for process in processes:
                if process.poll() is None:
                    process.wait(timeout=3)

    def test_missing_helper_and_denied_authorization_are_distinct(self):
        missing = InspectorAccess(Path(self.temp.name) / "missing")
        missing.start("disk"); missing.thread.join(timeout=3)
        self.assertEqual(missing.status, "unavailable")
        denied = InspectorAccess(self.binary)
        denied.start("disk", launcher=lambda _: Mock(poll=lambda: 1))
        denied.thread.join(timeout=3)
        self.assertEqual(denied.status, "denied")

    def test_connected_reader_failure_cannot_publish_success_snapshot(self):
        access = InspectorAccess(self.binary)
        def command(helper, port, token, parent, **_):
            return [str(helper), str(port), token, str(parent), "--inspect"]
        with patch("hardware_access.authorization_command", side_effect=command), \
                patch("hardware_access.collect", side_effect=InspectionFailure("timeout")):
            access.start("disk", launcher=lambda args: subprocess.Popen(args))
            access.thread.join(timeout=5)
        self.assertFalse(access.thread.is_alive())
        self.assertEqual(access.status, "timeout")
        self.assertIsNone(access.snapshot)
        self.assertIsNone(access.peer)
        self.assertIsNotNone(access.process.poll())

    def test_packaged_inspection_probe_checks_payload_not_source_tree(self):
        spec = importlib.util.spec_from_file_location("artifact_check", ROOT / "packaging/check_artifact.py")
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        with tempfile.TemporaryDirectory(prefix="memorytest-reader-payload-") as directory:
            bundle = Path(directory)
            binary = bundle / "MemoryTest"
            with self.assertRaisesRegex(RuntimeError, "missing"):
                check.check_native_readers(binary)
            engine = bundle / "_internal/bin"
            engine.mkdir(parents=True)
            import shutil
            shutil.copy2(self.binary, engine / ("telemetry_privileged.exe" if os.name == "nt" else "telemetry_privileged"))
            # Metal is checked by actual Mac installer acceptance. This fixture
            # only constructs the cross-platform inspection bridge payload.
            with patch.object(check.sys, "platform", "win32" if os.name == "nt" else "linux"):
                result = check.check_native_readers(binary)
            self.assertEqual(result, {"inspection_protocol": "passed", "os_elevation_requested": False})


if __name__ == "__main__":
    unittest.main()
