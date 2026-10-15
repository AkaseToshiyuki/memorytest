"""Clock-scripted protocol acceptance, independent of the real timing backend."""
import os
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
