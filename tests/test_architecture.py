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
import xml.etree.ElementTree as ET
from benchmark_registry import ROOT
from architecture_analysis import PROBES, BTB_COUNTS, CODE_BYTES, read_architecture
from result_model import build_module
from frontend_width_analysis import fetch_candidate, read_width_scan, sustained_widths


def fixture():
    return {"schema_version": 1, "protocol": "architecture-probes-v1", "isa": "aarch64-fp64",
        "placement": "macos-qos-user-initiated", "cpu_id": -1, "sample_count": 10,
        "sample_target_ns": 50_000_000, "timer": "monotonic-wall-time", "cache_source": "OS-reported-not-probed",
        "reported_cache_bytes": {"l1i": 131072, "l1d": 65536, "l2": 0, "l3": 0},
        "records": [{"probe": name, "blocks": 1_000_000, "operations_per_block": operations,
                     "validated": True, "elapsed_ns": [100_000_000 + 1_000_000 * i for i in range(5)] * 2}
                    for name, operations in PROBES.items()]}


def frontend_fixture(footprint_knee=False):
    data = fixture()
    data.update(schema_version=2, protocol="architecture-probes-v2")
    records = []
    for kind, count, stride in [("btb", n, s) for s in (8, 16, 32) for n in BTB_COUNTS] + [
            ("nop", size // 4, 4) for size in CODE_BYTES]:
        work = count * 100_000
        slow = kind == "btb" and (count*stride >= 32768 if footprint_knee else count >= 4096)
        records.append({"kind": kind, "count": count, "stride": stride, "code_bytes": count*stride,
            "blocks": 100_000, "elapsed_ns": [work * (2 if slow else 1)] * 10,
            "cycles": [work // 4] * 10, "instructions": [work + 400_000] * 10})
    data["frontend"] = {"protocol": "frontend-static-v1", "sample_target_ns": 1_000_000,
        "counter_source": "macos-rusage-process-user-and-kernel", "records": records}
    return data


class ArchitectureContracts(unittest.TestCase):
    def test_width_inference_against_independent_packet_enumeration(self):
        # Independent reference: enumerate the distinct fetch packets touched
        # by each instruction byte, rather than use the inference equation.
        for width in (16, 32, 64):
            points = []
            for n in range(4, 65):
                for crossed in (0, 1):
                    start = 65536-4 if crossed else 65536
                    packets = len({address//width for address in range(start, start+4*n)})
                    points.append(dict(count=n, crossed=crossed, code_bytes=4*n, stable=True,
                        counter_stable=True, ns_per_iteration=0.3*max(packets, n/10)+.05))
            result = fetch_candidate(points)
            self.assertIsNotNone(result)
            self.assertEqual(result["value"], width)
            for point in points:
                point["cycles_per_iteration"] = (point["ns_per_iteration"]-.05)/.3*2
            self.assertIsNone(fetch_candidate(points, use_cycles=True))
            # A common linear bottleneck or a cache-only staircase must not
            # identify a physical width without a distinct page penalty.
            for point in points: point["ns_per_iteration"] = point["count"]*.1
            self.assertIsNone(fetch_candidate(points))

    def test_width_layout_samples_and_decoder_path_gates(self):
        raw = {"protocol": "page-boundary-width-v1", "page_bytes": 16384, "sample_target_ns": 1_000_000,
               "counter_source": "unavailable", "decode_counter_source": "unavailable", "records": []}
        for n in range(4, 65):
            for crossed in (0, 1):
                raw["records"].append({"count": n, "crossed": crossed, "code_bytes": 4*n,
                    "page_offset": 16380 if crossed else 0, "blocks": 1_000_000,
                    "elapsed_ns": [n*100_000]*10, "cycles": [0]*10, "instructions": [0]*10, "mite_uops": [0]*10})
        parsed = read_width_scan(raw, "aarch64-fp64", "macos-qos-user-initiated")
        self.assertIsNone(parsed["fetch_width"])
        self.assertEqual(read_width_scan(raw, "aarch64-fp64", "linux-pinned-one-logical-cpu")["status"], "path_unverified")
        for field, value in (("code_bytes", 21), ("page_offset", 1), ("blocks", 500)):
            bad = copy.deepcopy(raw); bad["records"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                read_width_scan(bad, "aarch64-fp64", "macos-qos-user-initiated")
        evidence = read_architecture(frontend_fixture())["frontend"]
        decoded, retired = sustained_widths(evidence, "aarch64-fp64", "macos-qos-user-initiated", 131072)
        self.assertEqual(decoded["value"], 4)
        self.assertEqual(retired["value"], 4)
        self.assertIsNone(sustained_widths(evidence, "x86_64-sse2", "linux-pinned-one-logical-cpu", 32768)[0])

    def test_frontend_spacing_separates_target_count_from_code_footprint(self):
        result = read_architecture(frontend_fixture())["frontend"]
        self.assertEqual(result["btb_transitions"], [{"lower": 3072, "upper": 4096, "spacings": [8, 16, 32]}])
        self.assertEqual(result["peak_supply_ipc"], 4)
        self.assertIsNone(result["fetch_width"])
        self.assertIsNone(result["decode_width"])
        self.assertEqual(read_architecture(frontend_fixture(True))["frontend"]["btb_transitions"], [])

    def test_frontend_requires_complete_raw_samples_and_hardware_source(self):
        for mode in ("duplicate", "partial", "footprint", "counter", "zero-cycles", "missing"):
            data = frontend_fixture(); frontend = data["frontend"]
            if mode == "duplicate": frontend["records"][-1] = frontend["records"][0]
            if mode == "partial": frontend["records"][0]["cycles"].pop()
            if mode == "footprint": frontend["records"][0]["code_bytes"] += 4
            if mode == "counter": frontend["counter_source"] = "unavailable"
            if mode == "zero-cycles": frontend["records"][0]["cycles"][0] = 0
            if mode == "missing": data.pop("frontend")
            with self.subTest(mode=mode), self.assertRaises(ValueError): read_architecture(data)
        data = frontend_fixture(); data["frontend"]["counter_source"] = "unavailable"
        for row in data["frontend"]["records"]: row["cycles"] = row["instructions"] = [0]*10
        self.assertIsNone(read_architecture(data)["frontend"]["peak_supply_ipc"])

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

    def test_diagram_cards_and_svg_structure(self):
        from presentation import plot_svg
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            (reports / "cpu_architecture_report.md").write_text("# Architecture\n")
            (reports / "cpu_architecture.json").write_text(json.dumps(frontend_fixture()))
            result = build_module("cpu_architecture", reports)
            self.assertEqual(result.status, "passed")
            self.assertEqual((len(result.kpis), len(result.tables[0].rows)), (4, 12))
            for language in ("en", "zh"):
                svg = ET.fromstring(plot_svg(result.charts[0], language))
                self.assertEqual(svg.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertGreater(len(svg.findall("{http://www.w3.org/2000/svg}rect")), 5)

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
            (reports / "cpu_architecture.json").write_text(json.dumps(frontend_fixture()))
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
        version = subprocess.run([objdump, "--version"], capture_output=True, text=True, timeout=5)
        symbol_flag = "--disassemble-symbols=" if "LLVM" in version.stdout else "--disassemble="
        def body(name):
            run = subprocess.run([objdump, "-d", symbol_flag + name, str(self.binary)], capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stderr)
            match = re.search(r"<" + name + r">:\n(.*?)(?=\n\n|\Z)", run.stdout, re.S)
            self.assertIsNotNone(match, name)
            return match[1]
        arm = platform.machine().lower() in {"arm64", "aarch64"}
        for name, mnemonic, count in (("arch_nop", "nop", 64), ("arch_iadd_dep", "add", 32),
                ("arch_iadd_ind8", "add", 32), ("arch_fadd_dep", "fadd" if arm else "addsd", 32),
                ("arch_fmul_ind8", "fmul" if arm else "mulsd", 32)):
            self.assertGreaterEqual(len(re.findall(r"\b" + mnemonic + r"\b", body(name))), count, name)
        # Independent machine-code reference: exact taken-target spacing and
        # explicit NOP instruction length, excluding call/prologue/return.
        for name, mnemonic, count, stride in (("fe_btb_128_8", "b" if arm else "jmp", 128, 8),
                ("fe_nop_128_4", "nop" if arm else "nopl", 32, 4)):
            payload = body(name).split("ret")[0]
            addresses = [int(v, 16) for v in re.findall(r"^\s*([0-9a-f]+):[^\n]*\b" + mnemonic + r"\s", payload, re.M)]
            self.assertEqual(len(addresses), count, name)
            self.assertEqual([b-a for a, b in zip(addresses, addresses[1:])], [stride]*(count-1))

    def test_page_boundary_machine_bytes_and_forced_branch_encoding(self):
        run = subprocess.run([str(self.binary), "layout"], capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(run.stdout.splitlines()), 6)
        arm = platform.machine().lower() in {"arm64", "aarch64"}
        alignment = (16384 if arm else 4096) if sys.platform == "darwin" else 4096 if os.name == "nt" else 65536
        for line in run.stdout.splitlines():
            n, crossed, offset, code = line.split()
            n, crossed, offset, code = int(n), int(crossed), int(offset), bytes.fromhex(code)
            self.assertEqual(offset, alignment-4 if crossed else 0)
            self.assertEqual(len(code), 4*n + (0 if arm else 1))
            self.assertEqual(code[:4*(n-2)], bytes.fromhex("1f2003d5" if arm else "0f1f4000")*(n-2))
            if arm:
                branch = int.from_bytes(code[-4:], "little")
                self.assertEqual(branch & 0xff00001f, 0x54000001)
                displacement = (branch >> 5) & 0x7ffff
                if displacement & 0x40000: displacement -= 0x80000
                self.assertEqual(displacement*4, -(len(code)-4))
            else:
                self.assertEqual(code[-6:-4], bytes.fromhex("0f85"))
                self.assertEqual(int.from_bytes(code[-4:], "little", signed=True), -len(code))

    def test_bounded_native_full_pipeline(self):
        work = self.root / "report"; work.mkdir()
        run = subprocess.run([str(self.binary), "report"], cwd=work, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        raw = json.loads((work / "reports/cpu_architecture.json").read_text())
        evidence = read_architecture(raw)
        output = ROOT / "build/architecture-evidence"
        output.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work / "reports/cpu_architecture.json", output / "cpu_architecture.json")
        shutil.copy2(work / "reports/cpu_architecture_report.md", output / "cpu_architecture_report.md")
        self.assertEqual(set(evidence["metrics"]), set(PROBES))
        result = build_module("cpu_architecture", work / "reports")
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.charts[0].kind, "architecture")
        self.assertGreaterEqual(len(result.charts), 4)
        self.assertEqual(len(evidence["width_scan"]["records"]), 122)
        self.assertEqual(len(evidence["frontend"]["records"]), 57 if platform.machine().lower() in {"arm64", "aarch64"} else 81)
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
