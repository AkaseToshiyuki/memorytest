"""Independent inspector fixtures. No elevation, driver install or disk tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from hardware_inspector import (Reader, collect, cpu_info, device, disk_info, dram_info,
    gpu_info, group, memory_from_dmi, size, smart_device, text)


def fields(dev):
    return {f["name"]: f["value"] for g in dev["groups"] for f in g["fields"]}


def fixture(kind="disk"):
    dev = smart_device({"model_name": "Example NVMe 2 TB", "serial_number": "SECRET-SERIAL",
                       "user_capacity": {"bytes": 2_000_000_000_000}, "smart_status": {"passed": True},
                       "nvme_smart_health_information_log": {"temperature": 38, "power_on_hours": 123,
                           "percentage_used": 2, "available_spare": 100, "media_errors": 0}}, "/dev/example")
    if kind != "disk":
        dev = device("fixture-0", "Example processor / adapter / memory module", [
            group("processor" if kind == "cpu" else "graphics_adapter" if kind == "gpu" else "memory_module",
                  "Independent test fixture; not measured hardware", model="Example device", vendor="Example vendor",
                  capacity="32 GiB", physical=12, logical=24, serial="SECRET-SERIAL", configured_speed=None)])
    return {"schema": 1, "kind": kind, "collected_at": "2026-10-15T00:00:00+00:00",
            "devices": [dev], "notices": ["hardware_readonly"], "sources": []}


class HardwareParserTests(unittest.TestCase):
    def test_complete_structured_smart_counters_logs_and_privacy(self):
        from smart_inventory import smart_tables
        # Shapes and units follow smartmontools 7.5 nvmeprint/ataprint/scsiprint;
        # deliberately exceed the old 512-row truncation boundary.
        exact = "340282366920938463463374607431768211455"
        data = {"serial_number": "SECRET-SERIAL", "wwn": {"id": 123456},
                "nvme_smart_health_information_log": {"data_units_read": 3.402823669209385e38,
                    "data_units_read_s": exact, "data_units_read_le": [255] * 16,
                    "temperature_sensors": [36, None, 42], "vendor_health": {"flag": 0}},
                "scsi_error_counter_log": {"read": {"total_uncorrected_errors": 3}},
                "ata_smart_error_log": {"extended": {"table": [{"error_number": n} for n in range(600)]}}}
        tables = smart_tables(data)
        nvme = {row[0]: row[1:] for row in tables[0]["rows"]}
        self.assertEqual(nvme["data_units_read"], [exact, "512,000 B"])
        self.assertEqual(nvme["temperature_sensors[2]"], [42, "°C"])
        self.assertEqual(nvme["temperature_sensors[1]"], [None, "°C"])
        self.assertEqual(nvme["vendor_health.flag"], [0, None])
        self.assertNotIn("data_units_read_s", nvme)
        extra = dict((row[0], row[1]) for row in tables[1]["rows"])
        self.assertEqual(extra["ata_smart_error_log.extended.table[599].error_number"], 599)
        self.assertEqual(extra["scsi_error_counter_log.read.total_uncorrected_errors"], 3)
        private = tables[1]["private_cells"]
        self.assertEqual([tables[1]["rows"][row][column] for row, column in private], ["SECRET-SERIAL", 123456])

    def test_scsi_only_device_and_standby_are_not_missing_reader(self):
        for payload, notice in [({"scsi_error_counter_log": {"read": {"total_uncorrected_errors": 0}}}, None),
                                ({"power_mode": {"string": "STANDBY"}}, "smart_standby")]:
            reader = Mock()
            reader.plist.side_effect = [{"WholeDisks": ["disk0"]}, {"MediaName": "Fixture SSD"}]
            reader.json.side_effect = [({"devices": []}, 0), (payload, 2)]
            with patch("sys.platform", "darwin"), patch("hardware_inspector.find_tool", return_value="/trusted/smartctl"):
                dev = disk_info(reader)[0]
            self.assertEqual(dev["health"], "smart_unknown")
            if notice:
                self.assertIn(notice, dev["notices"])
            else:
                self.assertTrue(dev["tables"])
                self.assertEqual(fields(dev)["model"], "Fixture SSD")

    def test_missing_values_and_binary_units_are_not_fabricated(self):
        self.assertEqual(size(17_179_869_184), "16 GiB")
        self.assertEqual(size(1_099_511_627_776), "1 TiB")
        for value in (None, "Unknown", "[N/A]", float("nan")):
            self.assertIsNone(text(value))
        self.assertEqual(text(0), "0")
        self.assertIsNone(size(0))

    def test_smart_exit_bitmask_preserves_failed_device_and_rows(self):
        data = {"smartctl": {"exit_status": 8}, "smart_status": {"passed": False},
                "ata_smart_attributes": {"table": [{"id": 197, "name": "Current_Pending_Sector", "value": 5,
                    "worst": 4, "thresh": 1, "raw": {"value": 3, "string": "3"}}]}}
        dev = smart_device(data, "/dev/sda", 8)
        self.assertEqual(dev["health"], "smart_failed")
        self.assertEqual(dev["tables"][0]["rows"][0][:6], ["C5", "Current_Pending_Sector", 5, 4, 1, "3"])
        data["smart_status"]["passed"] = True
        for bits, expected in ((0, "smart_warning"), (2, "smart_unknown"), (4, "smart_unknown"),
                               (16, "smart_failed"), (32, "smart_warning"), (64, "smart_warning"), (128, "smart_warning")):
            data["smartctl"]["exit_status"] = bits
            self.assertEqual(smart_device(data, "/dev/sda")["health"], expected)

    def test_unknown_is_never_green_and_nvme_wear_is_not_remaining_life(self):
        self.assertEqual(smart_device({}, "disk")["health"], "smart_unknown")
        data = {"smart_status": {"passed": True}, "nvme_smart_health_information_log": {
            "percentage_used": 140, "temperature": 39, "power_on_hours": 4321, "media_errors": 0}}
        dev = smart_device(data, "disk")
        self.assertEqual(dev["health"], "smart_warning")
        self.assertEqual(fields(dev)["percentage_used"], "140 %")
        self.assertEqual(fields(dev)["temperature"], "39 °C")
        self.assertNotIn("remaining_life", fields(dev))
        data["nvme_smart_health_information_log"]["critical_warning"] = 1
        self.assertEqual(smart_device(data, "disk")["health"], "smart_failed")

    def test_dmi_slot_identity_units_and_provenance(self):
        raw = """Handle 0x1, DMI type 17, 84 bytes
