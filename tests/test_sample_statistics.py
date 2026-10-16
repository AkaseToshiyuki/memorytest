"""Exact rational reference and hand-worked cases, not C/Python agreement alone."""
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import shlex
import statistics
import subprocess
import tempfile
import unittest

from benchmark_registry import ROOT
from sample_statistics import summarize


def reference(values):
    """Independent exact order-statistic oracle; no production helpers."""
    def rank(items, fraction):
        ordered = sorted(Fraction(x) for x in items)
        position = (len(ordered) - 1) * fraction
        left = position.numerator // position.denominator
        weight = position - left
        return ordered[left] * (1 - weight) + ordered[min(left + 1, len(ordered) - 1)] * weight
    q1, middle, q3 = [rank(values, Fraction(q, 4)) for q in (1, 2, 3)]
    n = len(values)
    drift = 100 * (rank(values[-(n // 2):], Fraction(1, 2)) - rank(values[:n // 2], Fraction(1, 2))) / middle
    candidates = [i for i, x in enumerate(values) if n >= 10 and q3 > q1
                  and (x < 4 * q1 - 3 * q3 or x > 4 * q3 - 3 * q1)]
    unstable = abs(drift) > 10 or len(candidates) * 10 > n
    excluded = [] if unstable else candidates
    return dict(count=n, kept=n - len(excluded), outlier_candidates=len(candidates),
                rejected=len(excluded), unstable=unstable, raw_median=float(middle),
                median=float(rank([x for i, x in enumerate(values) if i not in excluded], Fraction(1, 2))),
                q1=float(q1), q3=float(q3), raw_min=min(values), raw_max=max(values),
                drift_pct=float(drift), excluded_indices=excluded)


def reference_rates(data):
    result = {}
    for record in data["records"]:
        samples = record["samples"]
        excluded = reference([s["elapsed_ns"] for s in samples])["excluded_indices"] if len(samples) in {10, 20} else []
        result[record["workload"], record["threads"]] = statistics.median(
            s["operations"] / s["elapsed_ns"] for i, s in enumerate(samples) if i not in excluded)
    return result


class SampleStatisticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-statistics-")
        cls.binary = Path(cls.temp.name) / ("stats.exe" if os.name == "nt" else "stats")
        subprocess.run([*shlex.split(os.environ.get("CC", "cc")), "-O2", "-std=c11",
                        str(ROOT / "tests/statistics_probe.c"), "-o", str(cls.binary)],
                       check=True, capture_output=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def c_result(self, values):
        return subprocess.run([str(self.binary)], input=f"{len(values)} " + " ".join(map(str, values)),
                              capture_output=True, text=True, timeout=3)

    def check_values(self, values):
        expected = reference(values)
        c = self.c_result(values)
        self.assertEqual(c.returncode, 0, c.stderr)
        for actual in (summarize(values), json.loads(c.stdout)):
            for key, value in expected.items():
                if isinstance(value, float):
                    self.assertAlmostEqual(actual[key], value, places=10, msg=key)
                else:
                    self.assertEqual(actual[key], value, key)
        return expected

    def test_hand_worked_even_quantiles(self):
        result = self.check_values([1, 2, 3, 4])
        self.assertEqual([result[k] for k in ("q1", "raw_median", "q3")], [1.75, 2.5, 3.25])
        self.assertEqual(result["drift_pct"], 80)
        self.assertEqual(result["rejected"], 0)

    def test_both_tails_and_supported_repeat_counts(self):
        for count in (20, 50, 100, 200):
            values = [98, 99, 100, 101, 102] * (count // 5)
            values[0], values[9] = 1, 1000
            with self.subTest(count=count):
                result = self.check_values(values)
                self.assertEqual(result["excluded_indices"], [0, 9])
                self.assertEqual([result[k] for k in ("q1", "median", "q3")], [99, 100, 101])
                self.assertFalse(result["unstable"])

    def test_ten_samples_allow_only_one_candidate_and_keep_drift_guard(self):
        one = [1000, 99, 100, 101, 102, 98, 99, 100, 101, 102]
        result = self.check_values(one)
        self.assertEqual(result["excluded_indices"], [0])
        self.assertEqual(result["kept"], 9)
        two = one[:]; two[-1] = 1
        result = self.check_values(two)
        self.assertEqual(result["excluded_indices"], [])
        self.assertTrue(result["unstable"])
        drift = [98, 99, 100, 101, 102, 198, 199, 200, 201, 202]
        self.assertTrue(self.check_values(drift)["unstable"])

    def test_zero_spread_and_small_samples_never_filter(self):
        for values in ([100] * 20, [100] * 19 + [1000], [1, 99, 100, 101, 1000]):
            self.assertEqual(self.check_values(values)["excluded_indices"], [])

    def test_drift_and_excessive_contamination_are_not_hidden(self):
        drift = [98, 99, 100, 101, 102] * 2 + [198, 199, 200, 201, 202] * 2
        contaminated = [98, 99, 100, 101, 102] * 4
        for index in (0, 9, 10):
            contaminated[index] = 1000
        for values in (drift, contaminated):
            result = self.check_values(values)
            self.assertTrue(result["unstable"])
            self.assertEqual(result["excluded_indices"], [])
        self.assertEqual(self.check_values(contaminated)["outlier_candidates"], 3)

    def test_invalid_values_rejected(self):
        for values in ([], [1, 2], [1] * 201, [1, 0, 2], [1, -2, 3], [1, math.inf, 2], [1, math.nan, 2]):
            with self.subTest(values=values[:3]):
                with self.assertRaises(ValueError):
                    summarize(values)
                self.assertNotEqual(self.c_result(values).returncode, 0)
        with self.assertRaises(ValueError):
            summarize([True, 1, 2])
