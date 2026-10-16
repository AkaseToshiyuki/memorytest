"""Bounded real cache pipeline checks; never perform a full scan."""
import os
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from benchmark_registry import ROOT
from run_benchmarks import validate_report
from result_model import build_module


class CacheReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-cache-regression-")
        cls.root = Path(cls.temp.name)
        cls.binary = cls.root / ("cache-regression.exe" if os.name == "nt" else "cache-regression")
        windows = os.name == "nt"
        sources = ["common.c", "platform.c", "report.c", "simd.c", "latency.c",
                   "util_windows.c" if windows else "util.c",
                   "detect_windows.c" if windows else "detect_macos.c" if sys.platform == "darwin" else "detect.c",
                   "pmu_macos.c" if windows or sys.platform == "darwin" else "pmu.c"]
        command = shlex.split(os.environ.get("CC", "cc")) + ["-O2", "-std=c11", "-pthread",
                    "-DBENCH_TARGET_NS=1000000", "-DBENCH_WARMUP_NS=500000",
                    str(ROOT / "tests/cache_regression.c"),
                    *[str(ROOT / "src" / name) for name in sources], "-lm", "-o", str(cls.binary)]
        if windows:
            command.append("-ladvapi32")
        subprocess.run(command, capture_output=True, check=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, mode, broken_directory=False, full_device=False):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            if broken_directory:
                (root / "reports").write_text("not a directory")
            if full_device:
                (root / "reports").mkdir()
                (root / "reports/cache_hierarchy_report.md").symlink_to("/dev/full")
            result = subprocess.run([str(self.binary), mode], cwd=root, capture_output=True,
                                    text=True, timeout=30, stdin=subprocess.DEVNULL)
            if mode in {"no_l3", "missing", "sparse"} and not broken_directory and not full_device:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                path = root / "reports/cache_hierarchy_report.md"
                self.assertIsNone(validate_report(path))
                text = path.read_text()
                for section in ("Cache Bandwidth Test", "Cache Latency Test", "Cache Hierarchy Detection"):
                    self.assertIn(section, text)
                self.assertNotIn("| L3 |", text)
                from result_model import build_module
                from report_view import build_report
                model = build_module("cache_hierarchy", path.parent)
                self.assertEqual(model.status, "passed")
                self.assertTrue(model.charts)
                self.assertTrue(model.tables)
                self.assertIn('<svg', build_report(path.parent, ["cache_hierarchy"]))
            else:
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_no_l3_still_writes_scan(self):
        self.run_case("no_l3")

    def test_independent_modes_only_measure_selected_work(self):
        for mode, name, per_size in (("bandwidth", "cache_bandwidth_test", 3),
                                    ("latency", "cache_latency_test", 1),
                                    ("detection", "cache_detection", 1)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=self.root) as folder:
                run = subprocess.run([str(self.binary), mode + "_only"], cwd=folder,
                                     capture_output=True, text=True, timeout=15)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                reports = Path(folder) / "reports"
                path = reports / f"{name}_report.md"
                self.assertIsNone(validate_report(path))
                audit = json.loads((reports / f"{name}_samples.json").read_text())
                self.assertEqual(len(audit["records"]), 5 * per_size)
                for record in audit["records"]:
                    self.assertEqual("dependent load" in record["measurement"], mode != "bandwidth")
                    self.assertEqual(len(record["elapsed_ns"]), 5)
                    self.assertEqual(record["statistics"]["rejected"], 0)
                text = path.read_text()
                self.assertEqual("## Cache Bandwidth Test" in text, mode == "bandwidth")
                self.assertEqual("## Cache Hierarchy Detection" in text, mode == "detection")
                self.assertEqual(build_module(name, reports).status, "passed")

    def test_latency_and_detection_share_exactly_one_scan(self):
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            run = subprocess.run([str(self.binary), "shared"], cwd=folder,
                                 capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(run.stdout.count("Warming and sampling cache working set:"), 5)
            reports = Path(folder) / "reports"
            self.assertFalse((reports / "cache_bandwidth_test_report.md").exists())
            first = json.loads((reports / "cache_latency_test_samples.json").read_text())
            second = json.loads((reports / "cache_detection_samples.json").read_text())
            self.assertEqual(first, second)
            self.assertEqual(len(first["records"]), 5)
            for name in ("cache_latency_test", "cache_detection"):
                self.assertIn("shared-cache-latency-scan-v1", (reports / f"{name}_report.md").read_text())
                model = build_module(name, reports)
                self.assertIn(("source", "shared_cache_scan"), [(a["key"], b["key"]) for a, b in model.conditions if isinstance(b, dict)])

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires /dev/full")
    def test_raw_sample_write_failure_fails_module(self):
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            reports = Path(folder) / "reports"
            reports.mkdir()
            (reports / "cache_hierarchy_samples.json").symlink_to("/dev/full")
            run = subprocess.run([str(self.binary), "no_l3"], cwd=folder, capture_output=True, timeout=15)
            self.assertNotEqual(run.returncode, 0)

    def test_synthetic_transitions_require_persistence_and_separated_ranges(self):
        result = subprocess.run([str(self.binary), "transitions"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sparse_scan_still_writes_data_without_inference(self):
        self.run_case("sparse")

    def test_missing_cache_metadata_still_writes_scan(self):
        self.run_case("missing")

    def test_allocation_failure_is_not_success(self):
        self.run_case("allocation")

    def test_unknown_memory_budget_fails_closed(self):
        self.run_case("memory")

    def test_report_creation_error_is_not_success(self):
        self.run_case("no_l3", broken_directory=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires /dev/full")
    def test_buffered_report_write_error_is_not_success(self):
        self.run_case("no_l3", full_device=True)


if __name__ == "__main__":
    unittest.main()
