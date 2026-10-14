"""Acceptance checks for module selection, isolated runs and HTML reporting."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from benchmark_registry import ROOT, TESTS, CATEGORIES, select_modules, report_filename
from report_score import collect_metrics, score_run
from run_benchmarks import run_suite, run_command

HEADINGS = {
    "cache_hierarchy": "Cache Hierarchy", "memory_bandwidth": "Memory Bandwidth",
    "inter_core": "Inter-Core Latency", "cpu_alu": "CPU ALU (Integer)",
    "cpu_float": "CPU Floating Point &amp; SIMD", "cpu_branch": "CPU Branch",
    "cpu_multi": "Multi-Core Scaling",
}


class WorkflowTests(unittest.TestCase):
    def test_report_reader_caches_per_render_without_cross_run_leakage(self):
        import report_html
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.mkdir(); second.mkdir()
            (first / "cpu_alu_report.md").write_text("# First\n")
            (second / "cpu_alu_report.md").write_text("# Second\n")
            report_html._report_cache.clear()
            with patch.object(report_html, "REPORTS", first):
                self.assertEqual(report_html._read_report("cpu_alu"), "# First\n")
                (first / "cpu_alu_report.md").write_text("# Changed\n")
                self.assertEqual(report_html._read_report("cpu_alu"), "# First\n")
                report_html.build_html(None, ["cpu_alu"])
                self.assertEqual(report_html._read_report("cpu_alu"), "# Changed\n")
            with patch.object(report_html, "REPORTS", second):
                self.assertEqual(report_html._read_report("cpu_alu"), "# Second\n")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memorytest-workflow-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()  # macOS /var is a /private/var alias
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fixture = ROOT / "tests" / "fake_benchmark.py"
        # Use a launcher rather than changing the fixture's repository mode.
        for info in TESTS.values():
            target = self.bin / info["bin"]
            target.write_text(f'#!{sys.executable}\nimport sys\nsys.argv[0] = {str(target)!r}\nexec(compile(open({str(fixture)!r}).read(), {str(fixture)!r}, "exec"), {{"__file__": {str(fixture)!r}}})\n')
            target.chmod(0o755)
        self.output = self.root / "reports"

    def run_cli(self, *args, modes=None):
        env = dict(os.environ)
        env.pop("CPU_TIMEOUT", None)
        env.update({"FIXTURE_" + name: mode for name, mode in (modes or {}).items()})
        return subprocess.run([sys.executable, str(ROOT / "run_benchmarks.py"),
                               "--skip-build", "--skip-charts", "--bin-dir", str(self.bin),
                               "--output-dir", str(self.output), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def latest(self):
        path = max((self.output / "runs").glob("*/manifest.json"), key=lambda p: p.stat().st_mtime_ns)
        return path.parent, json.loads(path.read_text())

    def assert_scope(self, content, modules):
        for name, heading in HEADINGS.items():
            self.assertEqual(f"<h2>{heading}</h2>" in content, name in modules, name)
        self.assertNotIn("<h2>Overall Score</h2>", content)
        self.assertNotIn("/100", content)
        self.assertNotIn("{html.escape(title)}", content)

    def test_default_runs_all_and_reports_all(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path, manifest = self.latest()
        self.assertEqual(manifest["selected_modules"], list(TESTS))
        self.assertTrue(manifest["success"])
        self.assertEqual(len(list(path.glob("*/executed"))), 7)
        self.assertEqual(len(list((path / "reports").glob("*.md"))), 7)
        report = (self.output / "benchmark_report.html").read_text()
        self.assert_scope(report, TESTS)
        self.assertIn("CAS Latency (ns)", report)
        self.assertIn("20", report)

    def test_each_single_module_has_only_its_own_html(self):
        for name in TESTS:
            with self.subTest(module=name):
                result = self.run_cli("--module", name)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                path, manifest = self.latest()
                self.assertEqual(manifest["selected_modules"], [name])
                self.assertEqual(len(list(path.glob("*/executed"))), 1)
                report = (self.output / report_filename([name])).read_text()
                self.assert_scope(report, [name])
                self.assertIn("Fixture CPU " + name, report)
                # The generic fixture has no v2 measured sidecar. Legacy multi
                # tables must not silently manufacture a CPU rating.
                self.assertEqual("ComputeMark unavailable" in report, name == "cpu_multi")
                self.assertNotIn("<h2>Memorytest ComputeMark", report)

    def test_category_and_repeated_modules_are_deduplicated(self):
        result = self.run_cli("--category", "cpu", "-m", "cpu_alu", "-m", "cpu_alu")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path, manifest = self.latest()
        self.assertEqual(manifest["selected_modules"], CATEGORIES["cpu"])
        self.assertEqual(len(list(path.glob("*/executed"))), 4)
        self.assert_scope((self.output / "selected_report.html").read_text(), CATEGORIES["cpu"])

    def test_nonzero_exit_keeps_reporting_and_excludes_partial_data(self):
        result = self.run_cli("-m", "cpu_alu", "-m", "cpu_branch", modes={"cpu_alu": "fail"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        path, manifest = self.latest()
        self.assertEqual([r["status"] for r in manifest["modules"]], ["failed", "passed"])
        self.assertEqual(manifest["modules"][0]["exit_code"], 7)
        self.assertFalse((path / "reports" / TESTS["cpu_alu"]["report"]).exists())
        self.assertTrue((path / "cpu_alu" / "reports" / TESTS["cpu_alu"]["report"]).exists())
        report = (self.output / "selected_report.html").read_text()
        self.assertIn("Incomplete run", report)
        self.assertIn("process exited with code 7", report)
        self.assertNotIn("Fixture CPU cpu_alu", report)

    def test_stale_files_and_previous_runs_are_never_reused(self):
        self.assertEqual(self.run_cli("-m", "cpu_alu").returncode, 0)
        first, _ = self.latest()
        first_html = (first / "cpu_alu_report.html").read_bytes()
        (self.output / "cpu_alu_report.md").write_text("OLD DATA SENTINEL")
        self.assertEqual(self.run_cli("-m", "cpu_alu", modes={"cpu_alu": "missing"}).returncode, 1)
        second, manifest = self.latest()
        self.assertNotEqual(first, second)
        self.assertFalse(manifest["success"])
        self.assertEqual(first_html, (first / "cpu_alu_report.html").read_bytes())
        self.assertEqual(collect_metrics(second / "reports", ["cpu_alu"]), {})
        self.assertNotIn("OLD DATA SENTINEL", (self.output / "cpu_alu_report.html").read_text())

    def test_invalid_reports_fail_even_when_process_succeeds(self):
        for mode in ["missing", "invalid", "system_only"]:
            with self.subTest(mode=mode):
                result = self.run_cli("-m", "cpu_alu", modes={"cpu_alu": mode})
                self.assertEqual(result.returncode, 1)
                _, manifest = self.latest()
                self.assertEqual(manifest["modules"][0]["status"], "invalid_report")
                self.assertTrue((self.output / "cpu_alu_report.html").is_file())

    def test_timeout_generates_html(self):
        result = self.run_cli("-m", "cpu_alu", "--timeout", "0.1", modes={"cpu_alu": "timeout"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        _, manifest = self.latest()
        self.assertEqual(manifest["modules"][0]["status"], "timeout")
        self.assertIn("time limit exceeded", (self.output / "cpu_alu_report.html").read_text())

    def test_missing_executable_still_reports_failure(self):
        (self.bin / TESTS["cpu_alu"]["bin"]).unlink()
        self.assertEqual(self.run_cli("-m", "cpu_alu").returncode, 1)
        _, manifest = self.latest()
        self.assertEqual(manifest["modules"][0]["status"], "failed")
        self.assertTrue((self.output / "cpu_alu_report.html").is_file())

    def test_build_failure_only_attempts_selected_target(self):
        def fail_build(command, *args, **kwargs):
            return 2 if command[0] == "make" else run_command(command, *args, **kwargs)
        with patch("run_benchmarks.run_command", side_effect=fail_build) as command, contextlib.redirect_stdout(io.StringIO()):
            code = run_suite(["cpu_alu"], self.output, self.bin, skip_charts=True)
        self.assertEqual(code, 1)
        builds = [call.args[0] for call in command.call_args_list if call.args[0][0] == "make"]
        self.assertEqual(builds, [["make", f"BUILD_DIR={self.bin}", str(self.bin / "test_cpu_alu")]])
        _, manifest = self.latest()
        self.assertEqual(manifest["modules"][0]["status"], "build_failed")

    def test_interruption_marks_remaining_modules_skipped(self):
        def interrupt_benchmark(command, *args, **kwargs):
            if Path(command[0]).parent == self.bin:
                raise KeyboardInterrupt
            return run_command(command, *args, **kwargs)
        with patch("run_benchmarks.run_command", side_effect=interrupt_benchmark), contextlib.redirect_stdout(io.StringIO()):
            code = run_suite(["cpu_alu", "cpu_branch"], self.output, self.bin, skip_build=True, skip_charts=True)
        self.assertEqual(code, 130)
        _, manifest = self.latest()
        self.assertEqual([r["status"] for r in manifest["modules"]], ["interrupted", "skipped"])

    def test_invalid_arguments_do_not_start_any_run(self):
        for args in [("-m", "unknown"), ("--timeout", "nan"), ("--timeout", "0"), ("--timeout", "inf")]:
            self.assertEqual(self.run_cli(*args).returncode, 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.run_cli("--list").returncode, 0)
        self.assertFalse(self.output.exists())

    def test_make_dry_run_does_not_execute_benchmarks(self):
        result = subprocess.run(["make", "-n", "test", "MODULE=cpu_alu",
                                 f"ARGS=--output-dir {self.output}"], cwd=ROOT,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('--module "cpu_alu"', result.stdout)
        self.assertNotIn("Selected:", result.stdout)
        self.assertFalse(self.output.exists())

    def test_scoring_respects_module_scope(self):
        self.assertEqual(self.run_cli("--category", "cpu").returncode, 0)
        path, _ = self.latest()
        metrics = collect_metrics(path / "reports", ["cpu_alu"])
        self.assertNotIn("multi_scaling", metrics)
        score = score_run(path / "reports", ["cpu_alu"])
        self.assertEqual([cat.name for cat in score.categories], ["alu"])
        self.assertEqual(score.n_metrics_total, 6)
        self.assertIsNone(score.compute_mark)
        self.assertFalse(any("ComputeMark" in warning for warning in score.warnings))

    def test_report_regeneration_uses_latest_manifest(self):
        self.assertEqual(self.run_cli("-m", "cpu_branch").returncode, 0)
        result = subprocess.run([sys.executable, str(ROOT / "report_html.py"), "--reports-dir", str(self.output),
                                 "--latest-run", "--skip-charts"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path, _ = self.latest()
        self.assert_scope((path / "cpu_branch_report.html").read_text(), ["cpu_branch"])
        rejected = subprocess.run([sys.executable, str(ROOT / "report_html.py"), "--run-dir", str(path),
                                   "-m", "cpu_alu", "--skip-charts"], capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 2)

    def test_unsupported_module_keeps_report_and_does_not_block_others(self):
        result = self.run_cli("-m", "inter_core", "-m", "cpu_alu", modes={"inter_core": "unsupported"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path, manifest = self.latest()
        self.assertEqual([r["status"] for r in manifest["modules"]], ["unsupported", "passed"])
        self.assertTrue(manifest["success"])
        self.assertFalse(manifest["all_selected_passed"])
        self.assertFalse((path / "reports" / TESTS["inter_core"]["report"]).exists())
        self.assertIn("Unsupported modules were not measured", (path / "selected_report.html").read_text())

    def test_unsupported_only_is_not_a_pass(self):
        result = self.run_cli("-m", "inter_core", modes={"inter_core": "unsupported"})
        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)
        path, manifest = self.latest()
        self.assertFalse(manifest["success"])
        self.assertTrue((path / "inter_core_report.html").exists())
        self.assertFalse(list((path / "reports").iterdir()))


if __name__ == "__main__":
    unittest.main()
