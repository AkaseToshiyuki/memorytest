"""Independent numerical references + protocol/failure checks; <=2 CPUs native."""
import copy
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile
import unittest

from benchmark_registry import ROOT
from compute_score import FLAGS, CLANG_FLAGS, KERNEL_HASH, PROFILE, WORKLOAD, compute_mark_score


def fixture(threads=2):
    data = {"schema_version": 2, "version": "v2", "workload": WORKLOAD,
            "profile": PROFILE, "build_flags": FLAGS, "source_hash": KERNEL_HASH,
            "compiler": "reference", "architecture": "fixture", "timer": "CLOCK_MONOTONIC",
            "selftest_passed": True, "affinity_verified": True,
            "threads": threads, "available_cpus": list(range(threads)), "records": []}
    # Independent exact example: integer 24 Gop/s, FP64 12 GFLOP/s per thread.
    for kind, operations in [("integer", 24), ("fp64", 12)]:
        for n in sorted({1, threads}):
            data["records"].append({"workload": kind, "threads": n,
                                    "iterations_per_thread": 100_000_000,
                                    "operations_per_iteration": operations,
                                    "samples": [{"elapsed_ns": 100_000_000,
                                                 "operations": 100_000_000 * operations * n,
                                                 "checksum": "123456789abcdef0"} for _ in range(5)]})
    return data


