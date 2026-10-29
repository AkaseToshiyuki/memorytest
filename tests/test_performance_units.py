"""Independent operation/time examples, archive compatibility and display precision."""
from fractions import Fraction
import json
from pathlib import Path
import tempfile
import unittest

from i18n import display
from performance_units import row_gops, throughput_scale, throughput_value
from report_score import parse_cpu_alu, parse_cpu_float
from result_model import build_module
from test_compute import fixture as compute_fixture


SYSTEM = "# Report\n| Item | Value |\n|---|---|\n| CPU | fixture |\n\n"


class ThroughputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_decimal_boundary_and_consistent_shared_scale(self):
        for value, expected, unit in ((0, 0, "GOPS"), (99.9, 99.9, "GOPS"),
                                      (100, 0.1, "TOPS"), (1250, 1.25, "TOPS")):
            self.assertEqual(throughput_value(value), (expected, unit))
        self.assertEqual(throughput_scale([100, 200]), (0.001, "TOPS"))
        self.assertEqual(throughput_scale([2, 200]), (1, "GOPS"))

    def test_independent_count_time_reference_and_legacy_units(self):
        # 400 million operations in 200 ms = 2 billion operations/second.
        expected = float(Fraction(400_000_000, 200_000_000))
        for header, value in (("Rate (GOPS)", "2"), ("Rate (TOPS)", "0.002"),
                              ("Ops/sec", "2000000000"), ("ns/op", "0.5")):
            self.assertEqual(row_gops([header], [value]), expected)
        for value in ("nan", "inf", "-1", True):
            self.assertIsNone(row_gops(["Rate (GOPS)"], [value]))
        self.assertIsNone(row_gops(["ns/op"], ["0"]))

    def test_old_and_new_arithmetic_reports_produce_correct_rates(self):
        formats = (
            ("Ops/sec | ns/op", ["2000000000 | 0.5", "200000000000 | 0.005"]),
            ("Rate (GOPS)", ["2", "200"]),
            ("Rate (TOPS)", ["0.002", "0.2"]),
        )
        for name in ("cpu_alu", "cpu_float"):
            for header, rates in formats:
                headers = ["Operation", "Time(ms)", *header.split(" | "), "Operations", "Spread(%)"]
                content = (SYSTEM + "## Results\n| " + " | ".join(headers) + " |\n|" + "---|" * len(headers) +
                           "\n| Add | 200 | " + rates[0] + " | 400000000 | 1 |\n" +
                           "| Mul | 200 | " + rates[1] + " | 40000000000 | 1 |\n")
                path = self.root / (name + "_report.md")
                path.write_text(content)
                model = build_module(name, self.root)
                self.assertEqual(model.status, "passed")
                self.assertEqual(model.charts[0].unit, "GOPS")
                self.assertEqual(model.charts[0].series[0]["values"], [2, 200])
                self.assertEqual([row[1] for row in model.tables[0].rows], [2, 200])
                self.assertEqual([(item["value"], item["unit"]) for item in model.kpis], [(2, "GOPS"), (0.2, "TOPS")])
                self.assertEqual(path.read_text(), content)

    def test_compute_rates_scale_without_changing_points_or_speedup(self):
        data = compute_fixture()
        for record in data["records"]:
            record["iterations_per_thread"] *= 10
            for sample in record["samples"]:
                sample["operations"] *= 10
        (self.root / "cpu_multi_core_report.md").write_text(SYSTEM)
        (self.root / "compute_mark_v2.json").write_text(json.dumps(data))
        model = build_module("cpu_multi", self.root)
        self.assertEqual(model.status, "passed")
        self.assertEqual([item["value"] for item in model.kpis], [169706, 339411, 0.48, 0.24])
        self.assertEqual([chart.unit for chart in model.charts], ["TOPS", "TOPS"])
        self.assertEqual(model.charts[0].series[0]["values"], [0.24, 0.48])
        self.assertEqual(model.tables[0].rows, [[1, 0.24, 1], [2, 0.48, 2]])

    def test_small_rates_remain_nonzero_in_display(self):
        for value in (0.000012345, 0.00012345, 0.0012345, 0.012345):
            shown = float(display(value))
            self.assertGreater(shown, 0)
            self.assertLess(abs(shown / value - 1), 0.001)

    def test_legacy_score_reader_uses_rates_without_inventing_ipc(self):
        integer = SYSTEM + "| Operation | Time(ms) | Rate (GOPS) | Operations | Spread(%) |\n|---|---|---|---|---|\n| Add | 200 | 2 | 400000000 | 1 |\n"
        floating = SYSTEM + "| Operation | Type | Time(ms) | Rate (TOPS) |\n|---|---|---|---|\n| SIMD FMA | SIMD | 200 | 0.002 |\n"
        self.assertEqual(parse_cpu_alu(integer), {"alu_add_ops_sec": 2_000_000_000})
        self.assertEqual(parse_cpu_float(floating), {"simd_fma_ns": 0.5, "simd_fma_ops_sec": 2_000_000_000})


if __name__ == "__main__":
    unittest.main()
