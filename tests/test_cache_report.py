"""Bounded real cache pipeline checks; never perform a full scan."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from benchmark_registry import ROOT
from run_benchmarks import validate_report


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
            if mode in {"no_l3", "missing"} and not broken_directory and not full_device:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                path = root / "reports/cache_hierarchy_report.md"
                self.assertIsNone(validate_report(path))
                text = path.read_text()
                self.assertIn("Cache Hierarchy Scan", text)
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
