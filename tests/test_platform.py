"""Portability acceptance. Darwin checks require an actual macOS host/SDK."""
import json
import os
import platform
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from benchmark_registry import ROOT
from run_benchmarks import environment_snapshot


class PlatformContractTests(unittest.TestCase):
    def test_performance_levels_are_enumerated_not_assumed_to_be_two(self):
        def sysctl(command, **kwargs):
            key = command[-1]
            values = {"hw.nperflevels": "3", "hw.perflevel0.name": "Performance",
                      "hw.perflevel1.name": "Efficiency", "hw.perflevel2.name": "Future tier"}
            return subprocess.CompletedProcess(command, 0 if key in values else 1, values.get(key, ""), "")
        with patch("run_benchmarks.platform.system", return_value="Darwin"), \
             patch("run_benchmarks.subprocess.run", side_effect=sysctl):
            data = environment_snapshot()
        self.assertEqual([level["name"] for level in data["macos_perflevels"]], ["Performance", "Efficiency", "Future tier"])
        self.assertIsNone(data["macos_perflevels"][2]["physicalcpu"])
        self.assertIsNone(data["allowed_cpus"])

    def test_darwin_metadata_is_read_only_and_missing_values_stay_unknown(self):
        def sysctl(command, **kwargs):
            self.assertEqual(command[:2], ["/usr/sbin/sysctl", "-n"])
            self.assertNotIn("shell", kwargs)
            return subprocess.CompletedProcess(command, 0 if command[-1] == "hw.logicalcpu" else 1,
                                               "8\n" if command[-1] == "hw.logicalcpu" else "", "")
        with patch("run_benchmarks.platform.system", return_value="Darwin"), \
             patch("run_benchmarks.subprocess.run", side_effect=sysctl):
            data = environment_snapshot()
        self.assertEqual(data["macos_sysctl"], {"hw.logicalcpu": "8"})
        self.assertIn("unavailable", data["cpu_placement"])
        self.assertNotIn("cgroup_membership", data)


@unittest.skipUnless(sys.platform == "darwin", "requires native macOS and Apple SDK")
class MacOSNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-macos-")
        cls.path = Path(cls.temp.name)
        cls.cc = shlex.split(os.environ.get("CC", "cc"))
        cls.bin = cls.path / "bin"
        # Report/API acceptance does not need production-length performance
        # samples. Keep all 20 samples and the real kernels/validation, but
        # shorten timing only in this temporary test build.
        build = subprocess.run(["make", f"BUILD_DIR={cls.bin}",
                               "CFLAGS=-O2 -std=c11 -pthread -DBENCH_TARGET_NS=1000000 -DBENCH_WARMUP_NS=500000",
                               "tests"], cwd=ROOT,
                               capture_output=True, text=True)
        if build.returncode:
            raise RuntimeError(build.stdout + build.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_detection_and_bounded_cache_eviction(self):
        binary = self.path / "platform-probe"
        common = ["common.c", "util.c", "platform.c", "detect_macos.c", "report.c", "simd.c", "pmu_macos.c", "latency.c"]
        subprocess.run([*self.cc, "-O2", "-std=c11", "-pthread", str(ROOT / "tests/macos_platform.c"),
                        *[str(ROOT / "src" / name) for name in common], "-lm", "-o", str(binary)],
                       capture_output=True, check=True)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        reference = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True).strip()
        self.assertEqual(values["MEMORY_BYTES"], reference)
        self.assertEqual(values["CACHE_L2"], values["CACHE_L2_API"])
        self.assertEqual(values["CACHE_L3"], values["CACHE_L3_API"])

    def test_unsupported_intercore_has_a_report_but_no_matrix(self):
        if platform.machine().lower() in {"arm64", "aarch64"}:
            from intercore_observed import read_observed
            work = self.path / "observed"; work.mkdir()
            run = subprocess.run([str(self.bin / "test_inter_core"), "--budget-ms", "2000", "--workers", "2"],
                                 cwd=work, capture_output=True, text=True, timeout=8)
            self.assertIn(run.returncode, (0, 77), run.stdout + run.stderr)
            self.assertFalse((work / "reports/inter_core_heatmap_data.json").exists())
            if run.returncode == 0:
                data = json.loads((work / "reports/inter_core_observed.json").read_text())
                self.assertGreater(read_observed(data)["covered_pairs"], 0)
                self.assertTrue((work / "reports/inter_core_latency_report.md").is_file())
            else:
                self.assertIn("UNSUPPORTED", run.stderr)
            return
        output = self.path / "unsupported"
        run = subprocess.run([sys.executable, str(ROOT / "run_benchmarks.py"), "-m", "inter_core",
                              "--bin-dir", str(self.bin), "--output-dir", str(output),
                              "--skip-build", "--skip-charts"], capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 77, run.stdout + run.stderr)
        manifest = json.loads(next((output / "runs").glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["modules"][0]["status"], "unsupported")
        self.assertTrue((output / "inter_core_report.html").is_file())

    def test_bounded_memory_report_uses_no_invented_cache_totals(self):
        work = self.path / "memory"; work.mkdir()
        run = subprocess.run([str(self.bin / "test_memory_bandwidth"), "-s", "1M", "-t", "2"],
                             cwd=work, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = (work / "reports/memory_bandwidth_report.md").read_text()
        self.assertIn("sysctl summary; may be cluster-shared", report)
        self.assertIn("Unsupported via public APIs", report)
        self.assertNotIn("per core,", report)
        self.assertIn("- Samples: 20", report)
        self.assertIn("- Sample target: 1 ms", report)
        # Match non-finite numeric tokens, not words such as "inferred".
        self.assertNotRegex(report, r"(?i)\b(?:nan|inf(?:inity)?)\b")

    def test_partial_thread_creation_cleanup(self):
        if (os.cpu_count() or 1) < 2: self.skipTest("needs 2 online CPUs")
        from compute_score import CLANG_FLAGS
        binary = self.path / "partial-creation"
        subprocess.run([*self.cc, *shlex.split(CLANG_FLAGS), str(ROOT / "tests/compute_creation_failure.c"),
                        str(ROOT / "src/compute_kernel.c"), "-lm", "-o", str(binary)], check=True, capture_output=True)
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
