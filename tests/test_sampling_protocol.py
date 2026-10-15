"""Clock-scripted protocol acceptance, independent of the real timing backend."""
import os
import re
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from benchmark_registry import ROOT


class SamplingProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-sampling-")
        cls.binary = Path(cls.temp.name) / ("sampling.exe" if os.name == "nt" else "sampling")
        command = shlex.split(os.environ.get("CC", "cc")) + ["-O2", "-std=c11", "-pthread",
                   str(ROOT / "tests/sampling_protocol.c"), "-o", str(cls.binary)]
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        cls.even = cls.binary.with_name("even" + cls.binary.suffix)
        command[-1] = str(cls.even)
        subprocess.run([*command, "-DBENCH_SAMPLES=4"], check=True, capture_output=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_mode(self, mode):
        run = subprocess.run([str(self.binary), mode], check=True, capture_output=True, text=True, timeout=3)
        return list(map(int, run.stdout.split()))

    def test_cold_start_and_scheduling_spike_do_not_collapse_sample_duration(self):
        status, calls, repetitions, median, low, high = self.run_mode("jitter")
        self.assertEqual(status, 0)
        self.assertEqual(repetitions, 56)
        self.assertEqual((median, low, high), (112, 112, 112))
        self.assertEqual(calls, 14)  # 1 warmup + 3 calibration + 5 rejected + 5 retained.

    def test_stalled_backward_and_kernel_error_fail_closed_with_bounded_calls(self):
        for mode in ("stalled", "backward", "error"):
            status, calls, *_ = self.run_mode(mode)
            self.assertNotEqual(status, 0, mode)
            self.assertLess(calls, 16, mode)

    def test_even_median_keeps_half_nanosecond(self):
        run = subprocess.run([str(self.even), "even"], check=True, capture_output=True, text=True, timeout=3)
        self.assertEqual(list(map(float, run.stdout.split())), [0, 6, 1, 101.5, 100, 103])

    def test_module_defaults_are_cache5_compute20_and_alu_float50(self):
        cc = shlex.split(os.environ.get("CC", "cc"))
        for module, expected in (("cache_hierarchy", 5), ("memory_bandwidth", 20),
                                 ("cpu_alu", 50), ("cpu_float", 50), ("cpu_branch", 20)):
            output = subprocess.check_output([*cc, "-dM", "-E", "-std=c11", "-pthread",
                                              str(ROOT / f"src/test_{module}.c")], text=True)
            macros = dict(re.findall(r"^#define (\w+) ([^\n]+)$", output, re.M))
            value = macros["BENCH_SAMPLES"]
            while value in macros:
                value = macros[value]
            self.assertEqual(int(value), expected, module)
        output = subprocess.check_output([*cc, "-dM", "-E", str(ROOT / "src/compute_kernel.h")], text=True)
        self.assertRegex(output, r"#define CM_REPEATS 20\b")