Memory Device
    Size: 16 GB
    Locator: DIMM_A1
    Bank Locator: BANK 0
    Type: DDR4
    Manufacturer: Example
    Part Number: PART-16G
    Serial Number: TEST-ONLY
    Speed: 3200 MT/s
    Configured Memory Speed: 2666 MT/s
    Data Width: 64 bits
    Total Width: 72 bits
Handle 0x2, DMI type 17, 84 bytes
Memory Device
    Size: No Module Installed
    Locator: DIMM_A2
"""
        devs = memory_from_dmi(raw)
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]["id"], "DIMM_A1")
        self.assertEqual(fields(devs[0])["rated_speed"], "3200 MT/s")
        self.assertEqual(fields(devs[0])["configured_speed"], "2666 MT/s")
        self.assertIn("firmware_not_spd", devs[0]["notices"])

    def test_mac_unified_memory_and_gpu_do_not_invent_spd_or_vram(self):
        reader = Mock()
        reader.profiler.return_value = [{"_name": "Memory", "SPMemoryDataType": "24 GB",
                                         "dimm_type": "LPDDR5", "dimm_manufacturer": "Example"}]
        with patch("sys.platform", "darwin"):
            devs = dram_info(reader)
            self.assertEqual(fields(devs[1])["capacity"], "24 GB")
            self.assertIsNone(fields(devs[0])["active_timings"])
            reader.profiler.return_value = [{"sppci_model": "Apple M4 Pro", "sppci_cores": "20", "spdisplays_metal": "Metal 3"}]
            with patch("hardware_inspector.find_tool", return_value=None):
                gpu = gpu_info(reader)[0]
            self.assertIsNone(fields(gpu)["vram"])
            self.assertEqual(fields(gpu)["gpu_cores"], "20")

    def test_mac_partial_sysctl_keeps_valid_keys(self):
        reader = Mock()
        reader.run.return_value = (b"machdep.cpu.brand_string: Apple M4 Pro\nhw.l2cachesize: 16777216\nhw.perflevel0.name: Performance\nhw.perflevel0.physicalcpu: 10\n", 1)
        with patch("sys.platform", "darwin"):
            cpu = cpu_info(reader)[0]
        self.assertEqual(cpu["name"], "Apple M4 Pro")
        self.assertEqual(fields(cpu)["l2"], "16 MiB")
        self.assertIsNone(fields(cpu)["nominal_frequency"])
        self.assertEqual(cpu["tables"][0]["rows"], [["Performance", "10", None, None, None, None, None, None, None]])

    def test_windows_memory_type_and_no_wrapped_vram(self):
        reader = Mock()
        reader.powershell.return_value = [{"DeviceLocator": "DIMM 0", "Capacity": 32 * 2**30,
            "SMBIOSMemoryType": 34, "ConfiguredClockSpeed": 5600, "Speed": 6400}]
        with patch("sys.platform", "win32"):
            devs = dram_info(reader)
            self.assertEqual(fields(devs[1])["memory_type"], "DDR5")
            self.assertEqual(fields(devs[1])["capacity"], "32 GiB")
            self.assertEqual(fields(devs[1])["configured_speed"], "5600 MHz (CIM)")
            self.assertIsNone(fields(devs[1])["rated_speed"])
            reader.powershell.return_value = [{"DeviceLocator": "DIMM 0", "ConfiguredClockSpeed": 0, "ConfiguredVoltage": 0}]
            unknown = fields(dram_info(reader)[1])
            self.assertIsNone(unknown["configured_speed"])
            self.assertIsNone(unknown["configured_voltage"])
            reader.powershell.return_value = [{"Name": "Example GPU", "PNPDeviceID": "PCI/test", "AdapterRAM": 0xFFFFFFFF}]
            with patch("hardware_inspector.find_tool", return_value=None):
                gpu = gpu_info(reader)[0]
            self.assertIsNone(fields(gpu)["vram"])
            self.assertIn("wmi_vram_note", gpu["notices"])

    def test_smart_commands_are_readonly_and_partial_json_is_kept(self):
        reader = Mock()
        reader.plist.side_effect = [{"WholeDisks": ["disk0", "bad;injected"]},
                                    {"MediaName": "Disk", "SMARTStatus": "Verified", "TotalSize": 500_000_000_000}]
        reader.json.side_effect = [({"devices": []}, 0), ({"smart_status": {"passed": False}}, 8)]
        with patch("sys.platform", "darwin"), patch("hardware_inspector.find_tool", return_value="/trusted/smartctl"):
            devs = disk_info(reader)
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]["health"], "smart_failed")
        self.assertEqual(reader.json.call_args_list[1].args[0], ["/trusted/smartctl", "-x", "--json=v", "-n", "standby", "/dev/disk0"])
        self.assertEqual(reader.plist.call_count, 2)

    def test_same_name_gpus_do_not_share_other_devices_measurements(self):
        reader = Mock()
        reader.powershell.return_value = [{"Name": "NVIDIA Example", "PNPDeviceID": "PCI/A"},
                                         {"Name": "NVIDIA Example", "PNPDeviceID": "PCI/B"}]
        reader.run.return_value = (b"NVIDIA Example, 00000000:01:00.0, 1.0, 8192, 100, 30, 0, 100, 200, 10\n"
                                  b"NVIDIA Example, 00000000:02:00.0, 1.0, 8192, 200, 40, 0, 300, 400, 20\n", 0)
        with patch("sys.platform", "win32"), patch("hardware_inspector.find_tool", return_value="nvidia-smi"):
            devs = gpu_info(reader)
        self.assertEqual(len(devs), 4)
        self.assertNotIn("temperature", fields(devs[0]))
        self.assertNotIn("temperature", fields(devs[1]))
        self.assertEqual(fields(devs[2])["temperature"], "30 °C")
        self.assertEqual(fields(devs[3])["temperature"], "40 °C")

    def test_os_disk_ok_without_smart_is_unknown(self):
        reader = Mock()
        reader.plist.side_effect = [{"WholeDisks": ["disk0"]}, {"MediaName": "Disk", "SMARTStatus": "Verified"}]
        with patch("sys.platform", "darwin"), patch("hardware_inspector.find_tool", return_value=None):
            dev = disk_info(reader)[0]
        self.assertEqual(dev["health"], "smart_unknown")
        self.assertEqual(fields(dev)["smart_os_status"], "Verified")
        self.assertIn("smartctl_missing", dev["notices"])

    def test_reader_bounds_time_and_does_not_request_shell_or_elevation(self):
        reader = Reader(budget=0)
        with patch("hardware_inspector.subprocess.run") as run:
            self.assertEqual(reader.run(["sysctl", "-n", "hw.model"]), (b"", None))
            run.assert_not_called()
        reader = Reader()
        with patch("hardware_inspector.subprocess.run", side_effect=subprocess.TimeoutExpired("tool", 1)) as run:
            reader.run(["tool", "--read"])
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 7)
            self.assertEqual(reader.sources[-1]["status"], "timeout")


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QByteArray, QProcess, QPoint
    from PySide6.QtWidgets import QApplication, QLabel, QTabWidget, QTableWidget
    from desktop_hardware import public_snapshot
    from desktop_window import MainWindow
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "Qt desktop dependencies")
class HardwarePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memorytest-inspector-")
        self.addCleanup(self.temp.cleanup)
        privilege = patch("desktop_window.is_administrator", return_value=False)
        privilege.start()
        self.addCleanup(privilege.stop)
        self.window = MainWindow(autodetect=False, output_dir=self.temp.name, restore=False)
        self.addCleanup(self.window.close)

    def seed(self, page):
        raw = QByteArray(json.dumps(fixture(page.kind)).encode())
        with patch.object(page.process, "readAllStandardOutput", return_value=raw):
            page.finished(0)

    def test_four_lazy_tools_and_fixture_snapshot_remains_readonly(self):
        self.assertEqual(self.window.tools_tabs.count(), 5)
        self.assertEqual(list(self.window.hardware_tools), ["cpu", "gpu", "dram", "disk"])
        for page in self.window.hardware_tools.values():
            self.assertEqual(page.process.state(), QProcess.NotRunning)
            self.assertIsNone(page.snapshot)
            self.seed(page)
            self.assertTrue(page.export_button.isEnabled())
        self.assertFalse(self.window.stress_page.active)
        self.assertEqual(self.window.stress_page.process.state(), QProcess.NotRunning)

    def test_serial_privacy_applies_to_display_clipboard_and_export(self):
        page = self.window.hardware_tools["disk"]
        self.seed(page)
        self.assertNotIn("SECRET-SERIAL", page.serialized())
        self.assertNotIn("smart_not_guarantee", page.serialized())
        self.assertNotIn("hardware_readonly", page.serialized())
        self.assertNotIn("SECRET-SERIAL", " ".join(x.text() for x in page.findChildren(QLabel)))
        page.copy_snapshot()
        self.assertNotIn("SECRET-SERIAL", QApplication.clipboard().text())
        destination = Path(self.temp.name) / "snapshot.json"
        with patch("desktop_hardware.QFileDialog.getSaveFileName", return_value=(str(destination), "JSON")):
            page.export_snapshot()
        self.assertNotIn("SECRET-SERIAL", destination.read_text())
        page.serials.setChecked(True)
        self.assertIn("SECRET-SERIAL", page.serialized())
        self.assertEqual(fields(page.snapshot["devices"][0])["serial"], "SECRET-SERIAL")

    def test_disk_basic_tabs_and_complete_lower_tables_are_visible(self):
        from benchmark_registry import ROOT
        page = self.window.hardware_tools["disk"]
        nvme = {"critical_warning": 0, "temperature": 38, "available_spare": 100, "available_spare_threshold": 10,
            "percentage_used": 2, "data_units_read": 123456789, "data_units_written": 98765432,
            "host_reads": 12345678, "host_writes": 9876543, "controller_busy_time": 987,
            "power_cycles": 124, "power_on_hours": 4321, "unsafe_shutdowns": 3, "media_errors": 0,
            "num_err_log_entries": 0, "warning_temp_time": 0, "critical_comp_time": 0,
            "temperature_sensors": [38, 42]}
        dev = smart_device({"model_name": "NVMe fixture — complete structured SMART", "serial_number": "SECRET-SERIAL",
            "firmware_version": "TEST-1", "user_capacity": {"bytes": 2_000_000_000_000},
            "device": {"protocol": "NVMe"}, "smart_status": {"passed": True},
            "nvme_smart_health_information_log": nvme}, "/dev/disk0")
        page.buffer = bytearray(json.dumps(dict(fixture(), devices=[dev])).encode())
        page.finished(0)
        with patch.object(self.window.stress_page, "start_preview"), patch.object(self.window.stress_page.probe, "start"):
            self.window.show()
            self.window.tools_button.click()
        self.window.tools_tabs.setCurrentIndex(4)
        self.window.resize(880, 720)
        evidence = ROOT / "build/ui-revision"
        evidence.mkdir(parents=True, exist_ok=True)
        for language in ("zh", "en"):
            self.window.language_combo.setCurrentIndex(self.window.language_combo.findData(language))
            self.app.processEvents()
            root = page.scroll.widget()
            basic = root.findChild(QTabWidget, "disk_basic_tabs")
            detail = root.findChild(QTabWidget, "disk_detail_tabs")
            self.assertEqual(basic.count(), 2)
            self.assertEqual(detail.count(), 2)
            self.assertEqual(detail.widget(0).rowCount(), 19)
            self.assertEqual(page.scroll.horizontalScrollBar().maximum(), 0)
            self.assertTrue(detail.isVisible())
            self.assertLess(detail.mapTo(page, QPoint(0, 0)).y() + 100, page.height())
            self.assertNotIn("SECRET-SERIAL", page.serialized())
            for view in root.findChildren(QTableWidget):
                self.assertNotIn("SECRET-SERIAL", " ".join(view.item(r, c).text() for r in range(view.rowCount())
                    for c in range(view.columnCount()) if view.item(r, c)))
            self.window.grab().save(str(evidence / f"smart-complete-{language}.png"))
        basic.setCurrentIndex(1)
        detail.setCurrentIndex(1)
        page.render()
        self.assertEqual(page.scroll.widget().findChild(QTabWidget, "disk_basic_tabs").currentIndex(), 1)
        self.assertEqual(page.scroll.widget().findChild(QTabWidget, "disk_detail_tabs").currentIndex(), 1)
        page.snapshot["devices"][0]["tables"] = []
        page.snapshot["devices"][0]["notices"] = ["hardware_permission_denied"]
        page.render()
        self.assertEqual(page.scroll.widget().findChild(QTabWidget, "disk_detail_tabs").widget(0).rowCount(), 0)
        self.assertIn("hardware_permission_denied", page.serialized())

    def test_parameter_pages_contain_no_methodology_or_working_commentary(self):
        for page in self.window.hardware_tools.values():
            self.seed(page)
        for language in ("zh", "en"):
            self.window.language = language
            self.window.retranslate()
            self.app.processEvents()
            for page in self.window.hardware_tools.values():
                visible_text = " ".join(item.text() for item in page.scroll.widget().findChildren(QLabel))
                for phrase in ("不安装驱动", "不提权", "不修改硬件", "不保证", "不转换", "不从型号",
                               "不是直接读取", "不代表", "数据来源", "Local reads only", "not inferred",
                               "not guarantee", "No driver installation", "Source:"):
                    self.assertNotIn(phrase, visible_text)
            page = self.window.hardware_tools["disk"]
            page.snapshot["devices"][0]["notices"] = ["smartctl_missing"]
            page.render()
            self.assertIn("未安装 SMART 读取组件" if language == "zh" else "SMART reader not installed",
                          " ".join(item.text() for item in page.scroll.widget().findChildren(QLabel)))

    def test_failed_refresh_discards_stale_health_and_recovers_controls(self):
        page = self.window.hardware_tools["disk"]
        self.seed(page)
        with patch.object(page.process, "start") as start:
            page.refresh()
            self.assertIsNone(page.snapshot)
            self.assertFalse(page.export_button.isEnabled())
            self.assertIn("--hardware-tool", start.call_args.args[1])
        page.process_error(QProcess.FailedToStart)
        self.assertTrue(page.refresh_button.isEnabled())
        self.assertFalse(page.export_button.isEnabled())
        self.assertFalse(page.deadline.isActive())

    def test_admin_startup_automatically_reads_with_inherited_privileges(self):
        page = self.window.hardware_tools["disk"]
        self.window.administrator = True
        self.seed(page)
        access = Mock(status="starting", snapshot=None)
        with patch("desktop_hardware.InspectorAccess", return_value=access):
            page.refresh()
        access.start.assert_called_once_with("disk")
        self.assertIsNone(page.snapshot)
        self.assertFalse(hasattr(page, "admin_button"))
        self.assertFalse(page.refresh_button.isEnabled())
        self.assertTrue(page.cancel_button.isEnabled())
        access.status = "reading"
        page.poll_access()
        self.assertEqual(page.status_key, "hardware_reading")
        access.status = "ready"
        access.snapshot = dict(fixture(), access="administrator")
        page.poll_access()
        self.assertEqual(page.snapshot["access"], "administrator")
        self.assertEqual(page.status_key, "hardware_snapshot")
        self.assertTrue(page.refresh_button.isEnabled())
        self.assertFalse(page.access_poll.isActive())
        denied = Mock(status="denied", snapshot=None)
        with patch("desktop_hardware.InspectorAccess", return_value=denied):
            page.refresh()
        page.poll_access()
        self.assertEqual(page.status_key, "hardware_read_failed")
        self.assertIsNone(page.snapshot)
        self.assertFalse(page.export_button.isEnabled())
        timed_out = Mock(status="timeout", snapshot=None)
        with patch("desktop_hardware.InspectorAccess", return_value=timed_out):
            page.refresh()
        page.poll_access()
        self.assertEqual(page.status_key, "timeout")
        self.assertIsNone(page.snapshot)
        self.assertFalse(page.export_button.isEnabled())
        waiting = Mock(status="starting", snapshot=None)
        with patch("desktop_hardware.InspectorAccess", return_value=waiting):
            page.refresh()
        page.cancel()
        waiting.close.assert_called_once()
        self.assertIsNone(page.access)
        self.assertTrue(page.refresh_button.isEnabled())

    def test_standard_startup_never_starts_privileged_reader_or_exposes_mode_controls(self):
        from PySide6.QtWidgets import QAbstractButton
        for page in self.window.hardware_tools.values():
            with patch("desktop_hardware.InspectorAccess") as access, patch.object(page.process, "start") as start:
                page.refresh()
                access.assert_not_called()
                start.assert_called_once()
            page.cancel()
            self.assertFalse(hasattr(page, "admin_button"))
        for language in ("zh", "en"):
            self.window.language = language
            self.window.retranslate()
            for button in self.window.findChildren(QAbstractButton):
                self.assertNotRegex(button.text() + button.toolTip(),
                    r"管理员|增强监控|详细模式|重新授权|(?i:administrator|enhanced monitoring|detailed mode|authorize)")

    def test_admin_source_mode_without_native_helper_uses_current_token_worker(self):
        page = self.window.hardware_tools["cpu"]
        self.window.administrator = True
        access = Mock()
        access.helper.is_file.return_value = False
        with patch("desktop_hardware.InspectorAccess", return_value=access), patch.object(page.process, "start") as start:
            page.refresh()
        access.start.assert_not_called()
        start.assert_called_once()
        self.assertIn("--hardware-tool", start.call_args.args[1])
        page.cancel()

    def test_missing_reason_is_visible_and_unified_memory_is_not_a_number(self):
        page = self.window.hardware_tools["gpu"]
        for language in ("zh", "en"):
            self.window.language = language
            self.assertEqual(page.value({"value": None, "status": "not_applicable"}), "不适用" if language == "zh" else "Not applicable")
            self.assertEqual(page.value({"value": None, "status": "not_exposed"}), "系统未提供此项" if language == "zh" else "Not exposed by OS")

    def test_output_cap_cancellation_and_bad_json(self):
        page = self.window.hardware_tools["cpu"]
        with patch.object(page.process, "readAllStandardOutput", return_value=QByteArray(b"x" * 2_000_001)):
            page.read_output()
        self.assertEqual(len(page.buffer), 0)
        self.assertEqual(page.stop_reason, "hardware_read_failed")
        page.stop_reason = None
        page.buffer = bytearray(b"not json")
        page.finished(0)
        self.assertIsNone(page.snapshot)

    def test_all_tools_compact_bilingual_themes_and_device_selection(self):
        from benchmark_registry import ROOT
        evidence = ROOT / "build/ui-revision"
        evidence.mkdir(parents=True, exist_ok=True)
        for page in self.window.hardware_tools.values():
            self.seed(page)
        with patch.object(self.window.stress_page, "start_preview"), patch.object(self.window.stress_page.probe, "start"):
            self.window.show()
            self.window.tools_button.click()
        for width, height in ((880, 720), (1120, 850)):
            self.window.resize(width, height)
            for language in ("zh", "en"):
                for theme in ("dark", "light"):
                    self.window.language_combo.setCurrentIndex(self.window.language_combo.findData(language))
                    self.window.theme_combo.setCurrentIndex(self.window.theme_combo.findData(theme))
                    for index, (kind, page) in enumerate(self.window.hardware_tools.items(), 1):
                        self.window.tools_tabs.setCurrentIndex(index)
                        self.app.processEvents()
                        self.assertEqual(page.selector.currentData(), page.snapshot["devices"][0]["id"])
                        for widget in (page.refresh_button, page.export_button, page.serials, page.selector):
                            self.assertTrue(self.window.rect().contains(widget.mapTo(self.window, QPoint(0, 0))))
                            self.assertTrue(self.window.rect().contains(widget.mapTo(self.window, widget.rect().bottomRight())))
                        self.assertEqual(page.scroll.horizontalScrollBar().maximum(), 0)
                        self.window.grab().save(str(evidence / f"tool-{kind}-{width}-{language}-{theme}.png"))

    def test_apple_style_full_fields_and_missing_smart_remain_compact(self):
        from benchmark_registry import ROOT
        evidence = ROOT / "build/hardware-repair-reviewed"
        evidence.mkdir(parents=True, exist_ok=True)
        reader = Mock(privileged=False)
        reader.run.return_value = (b"machdep.cpu.brand_string: Apple Fixture\nhw.nperflevels: 2\n"
            b"hw.optional.arm.FEAT_AES: 1\nhw.optional.arm.FEAT_SHA256: 1\nhw.optional.arm.FEAT_SME: 1\n"
            b"hw.perflevel0.name: Performance\nhw.perflevel0.physicalcpu: 10\nhw.perflevel0.logicalcpu: 10\n"
            b"hw.perflevel0.l1icachesize: 196608\nhw.perflevel0.l1dcachesize: 131072\n"
            b"hw.perflevel0.l2cachesize: 16777216\nhw.perflevel0.cpusperl2: 5\n"
            b"hw.perflevel1.name: Efficiency\nhw.perflevel1.physicalcpu: 4\n", 0)
        with patch("sys.platform", "darwin"), patch("hardware_inspector.find_tool", return_value=None), \
                patch("hardware_inspector.platform.machine", return_value="arm64"), \
                patch("hardware_inspector.platform.system", return_value="Darwin"), \
                patch("hardware_inspector.platform.release", return_value="fixture"), \
                patch("hardware_inspector.psutil.cpu_count", return_value=14):
            snapshots = {"cpu": cpu_info(reader)}
            reader.profiler.return_value = [{"sppci_model": "Apple Fixture", "sppci_cores": "20",
                "spdisplays_vendor": "sppci_vendor_Apple", "spdisplays_metalfamily": "spdisplays_metal3"}]
            with patch("hardware_inspector.mac_metal", return_value=[{"name": "Apple Fixture", "registry_id": "123",
                    "unified": True, "recommended_working_set": 32 * 2**30, "max_buffer_length": 16 * 2**30,
                    "families": ["Metal 3", "Mac 2"]}]):
                snapshots["gpu"] = gpu_info(reader)
            reader.profiler.return_value = [{"SPMemoryDataType": "48 GB", "dimm_type": "LPDDR5", "dimm_manufacturer": "Fixture"}]
            snapshots["dram"] = dram_info(reader)[1:]
            reader.plist.side_effect = [{"WholeDisks": ["disk0"]}, {"MediaName": "Fixture SSD", "SMARTStatus": "Verified",
                "TotalSize": 1_000_000_000_000, "BusProtocol": "PCI-Express", "SolidState": True}]
            snapshots["disk"] = disk_info(reader)
        for kind, page in self.window.hardware_tools.items():
            page.buffer = bytearray(json.dumps(dict(fixture(kind), devices=snapshots[kind])).encode())
            page.finished(0)
        with patch.object(self.window.stress_page, "start_preview"), patch.object(self.window.stress_page.probe, "start"):
            self.window.show()
            self.window.tools_button.click()
        self.window.resize(880, 720)
        for language in ("zh", "en"):
            self.window.language_combo.setCurrentIndex(self.window.language_combo.findData(language))
            for index, (kind, page) in enumerate(self.window.hardware_tools.items(), 1):
                self.window.tools_tabs.setCurrentIndex(index)
                self.app.processEvents()
                self.assertEqual(page.scroll.horizontalScrollBar().maximum(), 0)
                self.assertFalse(hasattr(page, "admin_button"))
                self.window.grab().save(str(evidence / f"{kind}-{language}.png"))
            text = " ".join(item.text() for item in self.window.hardware_tools["disk"].findChildren(QLabel))
            self.assertNotIn("SMART 检查通过" if language == "zh" else "SMART passed", text)

    def test_real_worker_contract_uses_cpu_inventory_only(self):
        page = self.window.hardware_tools["cpu"]
        page.refresh()
        # The real worker has a 30-second inventory budget and the page a
        # 35-second watchdog. Do not fail before either production bound.
        deadline = time.monotonic() + 40
        while page.process.state() != QProcess.NotRunning and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.app.processEvents()
        self.assertEqual(page.process.state(), QProcess.NotRunning)
        self.assertIsNotNone(page.snapshot)
        self.assertEqual(page.snapshot["kind"], "cpu")
        self.assertTrue(page.snapshot["devices"])
        self.assertFalse(self.window.stress_page.active)


if __name__ == "__main__":
    unittest.main()
