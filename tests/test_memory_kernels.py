"""Small independent memory references and two-worker, 384-KiB acceptance."""
import os
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from benchmark_registry import ROOT
from result_model import build_module, tables
from run_benchmarks import validate_report


class MemoryKernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-memory-kernels-")
        cls.root = Path(cls.temp.name)
        windows = os.name == "nt"
        cls.binary = cls.root / ("memory.exe" if windows else "memory")
        sources = ["common.c", "platform.c", "report.c", "simd.c", "latency.c",
                   "util_windows.c" if windows else "util.c",
                   "detect_windows.c" if windows else "detect_macos.c" if sys.platform == "darwin" else "detect.c",
                   "pmu_macos.c" if windows or sys.platform == "darwin" else "pmu.c"]
        command = shlex.split(os.environ.get("CC", "cc")) + ["-O2", "-std=c11", "-pthread",
            "-DBENCH_TARGET_NS=1000000", "-DBENCH_WARMUP_NS=500000",
            str(ROOT / "tests/memory_regression.c"), *[str(ROOT / "src" / s) for s in sources], "-lm", "-o", str(cls.binary)]
        if windows:
            command.append("-ladvapi32")
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_each_element_byte_count_and_full_cycle_independent_reference(self):
        run = subprocess.run([str(self.binary)], capture_output=True, text=True, check=True, timeout=10)
        lines = run.stdout.splitlines()
        a = [1 + (i % 8) / 8 for i in range(65)]
        b = [2 + (i % 4) / 4 for i in range(65)]
        byte_counts = [65 * 8, 65 * 8, 65 * 16, 65 * 16, 65 * 24, 65 * 24, 9 * 8, 3 * 8]
        arrays = {1: [3] * 65, 2: a, 3: [3 * x for x in a],
                  4: [x + y for x, y in zip(a, b)], 5: [x + 3 * y for x, y in zip(a, b)]}
        for op in range(8):
            index, payload, result, *values = lines[op].split()
            self.assertEqual(int(index), op)
            self.assertEqual(int(payload), byte_counts[op])
            if op in arrays:
                self.assertEqual(list(map(float, values)), arrays[op])
                self.assertEqual(float(result), arrays[op][-1])
            else:
                self.assertEqual(float(result), 3 * sum(a[::{0: 1, 6: 8, 7: 32}[op]]))
        cycle = list(map(int, lines[8].split()))
        self.assertEqual(sorted(cycle), list(range(65)))
        self.assertEqual(int(lines[9]), cycle[(3 * 256) % 65])

    def test_two_worker_shared_window_counts_and_latency_report(self):
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            run = subprocess.run([str(self.binary), "report"], cwd=folder, capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            reports = Path(folder) / "reports"
            path = reports / "memory_bandwidth_report.md"
            self.assertIsNone(validate_report(path))
            for _, headers, rows in tables(path.read_text()):
                if "Bandwidth (MB/s)" in headers:
                    self.assertEqual(len(rows), 8)
                    for row in rows:
                        rate, milliseconds, payload = map(float, row[1:4])
                        self.assertGreater(rate, 0)
                        self.assertAlmostEqual(rate, payload / (milliseconds * 1000), delta=rate * 2e-6)
                if "ns/op" in headers:
                    self.assertEqual(len(rows), 3)
                    self.assertEqual([float(row[1].split()[0]) for row in rows], [.125, .125, .25])
                    for row in rows:
                        ns, milliseconds, loads = map(float, row[2:5])
                        self.assertGreater(ns, 0)
                        self.assertAlmostEqual(ns, milliseconds * 1e6 / loads, delta=1e-6)
            model = build_module("memory_bandwidth", reports)
            self.assertEqual(model.status, "passed")
            self.assertEqual(len(model.charts[0].labels), 8)
            self.assertEqual(model.charts[1].title, "memory_latency")

    def test_read_write_copy_on_independently_defined_cycle_and_all_payloads(self):
        run = subprocess.run([str(self.binary), "latency_reference"], capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for line in run.stdout.splitlines():
            operation, words, cursor, *payload = map(int, line.split())
            self.assertEqual(cursor, [0, 3, 1, 4, 2][768 % 5])
            expected = []
            for node, next_node in enumerate([3, 4, 0, 1, 2]):
                expected += [next_node] + [(node * 17 + word) ^ (0x5a5aa5a5 if operation == 1 else 0x13579bdf)
                                           for word in range(1, words)]
            self.assertEqual(payload, expected)

    def test_partial_creation_and_allocation_fail_without_report_or_hang(self):
        for mode in ("creation", "allocation"):
            with tempfile.TemporaryDirectory(dir=self.root) as folder:
                run = subprocess.run([str(self.binary), mode], cwd=folder, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse((Path(folder) / "reports/memory_bandwidth_report.md").exists())

    def test_separate_modes_and_raw_timing_evidence(self):
        for mode, count in (("bandwidth", 8), ("latency", 3)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=self.root) as folder:
                run = subprocess.run([str(self.binary), mode + "_only"], cwd=folder,
                                     capture_output=True, text=True, timeout=15)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                reports = Path(folder) / "reports"
                name = f"memory_{mode}_test"
                path = reports / f"{name}_report.md"
                self.assertIsNone(validate_report(path))
                text = path.read_text()
                self.assertEqual("## Memory Bandwidth Test Results" in text, mode == "bandwidth")
                self.assertEqual("## Memory Latency" in text, mode == "latency")
                self.assertIn("- Thread Count: " + ("1" if mode == "latency" else "2"), text)
                audit = json.loads((reports / f"{name}_samples.json").read_text())
                self.assertEqual(len(audit["records"]), count)
                for record in audit["records"]:
                    self.assertEqual(len(record["elapsed_ns"]), 20)
                    self.assertGreaterEqual(record["statistics"]["kept"], 18)
                model = build_module(name, reports)
                self.assertEqual(model.status, "passed")
                self.assertEqual(len(model.charts), 1)
