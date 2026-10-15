"""Bounded Linux completion/count checks; no whole-machine matrix."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from benchmark_registry import ROOT


@unittest.skipUnless(sys.platform.startswith("linux"), "native matrix backend is Linux-only")
class IntercoreProtocolTests(unittest.TestCase):
    def test_repeated_dispatch_completion_and_discarded_warmup(self):
        with tempfile.TemporaryDirectory(prefix="memorytest-pair-") as directory:
            binary = Path(directory) / "pair"
            sources = ["common.c", "util.c", "platform.c", "detect.c", "report.c", "simd.c", "pmu.c", "latency.c"]
            command = shlex.split(os.environ.get("CC", "cc")) + ["-O2", "-std=c11", "-pthread",
                str(ROOT / "tests/intercore_protocol.c"), *[str(ROOT / "src" / s) for s in sources], "-lm", "-o", str(binary)]
            build = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            allowed = sorted(os.sched_getaffinity(0))
            if len(allowed) < 2:
                self.skipTest("two allowed logical CPUs required for verified pinning")
            selected = {allowed[0], allowed[-1]}
            # The subprocess starts under a sparse affinity mask, independent
            # of the implementation's CPU enumeration or topology guesses.
            def pin():
                os.sched_setaffinity(0, selected)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10, preexec_fn=pin)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("completion-counts-ok", run.stdout)
            self.assertNotIn("stale", run.stderr)
            self.assertIn(f"cpu-ids {allowed[0]} {allowed[-1]}", run.stdout)
            for mode in ("binding", "creation"):
                failure = subprocess.run([str(binary), mode], capture_output=True, text=True, timeout=5, preexec_fn=pin)
                self.assertEqual(failure.returncode, 0, failure.stdout + failure.stderr)
                self.assertIn("failure-cleanup-ok", failure.stdout)
