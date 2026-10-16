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
import struct
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


def resource_fixture():
    from backend_resource_analysis import FAMILIES
    knees = dict(rob_nop=512, prf_i32=192, prf_i64=128, prf_vec128=256, sched_int=48,
                 sched_vector=64, load_ready=224, load_wait=64, store_ready=256, store_wait=96)
    return {"protocol": "dual-ring-resource-v1", "status": "measured", "ring_bytes": 67108864,
        "line_bytes": 64, "scratch_bytes": 32832, "sample_target_ns": 1000000,
        "records": [{"family": f, "depth": d, "count": n, "serial": s, "blocks": 100000,
                     "elapsed_ns": [10000000 if s else 9500000 if n >= knees[f] else 5500000]*10}
                    for f, counts in FAMILIES.items() for d in (1, 2) for n in counts for s in (0, 1)]}


class BackendResourceContracts(unittest.TestCase):
    def test_effective_windows_need_depth_and_ready_control_agreement(self):
        from backend_resource_analysis import read_resources, window_candidate, SMALL
        raw = resource_fixture()
        result = read_resources(raw)
        self.assertEqual(result["candidates"]["prf_i64"]["upper"], 128)
        self.assertEqual(result["candidates"]["rob_nop"]["lower"], 448)
        self.assertTrue(all(c["status"] == "candidate" for c in result["candidates"].values()))
        # Throughput saturation has a smooth ratio curve, not an abrupt window.
        smooth = [{"depth": d, "count": n, "ratio": .55+.4*n/512, "stable": True} for d in (1, 2) for n in SMALL]
        self.assertNotEqual(window_candidate(smooth)["status"], "candidate")
        for row in raw["records"]:
            if row["family"] == "prf_i64" and row["depth"] == 2 and not row["serial"] and row["count"] == 128:
                row["elapsed_ns"] = [5500000]*10
        self.assertEqual(read_resources(raw)["candidates"]["prf_i64"]["status"], "depth_sensitive")
        raw = resource_fixture()
        for row in raw["records"]:
            if row["family"] == "rob_nop" and not row["serial"] and row["count"] >= 128:
                row["elapsed_ns"] = [9500000]*10
        self.assertEqual(read_resources(raw)["candidates"]["prf_i64"]["status"], "shared_limit")

    def test_resource_validation_missing_pairs_allocation_and_noise(self):
        from backend_resource_analysis import read_resources
        for mode in ("missing", "duplicate", "pair", "zero", "layout", "short", "retry"):
            raw = resource_fixture()
            if mode == "missing": raw["records"].pop()
            if mode == "duplicate": raw["records"][-1] = raw["records"][0]
            if mode == "pair": raw["records"][0]["blocks"] += 1
            if mode == "zero": raw["records"][0]["elapsed_ns"][0] = 0
            if mode == "layout": raw["scratch_bytes"] = 32768
            if mode == "short":
                raw["records"][0]["elapsed_ns"] = [249999]*10
                raw["records"][1]["elapsed_ns"] = [250000]*10
            if mode == "retry": raw["records"][0]["duration_retries"] = 1
            with self.subTest(mode=mode), self.assertRaises(ValueError): read_resources(raw)
        raw = resource_fixture(); raw["records"][0]["elapsed_ns"][0] *= 2
        self.assertEqual(read_resources(raw)["candidates"]["rob_nop"]["status"], "unstable")
        raw.update(status="allocation_failed", records=[])
        self.assertEqual(read_resources(raw)["candidates"], {})

    def test_btb_offset_votes_and_footprint_ambiguity(self):
        from backend_resource_analysis import read_btb_layout, BTB_COUNTS
        raw = {"protocol": "btb-layout-v1", "sample_target_ns": 1000000, "loop_control_branches": 1, "records": []}
        for stride, knee in zip((4, 8, 16, 32, 64), (128, 192, 256, 384, 512)):
            for offset in (0, 32):
                for n in BTB_COUNTS:
                    raw["records"].append(dict(count=n, stride=stride, offset=offset, actual_offset=offset,
                        code_bytes=n*stride, blocks=1000000, elapsed_ns=[n*1000000*(2 if n >= knee else 1)]*10))
        result = read_btb_layout(raw)
        self.assertEqual(len(result["transitions"]), 5)
        self.assertEqual({r["upper"] for r in result["transitions"]}, {128, 192, 256, 384, 512})
        for row in raw["records"]: row["elapsed_ns"] = [row["count"]*1000000*(2 if row["code_bytes"] >= 8192 else 1)]*10
        self.assertTrue(all(r["status"] == "footprint_or_index" for r in read_btb_layout(raw)["transitions"]))
        raw["records"][0]["actual_offset"] = 1
        with self.assertRaises(ValueError): read_btb_layout(raw)

    def test_competition_hardware_counter_floor_and_missing_counter_fallback(self):
        from backend_resource_analysis import read_competition, COMPETITION_SINGLE, COMPETITION_MIX
        raw = {"protocol": "mixed-throughput-v1", "sample_target_ns": 1000000, "chains_per_family": 4,
               "counter_source": "linux-perf-thread-user", "decode_counter_source": "unavailable", "records": []}
        for f in COMPETITION_SINGLE+COMPETITION_MIX:
            for n in (32, 64):
                ops = n*1000000
                raw["records"].append(dict(family=f, unroll=n, blocks=1000000, elapsed_ns=[ops]*10,
                                           cycles=[ops//4]*10, instructions=[ops+1000000]*10))
        result = read_competition(raw, "linux-pinned-one-logical-cpu")
        self.assertEqual(result["rename_width"]["value"], 4)
        self.assertEqual([m["gain"] for m in result["mixtures"]], [1]*6)
        raw["records"][0]["instructions"][0] = 1
        with self.assertRaises(ValueError): read_competition(raw, "linux-pinned-one-logical-cpu")
        raw["counter_source"] = "unavailable"
        for row in raw["records"]: row["cycles"] = row["instructions"] = [0]*10
        fallback = read_competition(raw, "os-scheduled-single-thread")
        self.assertIsNone(fallback["rename_width"])
        self.assertEqual([m["gain"] for m in fallback["mixtures"]], [1]*6)

    def test_cache_levels_remain_unassociated_with_sampled_cpu(self):
        raw = fixture()
        raw["cache_instances"] = [dict(level_id=0, name="Performance", logical_cpus=10, l1i=196608, l1d=131072, l2=25165824, cpus_per_l2=5),
                                  dict(level_id=1, name="Efficiency", logical_cpus=4, l1i=131072, l1d=65536, l2=4194304, cpus_per_l2=4)]
        parsed = read_architecture(raw)
        self.assertEqual([c["l1i"] for c in parsed["cache_instances"]], [196608, 131072])
        self.assertEqual(parsed["reported_cache_bytes"]["l1i"], 131072)
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            (reports / "cpu_architecture_report.md").write_text("# Architecture\n")
            (reports / "cpu_architecture.json").write_text(json.dumps(raw))
            model = build_module("cpu_architecture", reports)
            self.assertEqual(model.status, "passed", model.reason)
            self.assertEqual([row[1:] for row in model.tables[-1].rows], [[10, 192, 128, 24576], [4, 128, 64, 4096]])
        raw["sampled_cache_level_id"] = 0
        with self.assertRaises(ValueError): read_architecture(raw)


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
            window.tabs.setCurrentWidget(page)
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
            "-DBENCH_TARGET_NS=1000000", "-DBENCH_WARMUP_NS=500000", "-DARCH_RESOURCE_RING_BYTES=2097152", str(ROOT / "tests/architecture_regression.c"),
            *[str(ROOT / "src" / name) for name in common], "-lm", "-o", str(cls.binary)]
        if os.name == "nt": command.append("-ladvapi32")
        build = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if build.returncode: raise AssertionError(build.stdout + build.stderr)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_resource_pointers_stores_bounds_and_btb_machine_code(self):
        from backend_resource_analysis import resource_families, BTB_COUNTS as LAYOUT_COUNTS
        isa = "aarch64-fp64" if platform.machine().lower() in {"aarch64", "arm64"} else "x86_64-sse2"
        expected = {"resources": 4*sum(map(len, resource_families(isa).values())), "btb-layout": len(LAYOUT_COUNTS)*5*2}
        for mode, count in expected.items():
            run = subprocess.run([str(self.binary), mode], capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, (mode, run.stdout, run.stderr))
            self.assertEqual(int(run.stdout), count)

    def test_competition_all_results_against_independent_integer_rational_reference(self):
        run = subprocess.run([str(self.binary), "competition"], capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(run.stdout.splitlines()), 24)
        def bits(number): return struct.unpack("=Q", struct.pack("=d", float(number)))[0]
        for line in run.stdout.splitlines():
            family, unroll, *output = line.split()
            actual = [int(v, 16) for v in output]
            expected = list(range(1, 65))
            expected[8:16] = [bits(i+1) for i in range(8)]
            expected[32:34] = [bits(Fraction(1, 4)), bits(1+Fraction(1, 2**32))]
            streams = family.split("_")
            steps = 3*int(unroll)//(4*len(streams))
            for i in range(4):
                if "iadd" in streams: expected[i] += 3*steps
                if "imul" in streams: expected[4+i] = (5+i)*3**steps % 2**64
                if "fadd" in streams: expected[8+i] = bits(Fraction(1+i)+Fraction(steps, 4))
                if "fmul" in streams: expected[12+i] = bits(Fraction(5+i)*(1+Fraction(1, 2**32))**steps)
                if "load" in streams: expected[4+i] = 17+i
                if "store" in streams: expected[24+i] = 1+i
            for i, (a, b) in enumerate(zip(actual, expected)):
                # FP multiplication rounds at every step, unlike the exact
                # rational reference. Bound the accumulated error in ULPs.
                self.assertLessEqual(abs(a-b), steps if "fmul" in streams and 12 <= i < 16 else 0, (family, unroll, i))

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
        # The memory/pointer reference cannot catch a deleted filler stream.
        # Inspect emitted opcodes/destinations independently of macro counts.
        probes = [("prf_i32", r"\badd\s+w9," if arm else r"\blea[^\n]*%r9d", 128),
                  ("prf_i64", r"\badd\s+x9," if arm else r"\blea[^\n]*%r9\b", 128),
                  ("prf_vec128", r"\badd\s+v2\.2d," if arm else r"\bpshufd\b", 128),
                  ("load_wait", r"\bldr\s+x9," if arm else r"\bmov\s+[^\n]*\(%r10\),%r9\b", 128),
                  ("store_wait", r"\bstr\s+x10," if arm else r"\bmov\s+%r10,[^\n]*\(%r8\)", 128)]
        if arm: probes += [("prf_fp32", r"\bfadd\s+s2,", 128), ("prf_fp64", r"\bfadd\s+d2,", 128)]
        for family, pattern, count in probes:
            self.assertEqual(len(re.findall(pattern, body(f"res_{family}_1_{count}_0"))), count, family)
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
        run = subprocess.run([str(self.binary), "report"], cwd=work, capture_output=True, text=True, timeout=90)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        raw = json.loads((work / "reports/cpu_architecture.json").read_text())
        output = ROOT / "build/architecture-evidence"
        output.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work / "reports/cpu_architecture.json", output / "cpu_architecture.json")
        shutil.copy2(work / "reports/cpu_architecture_report.md", output / "cpu_architecture_report.md")
        evidence = read_architecture(raw)
        self.assertEqual(set(evidence["metrics"]), set(PROBES))
        result = build_module("cpu_architecture", work / "reports")
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.charts[0].kind, "architecture")
        self.assertGreaterEqual(len(result.charts), 4)
        self.assertEqual(len(evidence["width_scan"]["records"]), 122)
        self.assertEqual(len(evidence["resource_scan"]["records"]), 584 if evidence["isa"] == "aarch64-fp64" else 416)
        self.assertEqual(len(evidence["btb_layout_scan"]["records"]), 160)
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
            run = subprocess.run([str(self.binary), "report"], cwd=directory, capture_output=True, text=True, timeout=90)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
