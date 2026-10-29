"""Independent references, retained work, counts and bounded diagnostic reports."""
from decimal import Decimal, localcontext
import math
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

from benchmark_registry import ROOT
from result_model import build_module, tables
from run_benchmarks import validate_report


def float32(value):
    return struct.unpack("f", struct.pack("f", value))[0]


class DiagnosticKernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="memorytest-diagnostic-")
        cls.root = Path(cls.temp.name)
        windows = os.name == "nt"
        sources = ["common.c", "platform.c", "report.c", "simd.c", "latency.c",
                   "util_windows.c" if windows else "util.c",
                   "detect_windows.c" if windows else "detect_macos.c" if sys.platform == "darwin" else "detect.c",
                   "pmu_macos.c" if windows or sys.platform == "darwin" else "pmu.c"]
        cls.binaries = {}
        for kind in ("branch", "float", "alu"):
            path = cls.root / (kind + (".exe" if windows else ""))
            command = shlex.split(os.environ.get("CC", "cc")) + ["-O2", "-std=c11", "-pthread",
                "-DBENCH_TARGET_NS=1000000", "-DBENCH_WARMUP_NS=500000",
                "-DBRANCH_PATTERN_SIZE=4096", str(ROOT / "tests/diagnostic_regression.c"),
                *[str(ROOT / "src" / source) for source in sources], "-lm", "-o", str(path)]
            if kind == "branch":
                command.append("-DCHECK_BRANCH")
            elif kind == "alu":
                command.append("-DCHECK_ALU")
            if windows:
                command.append("-ladvapi32")
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            cls.binaries[kind] = path

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def invoke(self, kind, *args, cwd=None):
        return subprocess.run([str(self.binaries[kind]), *args], capture_output=True, text=True,
                              check=True, timeout=15, cwd=cwd).stdout

    def test_branch_actual_conditional_path_and_pattern_work(self):
        lines = self.invoke("branch").splitlines()
        values = [255 if (i * 17 + 11) % 7 else 0 for i in range(4099)]
        self.assertEqual(list(map(int, lines[0].split())), [0, sum(bool(v) for v in values[:3]), sum(bool(v) for v in values)])
        for index, line in enumerate(lines[1:]):
            pattern, expected, actual, prefix = line.split()
            expected_bits = ([0] * 4099 if index == 0 else [1] * 4099 if index == 1 else
                             [(i // {2: 1, 3: 2, 4: 256, 5: 1024, 6: 1}[index]) % 2 for i in range(4099)])
            self.assertEqual(int(pattern), index)
            self.assertEqual(int(expected), sum(expected_bits))
            self.assertEqual(int(actual), sum(expected_bits))
            if index != 6:
                self.assertEqual(prefix, "".join(map(str, expected_bits[:32])))
            else:
                self.assertIn("00", prefix)
                self.assertIn("11", prefix)

    def test_float_all_lanes_and_operation_counts_against_independent_reference(self):
        for line in self.invoke("float").splitlines():
            name, operations, total, *lanes = line.split("|")
            actual = [float(value) for value in lanes]
            is_float, is_vector = name.startswith("float "), name.startswith("SIMD ")
            op = name.split(" ")[-1]
            count = 512 if is_vector else 256
            if op in {"FMA", "Mul+Add", "Dot"}:
                count *= 2
            if name == "Mixed":
                count *= 3
            self.assertEqual(int(operations), count, name)
            expected = []
            for lane in range(len(actual)):
                seed = 1 + lane / 4
                if op in {"Sqrt", "Sin", "Log", "Pow"}:
                    value = 0.0
                    for i in range(64):
                        x = 1 + (4 * i + lane) / 8
                        term = {"Sqrt": math.sqrt, "Sin": math.sin, "Log": math.log,
                                "Pow": lambda v: v ** 1.125}[op](x)
                        value = float32(value + float32(term)) if is_float else value + term
                elif op == "Dot":
                    # Arithmetic-series closed form for sum((seed+i/8)*(i+1)).
                    value = seed * 2080 + (85344 + 2016) / 8
                elif not is_float and name != "Mixed":
                    with localcontext() as ctx:
                        ctx.prec = 60
                        base, factor = Decimal(str(seed)), Decimal("1.0001") ** 64
                        value = float(base + 8 if op in {"Add", "FAdd"} else
                                      base * factor if op in {"Mul", "FMul"} else
                                      base / factor if op == "Div" else
                                      (base + 1250) * factor - 1250)
                else:
                    value = seed
                    for _ in range(64):
                        if name == "Mixed":
                            value = math.sqrt((value + .5) * 1.0001)
                        elif op == "Add":
                            value = float32(value + .125)
                        elif op == "Mul":
                            value = float32(value * float32(1.0001))
                        else:
                            value = float32(value / float32(1.0001))
                expected.append(value)
            for value, reference in zip(actual, expected):
                self.assertTrue(math.isfinite(value), name)
                self.assertAlmostEqual(value, reference, delta=max(1e-11, abs(reference) * (2e-7 if is_float else 2e-13)), msg=name)
            self.assertAlmostEqual(float(total), 3 * sum(expected), delta=max(1e-9, abs(float(total)) * 3e-7), msg=name)

    def test_native_rate_conversion_boundary(self):
        values = [line.split() for line in self.invoke("alu", "units").splitlines()]
        self.assertEqual([(float(value), unit) for value, unit in values],
                         [(99.9, "GOPS"), (0.1, "TOPS"), (1.25, "TOPS")])

    def test_report_nonzero_median_and_exact_work_divisors(self):
        for kind in ("branch", "float", "alu"):
            with tempfile.TemporaryDirectory(dir=self.root) as folder:
                self.invoke(kind, "report", cwd=folder)
                reports = Path(folder) / "reports"
                path = reports / f"cpu_{kind}_report.md"
                self.assertIsNone(validate_report(path))
                text = path.read_text()
                count = 20 if kind == "branch" else 10
                self.assertIn(f"- Samples: {count}", text)
                audit = json.loads((reports / f"cpu_{kind}_samples.json").read_text())
                self.assertEqual(audit["protocol"], "diagnostic-v3")
                self.assertTrue(audit["records"])
                for record in audit["records"]:
                    self.assertEqual(len(record["elapsed_ns"]), count)
                    self.assertGreaterEqual(record["statistics"]["kept"], count * .9)
                    self.assertEqual(record["statistics"]["count"], count)
                self.assertNotIn("Estimated", text)
                self.assertNotIn("Mispred Rate", text)
                measured_rows = 0
                for _, headers, rows in tables(text):
                    measure = "ns/branch" if kind == "branch" else next(
                        (header for header in ("Rate (GOPS)", "Rate (TOPS)") if header in headers), None)
                    if measure not in headers:
                        continue
                    for row in rows:
                        measured_rows += 1
                        value, milliseconds = float(row[headers.index(measure)]), float(row[headers.index("Time(ms)")])
                        work = float(row[headers.index("Branches" if kind == "branch" else "Operations")])
                        self.assertGreater(value, 0)
                        self.assertGreater(milliseconds, 0)
                        if kind == "branch":
                            self.assertAlmostEqual(value, milliseconds * 1e6 / work, delta=1e-6)
                        else:
                            expected_gops = work / (milliseconds * 1e6)
                            self.assertAlmostEqual(value * (1000 if measure == "Rate (TOPS)" else 1),
                                                   expected_gops, delta=expected_gops * 1e-7)
                        self.assertGreaterEqual(float(row[headers.index("Spread(%)")]), 0)
                self.assertGreater(measured_rows, 0)
                self.assertEqual(build_module(f"cpu_{kind}", reports).status, "passed")

    def test_alu_iteration_counts_and_uint64_reference(self):
        actual = {row.split()[0]: int(row.split()[1]) for row in self.invoke("alu").splitlines()}
        count, mask = 3 * 256, (1 << 64) - 1
        triangle = count * (count - 1) // 2
        expected = {"Add": triangle, "Sub": (-triangle) & mask, "Mul": pow(3, count, 1 << 64),
                    "Div": 1, "AND": 0, "OR": 1023, "XOR": 0, "Count": count, "Load+ALU": triangle}
        left, right, remainder = 1, mask, 0
        for i in range(count):
            left = ((left << 1) | 1) & mask
            if left > mask >> 1:
                left = 1
            right >>= 1
            if right < 1024:
                right = mask
            remainder = remainder % 1000003 + i + 1
        expected.update(SHL=left, SHR=right, Mod=remainder)
        self.assertEqual(actual, expected)

    @unittest.skipUnless(sys.platform.startswith("linux") and platform.machine() == "x86_64" and shutil.which("objdump"), "x86 Linux disassembly check")
    def test_release_branch_kernel_retains_two_conditional_jumps(self):
        assembly = subprocess.check_output(["objdump", "-d", str(self.binaries["branch"])], text=True)
        body = re.search(r"<branch_count(?:\.[^>]*)?>:\n(.*?)(?=\n\n)", assembly, re.S).group(1)
        self.assertRegex(body, r"\bje\b")
        self.assertRegex(body, r"\bjne\b")
        self.assertRegex(body, r"\bcmpb\b")

    @unittest.skipUnless(sys.platform.startswith("linux") and platform.machine() == "x86_64" and shutil.which("objdump"), "x86 Linux disassembly check")
    def test_integer_chains_keep_register_work_and_real_multiply_divide(self):
        assembly = subprocess.check_output(["objdump", "-d", str(self.binaries["alu"])], text=True)
        for name, instruction in (("add", "add"), ("mul", "imul"), ("div", "div"), ("mod", "div")):
            body = re.search(r"<test_" + name + r"(?:\.[^>]*)?>:\n(.*?)(?=\n\n)", assembly, re.S).group(1)
            self.assertRegex(body, r"\b" + instruction + r"\b")
            self.assertNotRegex(body, r"\([^\n)]*%(?:rsp|rbp)\b")