class ScoreContractTests(unittest.TestCase):
    def test_windows_pinned_group_and_qpc_contract(self):
        data = fixture()
        data.update(os="Windows", timer="QueryPerformanceCounter", placement_policy="windows-pinned-process-group",
                    placement_verified=True, available_cpus=[64, 66], available_threads=2)
        score = compute_mark_score(data)
        self.assertEqual(score.scope, "process-group")
        self.assertEqual(score.cpu_ids, [64, 66])
        self.assertEqual(score.points, 33941)
        for key, value in [("timer", "CLOCK_MONOTONIC"), ("os", "Linux"),
                           ("placement_verified", False), ("available_cpus", [1, 65])]:
            bad = copy.deepcopy(data)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                compute_mark_score(bad)

    def test_macos_html_displays_topology_and_unpinned_policy(self):
        data = fixture()
        data.update(compiler="Apple clang test", build_flags=CLANG_FLAGS, os="Darwin",
                    placement_policy="macos-qos-user-initiated", placement_verified=True,
                    translated=False, affinity_verified=False, available_cpus=None, available_threads=2)
        with tempfile.TemporaryDirectory(prefix="memorytest-mac-html-") as directory:
            root = Path(directory)
            (root / "compute_mark_v2.json").write_text(json.dumps(data))
            (root / "cpu_multi_core_report.md").write_text(
                "# cpu_multi_core Test Report\n\n## System Configuration\n\n"
                "| Item | Value |\n|---|---|\n| CPU Model | Apple fixture |\n\n"
                "## macOS CPU performance levels\n\n"
                "| Level | OS name | Physical CPUs | Logical CPUs | L1D KiB | L2 KiB | CPUs sharing L2 |\n"
                "|---|---|---|---|---|---|---|\n| 0 | Performance | 4 | 4 | 128 | 16384 | 4 |\n"
                "| 1 | Efficiency | 4 | 4 | 64 | 4096 | 4 |\n\n"
                "## Measured workloads\n\n"
                "| Operation | Category | Threads | Time(ms) | Speedup | Efficiency | Status | Rate (Gop/s) |\n"
                "|---|---|---|---|---|---|---|---|\n| U64 xorshift | Integer | 1 | 100 | 1x | 100% | measured | 24 |\n")
            report = root / "result.html"
            result = subprocess.run([sys.executable, str(ROOT / "report_html.py"), "--reports-dir", str(root),
                                     "--module", "cpu_multi", "--skip-charts", "--output", str(report)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            text = report.read_text()
            self.assertIn("OS scheduled (not pinned)", text)
            self.assertIn("macOS CPU performance levels", text)
            self.assertIn("Performance", text)
            self.assertIn("Efficiency", text)
            self.assertIn("33,941", text)
            self.assertNotIn("ComputeMark unavailable", text)

    def test_macos_same_workload_units_without_claimed_cpu_pinning(self):
        data = fixture()
        data.update(compiler="Apple clang test", build_flags=CLANG_FLAGS, os="Darwin",
                    placement_policy="macos-qos-user-initiated", placement_verified=True,
                    translated=False, affinity_verified=False, available_cpus=None, available_threads=2)
        score = compute_mark_score(data)
        self.assertEqual((score.single_points, score.points), (16971, 33941))
        self.assertEqual(score.cpu_ids, [])
        self.assertFalse(score.affinity_verified)
        self.assertEqual(score.scope, "all-online")
        for key, value in [("affinity_verified", True), ("available_cpus", [0, 1]),
                           ("translated", True), ("placement_verified", False),
                           ("os", "Linux"), ("available_threads", 1), ("build_flags", FLAGS)]:
            bad = copy.deepcopy(data); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                compute_mark_score(bad)

    def test_unknown_scheduler_and_translated_results_rejected(self):
        for key, value in [("placement_policy", "hint-only"), ("translated", True)]:
            data = fixture(); data[key] = value
            with self.assertRaises(ValueError): compute_mark_score(data)

    def test_exact_units_and_unbounded_points(self):
        score = compute_mark_score(fixture())
        self.assertEqual(score.single_integer_gops, 24)
        self.assertEqual(score.single_fp64_gflops, 12)
        self.assertEqual(score.integer_gops, 48)
        self.assertEqual(score.fp64_gflops, 24)
        self.assertEqual(score.single_points, 16971)
        self.assertEqual(score.points, 33941)
        self.assertTrue(score.stable)

    def test_median_not_best_or_sum_of_per_thread_rates(self):
        data = fixture()
        for r in data["records"]:
            for sample, ns in zip(r["samples"], [100_000_000, 120_000_000, 130_000_000, 140_000_000, 200_000_000]):
                sample["elapsed_ns"] = ns
        score = compute_mark_score(data)
        self.assertAlmostEqual(score.integer_gops, 48 / 1.3)
        self.assertFalse(score.stable)

    def test_rejects_legacy_extrapolation(self):
        with self.assertRaises(ValueError):
            compute_mark_score({"alu_add_ops_sec": 1e9, "simd_fma_ops_sec": 2e9, "multi_scaling": []})

    def test_contract_fields_and_correctness_are_required(self):
        for key, value in [("version", "v1"), ("profile", "native"), ("build_flags", "-Ofast"),
                           ("source_hash", "unknown"), ("timer", "TSC/MHz"), ("selftest_passed", False),
                           ("affinity_verified", False), ("threads", True), ("compiler", "")]:
            data = fixture(); data[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): compute_mark_score(data)

    def test_malformed_counts_times_and_nondeterminism_rejected(self):
        for field, value in [("elapsed_ns", 0), ("elapsed_ns", 99_999_999), ("elapsed_ns", float("inf")),
                             ("operations", 1), ("operations", float("nan")), ("operations", True),
                             ("checksum", "bad"), ("checksum", "aaaaaaaaaaaaaaaa")]:
            data = fixture(); data["records"][0]["samples"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): compute_mark_score(data)

    def test_missing_or_duplicate_samples_and_records_rejected(self):
        changes = [lambda d: d["records"].pop(),
                   lambda d: d["records"].append(copy.deepcopy(d["records"][0])),
                   lambda d: d["records"][0]["samples"].pop(),
                   lambda d: d["records"][0].update(operations_per_iteration=48),
                   lambda d: d.update(available_cpus=[0, 0])]
        for change in changes:
            data = fixture(); change(data)
            with self.assertRaises(ValueError): compute_mark_score(data)

    def test_one_cpu_and_limited_scope(self):
        one = compute_mark_score(fixture(1))
        self.assertEqual(one.points, one.single_points)
        data = fixture(2); data["available_cpus"] = [2, 4, 6]
        score = compute_mark_score(data)
        self.assertEqual(score.cpu_ids, [2, 4])
        self.assertEqual(score.scope, "limited-2")

    def test_kernel_identity_is_pinned(self):
        self.assertEqual(hashlib.sha256((ROOT / "src/compute_kernel.c").read_bytes()).hexdigest(), KERNEL_HASH)

    def test_cli_only_displays_absolute_current_protocol_points(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compute_mark_v2.json"
            path.write_text(json.dumps(fixture()))
            result = subprocess.run([sys.executable, str(ROOT / "compute_score.py"), "--input", str(path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("33,941 points", result.stdout)
            self.assertNotIn("/100", result.stdout)
            path.write_text('{"version": "v1"}')
            rejected = subprocess.run([sys.executable, str(ROOT / "compute_score.py"), "--input", str(path)],
                                      capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 1)
            self.assertNotIn("points", rejected.stdout)


class NativeKernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-compute-")
        cls.path = Path(cls.temp.name)
        cc = shlex.split(os.environ.get("CC", "cc"))
        cls.cc = cc
        defines = subprocess.run([*cc, "-dM", "-E", "-"], input="", capture_output=True, text=True, check=True).stdout
        cls.flags = CLANG_FLAGS if "__clang__" in defines else FLAGS
        cls.shared_flags = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
        if os.name == "nt":
            (cls.path / "bin").mkdir()
            command = [*cc, *shlex.split(cls.flags), '-DCM_BUILD_PROFILE="portable-scalar-v2"',
                       f'-DCM_BUILD_FLAGS="{cls.flags}"', f'-DCM_SOURCE_HASH="{KERNEL_HASH}"',
                       str(ROOT / "src/test_cpu_multi.c"), str(ROOT / "src/compute_kernel.c"),
                       "-o", str(cls.path / "bin/test_cpu_multi.exe"), "-lm", "-ladvapi32"]
        else:
            command = ["make", f"BUILD_DIR={cls.path / 'bin'}", str(cls.path / "bin/test_cpu_multi")]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        if result.returncode: raise RuntimeError(result.stdout + result.stderr)
        cls.binary = cls.path / ("bin/test_cpu_multi.exe" if os.name == "nt" else "bin/test_cpu_multi")
        subprocess.run([*cc, *shlex.split(cls.flags), *cls.shared_flags, str(ROOT / "src/compute_kernel.c"),
                        "-lm", "-o", str(cls.path / "kernel.so")], check=True, capture_output=True)
        cls.lib = ctypes.CDLL(str(cls.path / "kernel.so"))
        cls.lib.cm_integer.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64)]
        cls.lib.cm_float.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_double)]
        if sys.platform.startswith("linux"):
            subprocess.run([*cc, "-shared", "-fPIC", str(ROOT / "tests/compute_faults.c"), "-ldl", "-pthread",
                            "-o", str(cls.path / "faults.so")], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        if os.name == "nt":
            import _ctypes
            _ctypes.FreeLibrary(cls.lib._handle)
        cls.temp.cleanup()

    def test_integer_kernel_against_python_masked_arithmetic(self):
        mask = (1 << 64) - 1
        for iterations in [0, 1, 2, 64, 4096]:
            expected = [0x123456789abcdef1, 0xfedcba9876543211, 0x3141592653589793, 0x2718281828459045]
            actual = (ctypes.c_uint64 * 4)(*expected)
            for _ in range(iterations):
                for j, x in enumerate(expected):
                    x ^= (x << 13) & mask; x ^= x >> 7; x ^= (x << 17) & mask
                    expected[j] = x
            self.lib.cm_integer(iterations, actual)
            self.assertEqual(list(actual), expected)

    def test_fp_kernel_against_independent_binary64_reference(self):
        for iterations in [0, 1, 2, 17, 64, 1000]:
            expected = [0.125, 0.25, 0.375, 0.625]
            actual = (ctypes.c_double * 4)(*expected)
            for _ in range(iterations): expected = [(x * (1.0 - x)) * 3.75 for x in expected]
            self.lib.cm_float(iterations, actual)
            self.assertEqual([struct.pack("=d", v) for v in actual], [struct.pack("=d", v) for v in expected])
        self.assertEqual(self.lib.cm_selftest(), 1)

    def test_fast_math_build_cannot_pass_selftest(self):
        output = self.path / "unsafe.so"
        subprocess.run([*self.cc, "-O3", "-ffast-math", *self.shared_flags, str(ROOT / "src/compute_kernel.c"),
                        "-lm", "-o", str(output)], check=True, capture_output=True)
        library = ctypes.CDLL(str(output))
        self.assertEqual(library.cm_selftest(), 0)
        if os.name == "nt":
            import _ctypes
            _ctypes.FreeLibrary(library._handle)

    def test_native_default_uses_allowed_cpu_ids_and_writes_recomputable_score(self):
        if not hasattr(os, "sched_getaffinity"): self.skipTest("Linux CPU affinity required")
        available = sorted(os.sched_getaffinity(0))
        chosen = available[::2][:2] if len(available) >= 3 else available[:2]
        work = self.path / "native"; work.mkdir()
        # Test the default all-allowed policy using a deliberately restricted cpuset.
        code = "import os,sys; os.sched_setaffinity(0, " + repr(chosen) + "); os.execv(sys.argv[1], [sys.argv[1]])"
        result = subprocess.run([sys.executable, "-c", code, str(self.binary)], cwd=work,
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads((work / "reports/compute_mark_v2.json").read_text())
        score = compute_mark_score(data)
        self.assertEqual(data["available_cpus"], chosen)
        self.assertEqual(score.threads, len(chosen))
        self.assertEqual(score.scope, "all-allowed")
        # Independent calculation from the raw samples, not score_to_dict output.
        rates = {(r["workload"], r["threads"]): sorted(s["operations"] / s["elapsed_ns"] for s in r["samples"])[2]
                 for r in data["records"]}
        expected = math.floor(1000 * (rates["integer", len(chosen)] * rates["fp64", len(chosen)]) ** 0.5 + 0.5)
        self.assertEqual(score.points, expected)
        report = work / "result.html"
        render = subprocess.run([sys.executable, str(ROOT / "report_html.py"), "--reports-dir", str(work / "reports"),
                                 "--module", "cpu_multi", "--skip-charts", "--output", str(report)],
                                capture_output=True, text=True)
        self.assertEqual(render.returncode, 0, render.stdout + render.stderr)
        text = report.read_text()
        self.assertIn("Memorytest ComputeMark v2", text)
        self.assertIn("Single-thread score", text)
        self.assertNotIn("/100", text)
        self.assertNotIn("ComputeMark unavailable", text)

    def test_creation_and_affinity_failure_do_not_deadlock_or_score(self):
        if not hasattr(os, "sched_getaffinity"): self.skipTest("Linux affinity fault injection")
        if len(os.sched_getaffinity(0)) < 2: self.skipTest("needs 2 allowed CPUs")
        harness = self.path / "partial-create"
        subprocess.run([*self.cc, *shlex.split(self.flags), str(ROOT / "tests/compute_creation_failure.c"),
                        str(ROOT / "src/compute_kernel.c"), "-lm", "-o", str(harness)], check=True, capture_output=True)
        partial = subprocess.run([str(harness)], capture_output=True, text=True, timeout=3)
        self.assertEqual(partial.returncode, 0, partial.stdout + partial.stderr)
        for name in ["CM_TEST_AFFINITY_FAIL"]:
            work = self.path / name; work.mkdir()
            env = dict(os.environ, LD_PRELOAD=str(self.path / "faults.so"), **{name: "1"})
            result = subprocess.run([str(self.binary), "--threads", "2"], cwd=work, env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((work / "reports/compute_mark_v2.json").exists())

    def test_shared_clock_does_not_use_cpu_frequency(self):
        binary = self.path / ("clock-check.exe" if os.name == "nt" else "clock-check")
        subprocess.run([*self.cc, "-O2", "-std=c11", "-pthread", str(ROOT / "tests/portable_clock.c"),
                        "-o", str(binary)], check=True, capture_output=True)
        self.assertEqual(subprocess.run([str(binary)], timeout=3).returncode, 0)

    @unittest.skipUnless(os.name == "nt", "Windows native validation")
    def test_windows_native_group_pinning_and_score(self):
        work = self.path / "windows-native"
        work.mkdir()
        result = subprocess.run([str(self.binary), "--threads", "2"], cwd=work,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads((work / "reports/compute_mark_v2.json").read_text())
        score = compute_mark_score(data)
        self.assertEqual(score.placement_policy, "windows-pinned-process-group")
        self.assertTrue(score.affinity_verified)
        self.assertEqual(score.threads, 2)
        rates = {(r["workload"], r["threads"]): sorted(s["operations"] / s["elapsed_ns"] for s in r["samples"])[2]
                 for r in data["records"]}
        expected = math.floor(1000 * math.sqrt(rates["integer", 2] * rates["fp64", 2]) + 0.5)
        self.assertEqual(score.points, expected)

    def test_invalid_thread_counts_fail_before_running(self):
        available = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
        for value in ["0", "-1", "nan", "2x", str(available + 1)]:
            result = subprocess.run([str(self.binary), "--threads", value], capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(sys.platform == "darwin", "macOS native validation")
    def test_macos_native_score_and_html(self):
        work = self.path / "darwin-native"; work.mkdir()
        threads = min(2, os.cpu_count() or 1)
        result = subprocess.run([str(self.binary), "--threads", str(threads)], cwd=work,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads((work / "reports/compute_mark_v2.json").read_text())
        score = compute_mark_score(data)
        self.assertEqual(score.placement_policy, "macos-qos-user-initiated")
        self.assertEqual(score.threads, threads)
        self.assertFalse(score.affinity_verified)
        self.assertIsNone(data["available_cpus"])
        rates = {(r["workload"], r["threads"]): sorted(s["operations"] / s["elapsed_ns"] for s in r["samples"])[2]
                 for r in data["records"]}
        self.assertEqual(score.points, math.floor(1000 * (rates["integer", threads] * rates["fp64", threads]) ** .5 + .5))
        report = work / "macos.html"
        render = subprocess.run([sys.executable, str(ROOT / "report_html.py"), "--reports-dir", str(work / "reports"),
                                 "--module", "cpu_multi", "--skip-charts", "--output", str(report)],
                                capture_output=True, text=True)
        self.assertEqual(render.returncode, 0, render.stdout + render.stderr)
        self.assertIn("OS scheduled (not pinned)", report.read_text())
        self.assertNotIn("ComputeMark unavailable", report.read_text())


if __name__ == "__main__":
    unittest.main()
