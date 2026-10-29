"""Bounded protocol fixtures; simulated CPU IDs are never hardware validation."""
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from benchmark_registry import ROOT
from intercore_observed import read_observed, PROTOCOL, PROTOCOLS
from test_sample_statistics import reference


def fixture(protocol=PROTOCOL):
    samples = PROTOCOLS[protocol][0]
    return {"schema_version": 1, "protocol": protocol, "os": "Darwin", "affinity_verified": False,
            "migration_verification": "endpoints-only", "cpu_id_source": "TPIDR_EL0-low12-private-ABI",
            "timer": "CLOCK_MONOTONIC", "qos": "user-initiated", "qos_verified": True,
            "background_load": "other-workers-runnable", "logical_cpus": 3, "workers": 2,
            "budget_ms": 1000, "sample_count": samples, "round_trips": 2000, "warmup_round_trips": 128,
            "covered_pairs": 1, "attempts": [{"from": [101, 101], "to": [104, 104],
                "elapsed_ns": (48 + index % 5) * 4000, "status": "kept"} for index in range(samples)]}


class ObservedContractTests(unittest.TestCase):
    def test_independent_statistics_sparse_ids_and_no_filled_reverse_pair(self):
        result = read_observed(fixture())
        self.assertEqual(result["ids"], [101, 104])
        self.assertEqual(set(result["pairs"]), {(101, 104)})
        for key, value in reference([48 + index % 5 for index in range(32)]).items():
            self.assertEqual(result["pairs"][101, 104][key], value)
        self.assertEqual((result["covered_pairs"], result["possible_pairs"]), (1, 6))

    def test_legacy_and_current_sample_counts_are_not_interchangeable(self):
        for protocol, count in (("macos-observed-cpu-pairs-v1", 10), (PROTOCOL, 32)):
            data = fixture(protocol)
            result = read_observed(data)
            self.assertEqual(result["sample_count"], count)
            self.assertEqual(result["pairs"][101, 104]["count"], count)
            self.assertEqual(result["pairs"][101, 104]["median"], 50)
            for wrong in (10, 32):
                if wrong != count:
                    data["sample_count"] = wrong
                    with self.assertRaises(ValueError): read_observed(data)

    def test_expanded_attempt_limit_and_large_report_preserve_evidence(self):
        from result_model import build_module
        data = fixture()
        quota = dict(data["attempts"][0], status="quota")
        data["attempts"] += [quota] * (64000 - 32)
        encoded = json.dumps(data)
        self.assertGreater(len(encoded), 5_000_000)
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            (reports / "inter_core_latency_report.md").write_text("# Observed\n- Protocol: " + PROTOCOL + "\n")
            (reports / "inter_core_observed.json").write_text(encoded)
            result = build_module("inter_core", reports)
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.tables[0].rows[0][2:4], [32, 50])
        data["attempts"].append(quota)
        with self.assertRaises(ValueError): read_observed(data)

    def test_reject_invalid_placement_counts_and_migrated_samples(self):
        for key, value in (("affinity_verified", True), ("sample_count", 20), ("covered_pairs", 2),
                           ("logical_cpus", True), ("qos_verified", False), ("protocol", "intercore-v3")):
            bad = fixture(); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): read_observed(bad)
        for mode in ("migration", "same", "missing", "extra", "zero", "bad_quota"):
            bad = fixture()
            if mode == "migration": bad["attempts"][0]["from"][1] = 103
            if mode == "same": bad["attempts"][0]["to"] = [101, 101]
            if mode == "missing": bad["attempts"].pop()
            if mode == "extra": bad["attempts"].append(copy.deepcopy(bad["attempts"][0]))
            if mode == "zero": bad["attempts"][0]["elapsed_ns"] = 0
            if mode == "bad_quota": bad["attempts"][0]["status"] = "quota"
            with self.subTest(mode=mode), self.assertRaises(ValueError): read_observed(bad)

    def test_model_keeps_missing_cells_blank(self):
        from result_model import build_module
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            (reports / "inter_core_latency_report.md").write_text("# Observed\n- Protocol: " + PROTOCOL + "\n")
            (reports / "inter_core_observed.json").write_text(json.dumps(fixture()))
            result = build_module("inter_core", reports)
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.charts[0].series[0]["values"], [[None, 50], [None, None]])

    def test_runner_requires_matching_raw_observations(self):
        from run_benchmarks import validate_evidence
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            report = reports / "inter_core_latency_report.md"
            report.write_text("# Observed\n- Protocol: " + PROTOCOL + "\n")
            self.assertIsNotNone(validate_evidence("inter_core", reports))
            (reports / "inter_core_observed.json").write_text(json.dumps(fixture()))
            self.assertIsNone(validate_evidence("inter_core", reports))
            report.write_text("# Observed\n- Protocol: macos-observed-cpu-pairs-v1\n")
            self.assertIsNotNone(validate_evidence("inter_core", reports))
            report.write_text("# Strict legacy matrix\n")
            self.assertIsNotNone(validate_evidence("inter_core", reports))


@unittest.skipIf(os.name == "nt", "POSIX worker protocol fixture")
class ObservedNativeFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-observed-")
        cls.root = Path(cls.temp.name)
        cls.binary = cls.root / "observed"
        common = ["common.c", "util.c", "platform.c", "report.c", "simd.c", "latency.c",
                  "detect_macos.c" if sys.platform == "darwin" else "detect.c",
                  "pmu_macos.c" if sys.platform == "darwin" else "pmu.c"]
        build = subprocess.run([*shlex.split(os.environ.get("CC", "cc")), "-O2", "-std=c11", "-pthread",
            str(ROOT / "tests/observed_intercore.c"), *[str(ROOT / "src" / name) for name in common],
            "-lm", "-o", str(cls.binary)], capture_output=True, text=True, timeout=60)
        if build.returncode: raise AssertionError(build.stdout + build.stderr)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_completion_count_rejections_timeout_and_cleanup(self):
        for mode, expected in ((0, 0), (1, 77), (2, 77), (3, 77), (4, 1), (5, 77), (6, 1)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=self.root) as directory:
                run = subprocess.run([str(self.binary), str(mode)], cwd=directory, capture_output=True, text=True, timeout=5)
                self.assertEqual(run.returncode, expected, run.stdout + run.stderr)
                if not mode:
                    raw = json.loads((Path(directory) / "reports/inter_core_observed.json").read_text())
                    result = read_observed(raw)
                    self.assertEqual(result["covered_pairs"], 2)
                    self.assertEqual(result["attempt_counts"], {"kept": 64})
                    for key, stats in result["pairs"].items():
                        values = [a["elapsed_ns"] / 4000 for a in raw["attempts"] if (a["from"][0], a["to"][0]) == key]
                        for field, value in reference(values).items():
                            if isinstance(value, float): self.assertAlmostEqual(stats[field], value, places=8)
                            else: self.assertEqual(stats[field], value)

    @unittest.skipUnless(Path("/dev/full").exists(), "requires fail-on-write device")
    def test_audit_write_failure_is_not_success(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            reports = Path(directory) / "reports"; reports.mkdir()
            (reports / "inter_core_observed.json").symlink_to("/dev/full")
            run = subprocess.run([str(self.binary), "0"], cwd=directory, capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
