"""Independent arithmetic, instruction-stream and presentation contracts."""
import copy
from fractions import Fraction
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from benchmark_registry import ROOT
from architecture_analysis import PROBES, read_architecture
from result_model import build_module


def fixture():
    return {"schema_version": 1, "protocol": "architecture-probes-v1", "isa": "aarch64-fp64",
        "placement": "macos-qos-user-initiated", "cpu_id": -1, "sample_count": 10,
        "sample_target_ns": 50_000_000, "timer": "monotonic-wall-time", "cache_source": "OS-reported-not-probed",
        "reported_cache_bytes": {"l1i": 131072, "l1d": 65536, "l2": 0, "l3": 0},
        "records": [{"probe": name, "blocks": 1_000_000, "operations_per_block": operations,
                     "validated": True, "elapsed_ns": [100_000_000 + 1_000_000 * i for i in range(5)] * 2}
                    for name, operations in PROBES.items()]}


class ArchitectureContracts(unittest.TestCase):
    def test_scoring_is_wall_time_and_not_stored_summary_or_clock_inference(self):
        data = fixture()
        for record in data["records"]: record["statistics"] = {"median": 1}
        result = read_architecture(data)
        self.assertAlmostEqual(result["metrics"]["iadd_dep"]["ns"], 102_000_000 / 32_000_000)
        self.assertAlmostEqual(result["metrics"]["nop_stream"]["gops"], 64_000_000 / 102_000_000)
        for key, value in (("cpu_id", 0), ("sample_count", 5), ("cache_source", "measured"), ("isa", "future")):
            bad = copy.deepcopy(data); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): read_architecture(bad)
        for mode in ("missing", "duplicate", "bad-count", "short", "unvalidated"):
            bad = copy.deepcopy(data)
            if mode == "missing": bad["records"].pop()
            if mode == "duplicate": bad["records"][-1] = bad["records"][0]
            if mode == "bad-count": bad["records"][0]["operations_per_block"] = 32
            if mode == "short": bad["records"][0]["elapsed_ns"][0] = 1
            if mode == "unvalidated": bad["records"][0]["validated"] = False
            with self.subTest(mode=mode), self.assertRaises(ValueError): read_architecture(bad)

    def test_diagram_cards_and_unknown_structures_in_both_languages(self):
        from presentation import plot_svg
        from report_view import module_html
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            (reports / "cpu_architecture_report.md").write_text("# Architecture\n")
            (reports / "cpu_architecture.json").write_text(json.dumps(fixture()))
            result = build_module("cpu_architecture", reports)
            self.assertEqual(result.status, "passed")
            self.assertEqual((len(result.kpis), len(result.tables[0].rows)), (4, 12))
            for language in ("en", "zh"):
                svg = plot_svg(result.charts[0], language)
                self.assertIn("stroke-dasharray", svg)
                self.assertIn("OS-reported" if language == "en" else "系统报告", svg)
                self.assertIn("unmeasured" if language == "en" else "尚未测定", svg)
                self.assertIn("NOT eight" if language == "en" else "不代表八个", svg)
                self.assertIn("<svg", module_html(result, language))

    def test_qt_architecture_page_main_selection_and_bilingual_layout(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtGui import QImage, QPainter
            from PySide6.QtSvg import QSvgRenderer
            from desktop_window import MainWindow
            from presentation import plot_svg
        except ImportError:
            self.skipTest("Qt desktop dependencies unavailable")
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            (reports / "cpu_architecture_report.md").write_text("# Architecture\n")
            (reports / "cpu_architecture.json").write_text(json.dumps(fixture()))
            model = build_module("cpu_architecture", reports)
            window = MainWindow(autodetect=False)
            self.addCleanup(window.close)
            self.assertIn("cpu_architecture", window.modules)
            page = window.pages["cpu_architecture"]
            page.model = model
            window.tabs.setCurrentIndex(3)
            window.group_tabs["core_group"].setCurrentWidget(page)
            window.resize(1100, 900)
            output = ROOT / "build/architecture-preview"; output.mkdir(parents=True, exist_ok=True)
            for language in ("zh", "en"):
                window.language_combo.setCurrentIndex(window.language_combo.findData(language))
                window.retranslate(); window.show(); app.processEvents()
                self.assertEqual(page.details.horizontalScrollBar().maximum(), 0)
                window.grab().save(str(output / f"architecture-page-{language}.png"))
                svg = plot_svg(model.charts[0], language)
                (output / f"architecture-{language}.svg").write_text(svg)
                renderer = QSvgRenderer(svg.encode())
                self.assertTrue(renderer.isValid())
                bitmap = QImage(1500, 1215, QImage.Format_ARGB32)
                painter = QPainter(bitmap); renderer.render(painter); painter.end()
                bitmap.save(str(output / f"architecture-{language}.png"))


@unittest.skipUnless(platform.machine().lower() in {"x86_64", "amd64", "aarch64", "arm64"}, "instruction backend unavailable")
class ArchitectureNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-architecture-")
        cls.root = Path(cls.temp.name)
        cls.binary = cls.root / ("architecture.exe" if os.name == "nt" else "architecture")
        common = ["common.c", "platform.c", "report.c", "simd.c", "latency.c",
                  "util_windows.c" if os.name == "nt" else "util.c",
                  "detect_windows.c" if os.name == "nt" else "detect_macos.c" if sys.platform == "darwin" else "detect.c",
                  "pmu_macos.c" if os.name == "nt" or sys.platform == "darwin" else "pmu.c"]
        command = [*shlex.split(os.environ.get("CC", "cc")), "-O2", "-std=c11", "-pthread",
            "-DBENCH_TARGET_NS=1000000", "-DBENCH_WARMUP_NS=500000", str(ROOT / "tests/architecture_regression.c"),
            *[str(ROOT / "src" / name) for name in common], "-lm", "-o", str(cls.binary)]
        if os.name == "nt": command.append("-ladvapi32")
        build = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if build.returncode: raise AssertionError(build.stdout + build.stderr)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_each_register_against_modular_integer_and_exact_rational_references(self):
        run = subprocess.run([str(self.binary)], capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        lines = run.stdout.splitlines()
        self.assertEqual(len(lines), 8)
        for line in lines:
            kind, *values = line.split(); kind = int(kind)
            for i, value in enumerate(values):
                steps = 3 * (4 if kind % 2 == 0 else 32) if kind % 2 == 0 or i == 0 else 0
                if kind <= 4:
                    expected = ((i + 1) + steps * 3 if kind <= 2 else (i + 1) * pow(3, steps)) % (2**64)
                    self.assertEqual(int(value), expected)
                else:
                    expected = Fraction(i + 1) + Fraction(steps, 4) if kind <= 6 else Fraction(i + 1) * (1 + Fraction(1, 2**32))**steps
                    self.assertAlmostEqual(float(value), float(expected), delta=max(1, steps) * 2**-50)

    def test_compiled_payload_instruction_counts(self):
        objdump = shutil.which("objdump")
        if not objdump or sys.platform == "darwin": self.skipTest("GNU objdump instruction view unavailable")
        run = subprocess.run([objdump, "-d", str(self.binary)], capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        arm = platform.machine().lower() in {"arm64", "aarch64"}
        for name, mnemonic, count in (("arch_nop", "nop", 64), ("arch_iadd_dep", "add", 32),
                ("arch_iadd_ind8", "add", 32), ("arch_fadd_dep", "fadd" if arm else "addsd", 32),
                ("arch_fmul_ind8", "fmul" if arm else "mulsd", 32)):
            match = re.search(r"<" + name + r">:\n(.*?)(?=\n\n|\Z)", run.stdout, re.S)
            self.assertIsNotNone(match, name)
            self.assertGreaterEqual(len(re.findall(r"\b" + mnemonic + r"\b", match[1])), count, name)

    def test_bounded_native_full_pipeline(self):
        work = self.root / "report"; work.mkdir()
        run = subprocess.run([str(self.binary), "report"], cwd=work, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        raw = json.loads((work / "reports/cpu_architecture.json").read_text())
        evidence = read_architecture(raw)
        self.assertEqual(set(evidence["metrics"]), set(PROBES))
        result = build_module("cpu_architecture", work / "reports")
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.charts[0].kind, "architecture")
        if sys.platform.startswith("linux"):
            cpu = evidence["cpu_id"]
            expected = dict.fromkeys(("l1i", "l1d", "l2", "l3"), 0)
            for entry in Path(f"/sys/devices/system/cpu/cpu{cpu}/cache").glob("index*"):
                level, kind = (entry / "level").read_text().strip(), (entry / "type").read_text().strip()
                key = {("1", "Instruction"): "l1i", ("1", "Data"): "l1d",
                       ("2", "Unified"): "l2", ("3", "Unified"): "l3"}.get((level, kind))
                if key: expected[key] = int((entry / "size").read_text().strip().removesuffix("K")) * 1024
            self.assertEqual(evidence["reported_cache_bytes"], expected)

    @unittest.skipUnless(Path("/dev/full").exists(), "requires fail-on-write device")
    def test_audit_write_failure_is_not_success(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            reports = Path(directory) / "reports"; reports.mkdir()
            (reports / "cpu_architecture.json").symlink_to("/dev/full")
            run = subprocess.run([str(self.binary), "report"], cwd=directory, capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
