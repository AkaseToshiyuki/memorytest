#!/usr/bin/env python3
"""
Memory Benchmark Report Generator

Generates PDF reports with visualizations.

Usage:
    python3 generate_report.py --all                # Run all tests and generate PDF report
    python3 generate_report.py --test <test_name> # Run single test and generate report
    python3 generate_report.py --list              # List available tests
"""

import os
import sys
import subprocess
import argparse
import getpass
import json
import time
from datetime import datetime

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch, cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
    from reportlab.graphics.shapes import Drawing, Rect
    from reportlab.graphics import renderPDF
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(SCRIPT_DIR, "bin")
REPORTS_DIR = os.path.join(SCRIPT_DIR, "reports")
CHARTS_DIR = os.path.join(REPORTS_DIR, "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

# Global sudo password cache
_sudo_password = None

TESTS_CONFIG_PATH = os.path.join(SCRIPT_DIR, "tests.json")


def load_tests_config(path=TESTS_CONFIG_PATH):
    """Load the unified test registry from tests.json.

    Returns a (tests, categories) tuple where:
      - tests: dict keyed by test name with all merged fields
      - categories: dict mapping category -> list of test names
    Raises FileNotFoundError or ValueError on schema problems.
    """
    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)

    if "tests" not in config or not isinstance(config["tests"], dict):
        raise ValueError(f"Invalid tests.json: missing 'tests' object in {path}")

    tests = config["tests"]
    # Derive categories from tests if not explicitly provided.
    if "categories" in config and isinstance(config["categories"], dict):
        categories = config["categories"]
    else:
        categories = {}
        for name, info in tests.items():
            cat = info.get("category", "uncategorized")
            categories.setdefault(cat, []).append(name)

    # Sanity check: every test listed in a category must exist in tests.
    for cat, names in categories.items():
        missing = [n for n in names if n not in tests]
        if missing:
            raise ValueError(
                f"Invalid tests.json: category '{cat}' references unknown tests {missing}"
            )

    return tests, categories


TESTS, CATEGORIES = load_tests_config()
MEMORY_TESTS = CATEGORIES.get("memory", [])
CPU_TESTS = CATEGORIES.get("cpu", [])


def get_sudo_password():
    """Get sudo password once and cache it"""
    global _sudo_password
    if _sudo_password is None:
        try:
            _sudo_password = getpass.getpass("sudo password: ")
        except EOFError:
            _sudo_password = ""
    return _sudo_password


def verify_sudo_once():
    """Verify sudo once at the beginning, cache the credential"""
    password = get_sudo_password()
    if not password:
        return False
    try:
        proc = subprocess.run(
            ['sudo', '-S', '-v'],
            input=password + '\n',
            text=True,
            capture_output=True
        )
        return proc.returncode == 0
    except Exception:
        return False


def ensure_dirs():
    """Ensure build and reports directories exist"""
    os.makedirs(BUILD_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(CHARTS_DIR, exist_ok=True)


def drop_caches():
    """Try to clear OS page cache. Requires root; falls back to plain sync().

    Returns True if a cleanup attempt succeeded (even non-root sync).
    Ported from legacy run_tests.py.
    """
    try:
        subprocess.run(["sync"], capture_output=True)
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3")
        print("[cache] drop_caches=3 (root)")
        return True
    except (PermissionError, IOError):
        try:
            subprocess.run(["sync"], capture_output=True)
            print("[cache] sync only (no root)")
            return True
        except Exception:
            return False
    except Exception:
        return False


def cleanup_before_next_test():
    """Drop page caches between tests to reduce measurement noise."""
    print("-" * 40)
    print("[cache] clearing between tests...")
    drop_caches()
    time.sleep(1)
    print("-" * 40)
    print()


def build_test(test_name):
    """Build a single test"""
    if test_name not in TESTS:
        print(f"Error: Unknown test '{test_name}'")
        return False

    test = TESTS[test_name]
    bin_path = os.path.join(BUILD_DIR, test["bin"])

    if os.path.exists(bin_path):
        return True

    print(f"Building {test_name}...")
    result = subprocess.run(
        ["make", "-C", SCRIPT_DIR, test["bin"]],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"Build failed:\n{result.stderr}")
        return False
    return True


def run_test(test_name, verbose=False):
    """Run a single test and generate its report"""
    global _sudo_password
    if test_name not in TESTS:
        print(f"Error: Unknown test '{test_name}'")
        return False

    test = TESTS[test_name]
    bin_path = os.path.join(BUILD_DIR, test["bin"])

    if not os.path.exists(bin_path):
        if not build_test(test_name):
            return False

    print(f"\n{'='*60}")
    print(f"Running: {test['name']}")
    print(f"{'='*60}")

    # Ensure reports/ exists for stdout redirect target
    report_path = os.path.join(REPORTS_DIR, test["report"])
    os.makedirs(REPORTS_DIR, exist_ok=True)

    if _sudo_password:
        result = subprocess.run(
            ['sudo', '-S', bin_path],
            cwd=SCRIPT_DIR,
            input=_sudo_password + '\n',
            text=True,
            capture_output=True  # capture stdout (the markdown report) so we can persist it
        )
    else:
        result = subprocess.run(
            [bin_path],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True
        )

    if result.returncode != 0:
        print(f"Test failed: {test_name} (rc={result.returncode})")
        if result.stderr:
            print(result.stderr[:500])
        return False

    # Persist captured stdout to the expected markdown report path.
    # Each binary prints a full markdown report to stdout; without this redirect
    # the report file would never exist and downstream PDF generation would
    # see zero passes.
    try:
        with open(report_path, 'w', encoding='utf-8') as fh:
            fh.write(result.stdout)
        print(f"\n[Report] Generated: {report_path} ({len(result.stdout)} bytes)")
    except OSError as exc:
        print(f"\n[Error] Could not write report: {exc}")
        return False

    return True


def list_tests():
    """List all available tests"""
    print("\nAvailable tests:\n")
    print(f"{'Test Name':<20} | {'Description':<45} | {'Binary'}")
    print("-" * 90)

    for name, info in TESTS.items():
        bin_path = os.path.join(BUILD_DIR, info["bin"])
        status = "[x]" if os.path.exists(bin_path) else "[ ]"
        print(f"{status} {name:<18} | {info['description']:<45} | {info['bin']}")

    print("\nUsage:")
    print("  python3 generate_report.py --all           # Run all tests")
    print("  python3 generate_report.py --test <name>   # Run single test")


def _parse_table_row(line: str) -> list:
    """Parse a single table row, supporting both markdown (`| a | b |`)
    and ASCII-aligned (`a | b`) flavours our binaries emit."""
    if '|' not in line:
        return [c.strip() for c in line.split()]
    cells = [c.strip() for c in line.split('|')]
    # Drop the leading/trailing empty fragments that come from leading/trailing `|`
    if cells and not cells[0]:
        cells = cells[1:]
    if cells and not cells[-1]:
        cells = cells[:-1]
    return cells


def _find_table_after(text: str, header_keyword: str, max_skip: int = 0):
    """Find a markdown/ASCII table whose header line contains header_keyword.

    The first table whose header line is followed by a delimiter line of
    `-` `|` `:` characters is returned as (headers, rows). Empty separator
    lines AND sub-section headers like "-- ALU --" are skipped within the
    table body.

    Args:
        text: full report text
        header_keyword: substring that must appear in the header line
        max_skip: number of leading matching tables to skip (use 0 for the
            first table; useful when the file has a "system info" table
            first)
    """
    import re
    delim_re = re.compile(r'^[\s\-:|]+\|?$')
    section_re = re.compile(r"^--\s+.+\s+--$")
    lines = text.splitlines()
    skipped = 0
    i = 0
    while i < len(lines) - 1:
        line_i = lines[i]
        line_ip1 = lines[i + 1]
        if header_keyword.lower() in line_i.lower() and delim_re.match(line_ip1):
            if skipped < max_skip:
                # Advance past this table so we look for the next one.
                j = i + 2
                while j < len(lines):
                    rl = lines[j]
                    if not rl.strip():
                        j += 1
                        continue
                    if section_re.match(rl):
                        j += 1
                        continue
                    if "|" not in rl:
                        break
                    if delim_re.match(rl):
                        break
                    j += 1
                i = j
                skipped += 1
                continue
            headers = _parse_table_row(line_i)
            rows = []
            j = i + 2
            while j < len(lines):
                rl = lines[j]
                if not rl.strip():
                    j += 1
                    continue
                if section_re.match(rl):
                    j += 1
                    continue
                if "|" not in rl:
                    break
                if delim_re.match(rl):
                    break
                cells = _parse_table_row(rl)
                if len(cells) == len(headers):
                    rows.append(cells)
                j += 1
            return headers, rows
        i += 1
    return None


def parse_cache_report(report_path):
    """Parse cache hierarchy report data.

    The cache binary writes either a markdown-style table
    (`| Size | RdLat(ns) | ...`) or an ASCII-aligned variant
    (`Size | RdLat(ns) | ...`). The new `_find_table_after` helper
    handles both, plus the multi-channel bandwidth tables that
    follow. We pick the FIRST table whose header mentions "Size"
    AND "RdLat" (or "Latency") — that's the latency table.
    """
    data = {'sizes': [], 'rd_lat': [], 'wr_lat': [], 'bw': [], 'expected': []}
    try:
        text = open(report_path).read()
        # The first such table is the latency table
        tbl = _find_table_after(text, "RdLat", max_skip=0)
        if tbl is None:
            tbl = _find_table_after(text, "Latency", max_skip=0)
        if tbl is None:
            return data
        headers, rows = tbl
        # Locate columns by header name (case-insensitive, fuzzy)
        def col_idx(*candidates):
            for i, h in enumerate(headers):
                hl = h.lower()
                for c in candidates:
                    if c in hl:
                        return i
            return None
        size_col = col_idx("size")
        rd_col = col_idx("rdlat", "read")
        wr_col = col_idx("wrlat", "write")
        bw_col = col_idx("bw", "bandwidth")
        exp_col = col_idx("expected")
        if size_col is None: return data
        # Default to columns 0,1,2,3,4 if header names didn't match
        if rd_col is None: rd_col = 1
        if wr_col is None: wr_col = 2
        if bw_col is None: bw_col = 3
        if exp_col is None: exp_col = 4
        for row in rows:
            try:
                size = row[size_col]
                rd_lat = float(row[rd_col])
                wr_lat = float(row[wr_col])
                bw = float(row[bw_col])
                expected = row[exp_col] if exp_col < len(row) else ""
                data['sizes'].append(size)
                data['rd_lat'].append(rd_lat)
                data['wr_lat'].append(wr_lat)
                data['bw'].append(bw)
                data['expected'].append(expected)
            except (ValueError, IndexError):
                pass
    except Exception as e:
        print(f"Error parsing cache report: {e}")
    return data


def parse_multi_core_report(report_path):
    """Parse multi-core report data for scaling charts.

    The multi-core binary writes a markdown/ASCII table with columns like:
    `Operation | Category | Threads | Ops/sec | ns/op | Speedup | Efficiency`.
    We look for any table whose header contains both "Operation" AND
    ("Speedup" OR "Efficiency") so we tolerate header renames between
    binary versions.
    """
    data = {'operations': {}, 'thread_counts': [1, 2, 4, 8, 16, 24]}
    try:
        text = open(report_path).read()
        tbl = _find_table_after(text, "Speedup", max_skip=0)
        if tbl is None:
            tbl = _find_table_after(text, "Efficiency", max_skip=0)
        if tbl is None:
            tbl = _find_table_after(text, "Operation", max_skip=0)
        if tbl is None:
            return data
        headers, rows = tbl
        def col_idx(*candidates):
            for i, h in enumerate(headers):
                hl = h.lower()
                for c in candidates:
                    if c in hl:
                        return i
            return None
        op_col = col_idx("operation")
        sp_col = col_idx("speedup")
        eff_col = col_idx("efficiency")
        if op_col is None or sp_col is None or eff_col is None:
            return data
        for row in rows:
            try:
                op = row[op_col]
                speedup = float(row[sp_col].replace('x', '').strip())
                eff = float(row[eff_col].replace('%', '').strip())
                if op not in data['operations']:
                    data['operations'][op] = {'speedup': [], 'efficiency': []}
                data['operations'][op]['speedup'].append(speedup)
                data['operations'][op]['efficiency'].append(eff)
            except (ValueError, IndexError):
                pass
    except Exception as e:
        print(f"Error parsing multi-core report: {e}")
    return data


def create_cache_latency_chart(data, output_path):
    """Create cache latency visualization chart"""
    if not HAS_MATPLOTLIB or not data['sizes']:
        return None

    try:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

        # Latency chart
        x = range(len(data['sizes']))
        width = 0.35
        ax1.bar([i - width/2 for i in x], data['rd_lat'], width, label='Read', color='steelblue')
        ax1.bar([i + width/2 for i in x], data['wr_lat'], width, label='Write', color='coral')
        ax1.set_xlabel('Size')
        ax1.set_ylabel('Latency (ns)')
        ax1.set_title('Cache Hierarchy Latency')
        ax1.set_xticks(x)
        ax1.set_xticklabels(data['sizes'], rotation=45, ha='right')
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        # Bandwidth chart
        ax2.plot(data['sizes'], data['bw'], marker='o', linewidth=2, color='green')
        ax2.set_xlabel('Size')
        ax2.set_ylabel('Bandwidth (MB/s)')
        ax2.set_title('Cache Hierarchy Bandwidth')
        ax2.set_xticks(range(len(data['sizes'])))
        ax2.set_xticklabels(data['sizes'], rotation=45, ha='right')
        ax2.grid(True, alpha=0.3)
        ax2.set_yscale('log')

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return output_path
    except Exception as e:
        print(f"Error creating cache chart: {e}")
        return None


def create_multi_core_scaling_chart(data, output_path):
    """Create multi-core scaling visualization"""
    if not HAS_MATPLOTLIB or not data['operations']:
        return None

    try:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
        threads = data['thread_counts']

        colors = ['steelblue', 'coral', 'green', 'orange']
        for i, (op, values) in enumerate(data['operations'].items()):
            if values['speedup']:
                ax1.plot(threads[:len(values['speedup'])], values['speedup'],
                        marker='o', linewidth=2, label=op, color=colors[i % len(colors)])
                ax2.plot(threads[:len(values['efficiency'])], values['efficiency'],
                        marker='s', linewidth=2, label=op, color=colors[i % len(colors)])

        # Ideal scaling line
        ax1.plot(threads, threads, 'k--', alpha=0.5, label='Ideal')

        ax1.set_xlabel('Threads')
        ax1.set_ylabel('Speedup')
        ax1.set_title('Multi-Core Speedup')
        ax1.legend(loc='upper left')
        ax1.grid(True, alpha=0.3)
        ax1.set_xticks(threads)

        ax2.axhline(y=100, color='k', linestyle='--', alpha=0.5, label='Ideal (100%)')
        ax2.set_xlabel('Threads')
        ax2.set_ylabel('Efficiency (%)')
        ax2.set_title('Multi-Core Efficiency')
        ax2.legend(loc='upper right')
        ax2.grid(True, alpha=0.3)
        ax2.set_xticks(threads)
        ax2.set_ylim(0, 110)

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return output_path
    except Exception as e:
        print(f"Error creating multi-core chart: {e}")
        return None


def create_bandwidth_chart(report_path, output_path):
    """Create memory bandwidth chart.

    The bandwidth binary writes a textual summary like::

        === BANDWIDTH RESULTS ===
          Read:    36279.58 MB/s
          Write:   17463.33 MB/s
          Copy:    23687.22 MB/s

    (no markdown/ASCII table). We extract values via regex; falls back
    to a multi-channel bandwidth table (`Size | Read | Write | Copy`)
    if the textual summary is absent.
    """
    if not HAS_MATPLOTLIB:
        return None

    try:
        import re
        text = open(report_path).read()
        read_bw = write_bw = copy_bw = None
        patterns = {
            "Read":  r"Read:\s+([0-9]+\.[0-9]+)\s*MB/s",
            "Write": r"Write:\s+([0-9]+\.[0-9]+)\s*MB/s",
            "Copy":  r"Copy:\s+([0-9]+\.[0-9]+)\s*MB/s",
        }
        for op, pat in patterns.items():
            m = re.search(pat, text)
            if m:
                v = float(m.group(1))
                if op == "Read":  read_bw  = v
                elif op == "Write": write_bw = v
                else:               copy_bw  = v

        # Fallback: try the multi-channel bandwidth table (Size | Read | Write | Copy)
        if read_bw is None:
            tbl = _find_table_after(text, "Read", max_skip=0)
            if tbl:
                headers, rows = tbl
                # Average across all rows for a stable single-value summary
                def col_idx(*cands):
                    for i, h in enumerate(headers):
                        hl = h.lower()
                        for c in cands:
                            if c in hl:
                                return i
                    return None
                r_col = col_idx("read")
                w_col = col_idx("write")
                c_col = col_idx("copy")
                r_vals = [float(row[r_col]) for row in rows if r_col is not None and r_col < len(row)]
                w_vals = [float(row[w_col]) for row in rows if w_col is not None and w_col < len(row)]
                c_vals = [float(row[c_col]) for row in rows if c_col is not None and c_col < len(row)]
                if r_vals: read_bw = max(r_vals)  # use peak across buffer sizes
                if w_vals: write_bw = max(w_vals)
                if c_vals: copy_bw = max(c_vals)

        if read_bw is None and write_bw is None and copy_bw is None:
            return None

        fig, ax = plt.subplots(figsize=(8, 5))
        categories = ['Read', 'Write', 'Copy']
        values = [read_bw, write_bw, copy_bw]
        colors = ['steelblue', 'coral', 'green']

        bars = ax.bar(categories, values, color=colors)
        ax.set_ylabel('Bandwidth (MB/s)')
        ax.set_title('Memory Bandwidth')
        ax.grid(True, alpha=0.3, axis='y')

        for bar, val in zip(bars, values):
            if val is None: continue
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 500,
                   f'{val:.0f}', ha='center', va='bottom')

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return output_path
    except Exception as e:
        print(f"Error creating bandwidth chart: {e}")
        return None


def create_cpu_chart(report_path, output_path, chart_type='alu'):
    """Create CPU performance chart"""
    if not HAS_MATPLOTLIB:
        return None

    try:
        with open(report_path, 'r') as f:
            content = f.read()

        ops, ns_ops = [], []
        in_table = False

        for line in content.split('\n'):
            if 'ns/op' in line and '|' in line:
                in_table = True
                continue
            if in_table and '|' in line:
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 5 and parts[1] not in ['Operation', '---', '-', '']:
                    try:
                        op = parts[1]
                        ns_op = float(parts[4])
                        ops.append(op)
                        ns_ops.append(ns_op)
                    except:
                        pass
            elif in_table and line.strip() == '':
                break

        if not ops:
            return None

        fig, ax = plt.subplots(figsize=(10, 6))
        y_pos = range(len(ops))
        ax.barh(y_pos, ns_ops, color='steelblue')
        ax.set_yticks(y_pos)
        ax.set_yticklabels(ops)
        ax.set_xlabel('ns/op')
        ax.set_title(f'CPU {chart_type.upper()} Operations - Latency')
        ax.grid(True, alpha=0.3, axis='x')
        ax.invert_yaxis()

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return output_path
    except Exception as e:
        print(f"Error creating CPU chart: {e}")
        return None


def create_inter_core_heatmap(report_path, output_path):
    """Create inter-core latency heatmap.

    The inter-core binary writes a 24x24 matrix using SPACE-delimited
    floats (NOT `|`). The matrix starts after `=== CAS Latency (ns) ===`
    and continues until `=== CAS Throughput` (or end of file).
    Each row begins with the source core id; the second cell is `-`
    (self-pair sentinel); the rest of the cells are the cross-core
    latencies in nanoseconds.
    """
    if not HAS_MATPLOTLIB:
        return None

    try:
        text = open(report_path).read()
        matrix = []
        in_matrix = False
        for line in text.splitlines():
            if "CAS Latency" in line and "ns" in line:
                in_matrix = True
                continue
            if "CAS Throughput" in line:
                break
            if not in_matrix:
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                src = int(parts[0])
            except ValueError:
                continue
            # Find the self-pair sentinel. The matrix is space-delimited
            # and the sentinel is the "-" sitting at column (src) of
            # the matrix row. We scan up to position 5 because the
            # leftmost columns may be present (so sentinel_idx can
            # range from 1 to 5 for src=0..4).
            sentinel_idx = None
            for idx in range(1, min(6, len(parts))):
                if parts[idx] == "-":
                    sentinel_idx = idx
                    break
            if sentinel_idx is None:
                continue
            # Latencies to ALL cores. The full NxN matrix should be
            # exactly N cells long (excluding src and the sentinel).
            # Pad/truncate as needed to keep rows rectangular.
            row_vals = []
            for k, p in enumerate(parts[1:], start=1):
                if k == sentinel_idx:
                    continue  # skip the self-sentinel
                try:
                    row_vals.append(float(p))
                except ValueError:
                    row_vals.append(0.0)
            if len(row_vals) >= 4:
                matrix.append(row_vals)

        if not matrix or len(matrix) < 4:
            return None

        # Pad rows to consistent width (some rows may be shorter on 1st pass)
        ncols = max(len(r) for r in matrix)
        for r in matrix:
            while len(r) < ncols:
                r.append(0.0)

        fig, ax = plt.subplots(figsize=(12, 10))
        im = ax.imshow(matrix, cmap='YlOrRd', aspect='auto')
        ax.set_title('Inter-Core CAS Latency (ns)', fontsize=14)
        ax.set_xlabel('Core ID', fontsize=12)
        ax.set_ylabel('Core ID', fontsize=12)

        n = len(matrix)
        ax.set_xticks(range(n))
        ax.set_yticks(range(n))
        ax.set_xticklabels(range(n))
        ax.set_yticklabels(range(n))

        for i in range(n):
            for j in range(len(matrix[i])):
                val = matrix[i][j]
                row_min = min(matrix[i])
                row_max = max(matrix[i])
                text_color = 'white' if val > (row_max + row_min) / 2 else 'black'
                ax.text(j, i, f'{val:.1f}', ha='center', va='center',
                       color=text_color, fontsize=6)

        cbar = fig.colorbar(im, ax=ax)
        cbar.set_label('Latency (ns)', fontsize=10)

        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return output_path
    except Exception as e:
        print(f"Error creating heatmap: {e}")
        return None


def extract_bandwidth_data(report_path):
    """Extract bandwidth data from report.

    The bandwidth binary writes a textual summary like::

        === BANDWIDTH RESULTS ===
          Read:    36279.58 MB/s
          Write:   17463.33 MB/s
          Copy:    23687.22 MB/s

    (no markdown table), so we use regex. Falls back to a markdown table
    parser if the textual summary is absent.
    """
    import re
    data = []
    try:
        text = open(report_path).read()
        # Primary: regex against the textual summary
        for op in ("Read", "Write", "Copy"):
            m = re.search(rf"{op}:\s+([0-9]+\.[0-9]+)\s*MB/s", text)
            if m:
                bw = float(m.group(1))
                # Theoretical peak is roughly 76 GB/s for 2-channel DDR4-4800
                # per the binary's "理论峰值" line. Compute efficiency
                # against it as a sanity-check number.
                peak_m = re.search(r"理论峰值[^\d]*([0-9]+\.?[0-9]*)\s*GB/s", text)
                if peak_m:
                    peak = float(peak_m.group(1)) * 1000  # GB/s → MB/s
                    eff = f"{(bw / peak * 100):.1f}%"
                else:
                    eff = "-"
                data.append((op, bw, eff))
        if data:
            return data
        # Fallback: try a markdown/ASCII table (multi-channel bandwidth)
        tbl = _find_table_after(text, "Read", max_skip=0)
        if tbl:
            headers, rows = tbl
            def col_idx(*cands):
                for i, h in enumerate(headers):
                    hl = h.lower()
                    for c in cands:
                        if c in hl:
                            return i
                return None
            r_col = col_idx("read")
            w_col = col_idx("write")
            c_col = col_idx("copy")
            for row in rows:
                if r_col is not None and r_col < len(row):
                    try: data.append(("Read", float(row[r_col]), "-"))
                    except ValueError: pass
                if w_col is not None and w_col < len(row):
                    try: data.append(("Write", float(row[w_col]), "-"))
                    except ValueError: pass
                if c_col is not None and c_col < len(row):
                    try: data.append(("Copy", float(row[c_col]), "-"))
                    except ValueError: pass
    except Exception:
        pass
    return data

def extract_cpu_alu_data(report_path):
    """Extract CPU ALU data from report.

    Recognises both canonical markdown (`| Op | ...`) and ASCII-aligned
    (`Op | ...`) header lines, and skips sub-section headers like
    `-- ALU --` and blank lines.
    """
    import re
    data = []
    in_table = False
    delim_re = re.compile(r'^[\s\-:|]+\|?$')
    section_re = re.compile(r"^--\s+.+\s+--$")
    try:
        text = open(report_path).read()
        for line in text.splitlines():
            if not in_table and 'ns/op' in line and '|' in line:
                in_table = True
                continue
            if in_table:
                if not line.strip():
                    continue  # skip blank sub-section separators
                if section_re.match(line):
                    continue
                if delim_re.match(line):
                    continue  # skip delimiter
                if '|' not in line:
                    break
                parts = [p.strip() for p in line.split('|')]
                # Detect column offset: if parts[0] is a known header label
                # (Operation, Add, Sub, ...), shift left by one.
                if parts[0] in ('Operation', '', '-') and len(parts) >= 7:
                    cells = parts[1:-1] if not parts[-1] else parts[1:]
                else:
                    cells = parts
                if len(cells) >= 5 and cells[0] and cells[0] not in ('Operation',):
                    data.append(cells[:5])
    except Exception:
        pass
    return data


def extract_cpu_float_data(report_path):
    """Extract CPU float data from report (recognises both markdown and ASCII tables)."""
    import re
    data = []
    in_table = False
    delim_re = re.compile(r'^[\s\-:|]+\|?$')
    section_re = re.compile(r"^--\s+.+\s+--$")
    try:
        text = open(report_path).read()
        for line in text.splitlines():
            if not in_table and 'ns/op' in line and '|' in line:
                in_table = True
                continue
            if in_table:
                if not line.strip():
                    continue  # skip blank sub-section separators
                if section_re.match(line):
                    continue
                if delim_re.match(line):
                    continue
                if '|' not in line:
                    break
                parts = [p.strip() for p in line.split('|')]
                if parts[0] in ('Operation', '', '-') and len(parts) >= 7:
                    cells = parts[1:-1] if not parts[-1] else parts[1:]
                else:
                    cells = parts
                if len(cells) >= 5 and cells[0] and cells[0] not in ('Operation',):
                    data.append(cells[:6])
    except Exception:
        pass
    return data


def extract_multi_core_data(report_path):
    """Extract multi-core scaling data (recognises both markdown and ASCII tables)."""
    import re
    data = []
    in_table = False
    delim_re = re.compile(r'^[\s\-:|]+\|?$')
    section_re = re.compile(r"^--\s+.+\s+--$")
    try:
        text = open(report_path).read()
        for line in text.splitlines():
            if not in_table and 'Operation' in line and 'Category' in line and '|' in line:
                in_table = True
                continue
            if in_table:
                if not line.strip():
                    continue  # skip blank sub-section separators
                if section_re.match(line):
                    continue
                if delim_re.match(line):
                    continue
                if '|' not in line:
                    break
                parts = [p.strip() for p in line.split('|')]
                if parts[0] in ('Operation', '', '-') and len(parts) >= 8:
                    cells = parts[1:-1] if not parts[-1] else parts[1:]
                else:
                    cells = parts
                if len(cells) >= 6 and cells[0] and cells[0] not in ('Operation',):
                    # Strip the trailing 'x' from the Speedup column.
                    # Multi-core binary writes "1.00      x" (with the unit
                    # on a separate column visually but merged into the
                    # previous cell by the table parser). The header has
                    # 7 columns: Operation, Category, Threads, Time(ms),
                    # Speedup, Efficiency, Status → speedup is cells[4].
                    if len(cells) >= 5:
                        cells[4] = cells[4].replace('x', '').strip()
                    data.append(cells[:6])
    except Exception:
        pass
    return data

def extract_inter_core_stats(report_path):
    """Extract inter-core latency statistics.

    The inter-core binary writes a space-delimited 24x24 matrix plus a
    summary block. We extract min/max/avg by parsing the matrix itself
    (so this works for any matrix size, not just 24x24).
    """
    import re
    stats = {}
    try:
        text = open(report_path).read()
        # Try the textual summary block first (1-hop Avg: ..., 2-hop Avg: ...)
        m = re.search(r"1-hop Avg:\s*([0-9]+\.?[0-9]*)\s*ns", text)
        if m:
            stats["1-hop Avg"] = f"{m.group(1)} ns"
        m = re.search(r"2-hop Avg:\s*([0-9]+\.?[0-9]*)\s*ns", text)
        if m:
            stats["2-hop Avg"] = f"{m.group(1)} ns"
        # Try the Range/Avg summary at the end of the matrix
        m = re.search(r"Range:\s*([0-9]+\.?[0-9]*)\s*-\s*([0-9]+\.?[0-9]*)\s*ns", text)
        if m:
            stats["Min Latency"] = f"{m.group(1)} ns"
            stats["Max Latency"] = f"{m.group(2)} ns"
        m = re.search(r"Avg:\s*([0-9]+\.?[0-9]*)\s*ns", text)
        if m:
            stats["Avg Latency"] = f"{m.group(1)} ns"
        # If we already have stats from the summary, return them
        if stats:
            return stats
        # Fallback: parse the matrix itself
        matrix = []
        in_matrix = False
        for line in text.splitlines():
            if "CAS Latency" in line and "ns" in line:
                in_matrix = True
                continue
            if "CAS Throughput" in line:
                break
            if not in_matrix:
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                int(parts[0])
            except ValueError:
                continue
            for idx in range(1, min(6, len(parts))):
                if parts[idx] == "-":
                    # Latencies are at all positions EXCEPT the sentinel.
                    vals = [float(p) for p in parts[1:] if p != "-" and p.replace('.','').isdigit()]
                    if vals:
                        matrix.append(vals)
                    break
        if matrix:
            all_vals = [v for row in matrix for v in row if v > 0]
            if all_vals:
                stats["Min Latency"] = f"{min(all_vals):.1f} ns"
                stats["Max Latency"] = f"{max(all_vals):.1f} ns"
                stats["Avg Latency"] = f"{(sum(all_vals) / len(all_vals)):.1f} ns"
                stats["Pairs Measured"] = f"{len(all_vals)}"
    except Exception:
        pass
    return stats

def generate_pdf_report():
    """Generate final PDF report with charts"""
    if not HAS_REPORTLAB:
        print("\n[Info] reportlab not installed. Skipping PDF generation.")
        print("Install with: pip install reportlab matplotlib numpy")
        return True  # Return True so the test doesn't fail

    print("\n" + "="*70)
    print("GENERATING PDF REPORT")
    print("="*70)

    # Get device info
    import socket
    hostname = socket.gethostname()
    try:
        with open('/etc/machine-id', 'r') as f:
            device_id = f.read().strip()[:8]
    except:
        device_id = hostname[:8]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    pdf_path = os.path.join(REPORTS_DIR, "benchmark_report.pdf")
    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                           rightMargin=72, leftMargin=72,
                           topMargin=72, bottomMargin=72)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        spaceAfter=30
    )
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=16,
        spaceAfter=12,
        spaceBefore=20
    )

    # Watermark function — minimal footer only (the diagonal "CONFIDENTIAL"
    # was previously causing "ALTIENDFINOC"-style vertical text to appear in
    # PDF extractors and looked like a rendering bug; removed).
    def add_watermark(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.grey)
        wm_text = f"{timestamp} | {hostname} | {device_id}"
        canvas.drawRightString(A4[0] - 72, 36, wm_text)
        canvas.restoreState()

    story = []

    # Title
    story.append(Paragraph("Memory Benchmark Report", title_style))
    story.append(Paragraph(f"<i>Generated: {timestamp}</i>", styles['Normal']))
    story.append(Spacer(1, 10))

    # Device Info Box
    device_info = [
        ['Device ID', device_id],
        ['Hostname', hostname],
        ['Report Time', timestamp],
    ]
    t = Table(device_info, colWidths=[1.5*inch, 3*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.Color(0.95, 0.95, 1.0)),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOX', (0, 0), (-1, -1), 1, colors.Color(0.7, 0.7, 0.9)),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.Color(0.8, 0.8, 0.9)),
    ]))
    story.append(t)
    story.append(Spacer(1, 20))

    # System Info Section (at beginning)
    cache_report = os.path.join(REPORTS_DIR, "cache_hierarchy_report.md")
    sys_info = []
    if os.path.exists(cache_report):
        with open(cache_report, 'r') as f:
            content = f.read()
            in_config = False
            for line in content.split('\n'):
                if 'System Configuration' in line:
                    in_config = True
                    continue
                if in_config and '|' in line and '-' not in line:
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 3 and parts[1]:
                        sys_info.append([parts[1], parts[2]])
                elif in_config and line.strip() == '':
                    break

    if sys_info:
        story.append(Paragraph("System Configuration", heading_style))
        t = Table(sys_info, colWidths=[2*inch, 3*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        story.append(t)
        story.append(Spacer(1, 20))

    # Generate charts
    charts_generated = []

    # Cache hierarchy chart
    if os.path.exists(cache_report):
        data = parse_cache_report(cache_report)
        chart_path = os.path.join(CHARTS_DIR, "cache_latency.png")
        if create_cache_latency_chart(data, chart_path):
            charts_generated.append(('Cache Hierarchy', chart_path))

    # Multi-core scaling chart
    multi_report = os.path.join(REPORTS_DIR, "cpu_multi_core_report.md")
    if os.path.exists(multi_report):
        data = parse_multi_core_report(multi_report)
        chart_path = os.path.join(CHARTS_DIR, "multi_core_scaling.png")
        if create_multi_core_scaling_chart(data, chart_path):
            charts_generated.append(('Multi-Core Scaling', chart_path))

    # Memory bandwidth chart
    bw_report = os.path.join(REPORTS_DIR, "memory_bandwidth_report.md")
    if os.path.exists(bw_report):
        chart_path = os.path.join(CHARTS_DIR, "memory_bandwidth.png")
        if create_bandwidth_chart(bw_report, chart_path):
            charts_generated.append(('Memory Bandwidth', chart_path))

    # CPU ALU chart
    alu_report = os.path.join(REPORTS_DIR, "cpu_alu_report.md")
    if os.path.exists(alu_report):
        chart_path = os.path.join(CHARTS_DIR, "cpu_alu.png")
        if create_cpu_chart(alu_report, chart_path, 'alu'):
            charts_generated.append(('CPU ALU', chart_path))

    # CPU Float chart
    float_report = os.path.join(REPORTS_DIR, "cpu_float_report.md")
    if os.path.exists(float_report):
        chart_path = os.path.join(CHARTS_DIR, "cpu_float.png")
        if create_cpu_chart(float_report, chart_path, 'float'):
            charts_generated.append(('CPU Float', chart_path))

    # Inter-core heatmap
    ic_report = os.path.join(REPORTS_DIR, "inter_core_latency_report.md")
    if os.path.exists(ic_report):
        chart_path = os.path.join(CHARTS_DIR, "inter_core_heatmap.png")
        if create_inter_core_heatmap(ic_report, chart_path):
            charts_generated.append(('Inter-Core Latency', chart_path))

    # Cache Hierarchy Section
    if os.path.exists(cache_report):
        story.append(Paragraph("Cache Hierarchy Test", heading_style))
        data = parse_cache_report(cache_report)
        if data['sizes']:
            table_data = [['Size', 'Rd Lat (ns)', 'Wr Lat (ns)', 'BW (MB/s)', 'Expected']]
            for i, size in enumerate(data['sizes']):
                if i < len(data['rd_lat']):
                    table_data.append([
                        size,
                        f"{data['rd_lat'][i]:.2f}",
                        f"{data['wr_lat'][i]:.2f}",
                        f"{data['bw'][i]:.0f}",
                        data['expected'][i] if i < len(data['expected']) else ''
                    ])
            t = Table(table_data, colWidths=[1*inch, 1.2*inch, 1.2*inch, 1.2*inch, 1*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "cache_latency.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=4*inch))
        story.append(PageBreak())

    # Memory Bandwidth Section
    bw_report = os.path.join(REPORTS_DIR, "memory_bandwidth_report.md")
    if os.path.exists(bw_report):
        story.append(Paragraph("Memory Bandwidth Test", heading_style))
        bw_data = extract_bandwidth_data(bw_report)
        if bw_data:
            table_data = [['Operation', 'Bandwidth (MB/s)', 'Efficiency']]
            for op, bw, eff in bw_data:
                table_data.append([op, f"{bw:.2f}", eff])
            t = Table(table_data, colWidths=[1.5*inch, 1.5*inch, 1.5*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "memory_bandwidth.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=4*inch))
        story.append(PageBreak())

    # CPU ALU Section
    alu_report = os.path.join(REPORTS_DIR, "cpu_alu_report.md")
    if os.path.exists(alu_report):
        story.append(Paragraph("CPU ALU Test", heading_style))
        alu_data = extract_cpu_alu_data(alu_report)
        if alu_data:
            # alu_data columns: Operation, Time(ms), Ops/sec, ns/op, CPI
            table_data = [['Operation', 'Time(ms)', 'Ops/sec', 'ns/op', 'CPI']]
            for row in alu_data:
                if len(row) >= 5:
                    table_data.append([row[0], row[1], f"{float(row[2]):.0f}", row[3], row[4]])
            t = Table(table_data, colWidths=[1.2*inch, 1*inch, 1.3*inch, 0.8*inch, 0.8*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "cpu_alu.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=4*inch))
        story.append(PageBreak())

    # CPU Float Section
    float_report = os.path.join(REPORTS_DIR, "cpu_float_report.md")
    if os.path.exists(float_report):
        story.append(Paragraph("CPU Floating-Point Test", heading_style))
        float_data = extract_cpu_float_data(float_report)
        if float_data:
            table_data = [['Operation', 'Type', 'Ops/sec', 'ns/op', 'CPI', 'IPC']]
            for row in float_data:
                if len(row) >= 6:
                    table_data.append([row[0], row[1], f"{float(row[2]):.0f}", row[3], row[4], row[5]])
            t = Table(table_data, colWidths=[1*inch, 0.8*inch, 1.3*inch, 0.8*inch, 0.7*inch, 0.7*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "cpu_float.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=4*inch))
        story.append(PageBreak())

    # Multi-Core Section
    multi_report = os.path.join(REPORTS_DIR, "cpu_multi_core_report.md")
    if os.path.exists(multi_report):
        story.append(Paragraph("Multi-Core Scaling Test", heading_style))
        multi_data = extract_multi_core_data(multi_report)
        if multi_data:
            # multi_data columns: Operation, Category, Threads, Time(ms), Speedup, Efficiency
            table_data = [['Operation', 'Category', 'Threads', 'Time(ms)', 'Speedup', 'Efficiency']]
            for row in multi_data:
                if len(row) >= 6:
                    try:
                        table_data.append([row[0], row[1], row[2], row[3], f"{float(row[4]):.2f}", row[5]])
                    except (ValueError, IndexError):
                        table_data.append(row[:6])
            t = Table(table_data, colWidths=[0.9*inch, 0.7*inch, 0.6*inch, 0.9*inch, 0.8*inch, 0.9*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                ('TOPPADDING', (0, 0), (-1, -1), 2),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "multi_core_scaling.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=4*inch))
        story.append(PageBreak())

    # Inter-Core Section
    ic_report = os.path.join(REPORTS_DIR, "inter_core_latency_report.md")
    if os.path.exists(ic_report):
        story.append(Paragraph("Inter-Core Latency Test", heading_style))
        ic_stats = extract_inter_core_stats(ic_report)
        if ic_stats:
            table_data = [['Metric', 'Value']]
            for k, v in ic_stats.items():
                table_data.append([k, v])
            t = Table(table_data, colWidths=[2*inch, 2*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(t)
            story.append(Spacer(1, 10))
        chart_path = os.path.join(CHARTS_DIR, "inter_core_heatmap.png")
        if os.path.exists(chart_path):
            story.append(Image(chart_path, width=6*inch, height=5*inch))

    # Build PDF with watermark
    doc.build(story, onFirstPage=add_watermark, onLaterPages=add_watermark)
    print(f"\n[PDF] Report generated: {pdf_path}")
    return True


def run_all_tests(verbose=False, test_names=None, clean_cache=True):
    """Run a sequence of tests (defaults to all) and (optionally) clean caches between them.

    Args:
        verbose: pass-through to run_test()
        test_names: iterable of test ids; defaults to all tests in TESTS
        clean_cache: if True (default), call cleanup_before_next_test() between runs
    """
    if test_names is None:
        test_names = list(TESTS.keys())
    # Validate
    unknown = [t for t in test_names if t not in TESTS]
    if unknown:
        print(f"Error: unknown test(s): {unknown}")
        print("Use --list to see available tests.")
        return False

    print("\n" + "="*70)
    print(f"RUNNING {len(test_names)} BENCHMARK TEST(S)")
    print("="*70)

    ensure_dirs()

    print("\n[Info] Verifying sudo credentials...")
    if verify_sudo_once():
        print("[Info] sudo credentials cached successfully")
    else:
        print("[Info] Running without sudo")

    results = {}
    for i, test_name in enumerate(test_names, 1):
        print(f"\n[{i}/{len(test_names)}] ", end="")
        success = run_test(test_name, verbose)
        results[test_name] = success

        if clean_cache and i < len(test_names):
            cleanup_before_next_test()

    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, success in results.items():
        status = "[PASS]" if success else "[FAIL]"
        print(f"  {status} {TESTS[test_name]['name']}")

    print(f"\nTotal: {passed}/{total} passed")

    # Generate PDF report if dependencies available
    if passed > 0:
        generate_pdf_report()

    return passed == total


def main():
    parser = argparse.ArgumentParser(
        description="Memory Benchmark Report Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument("--test", "-t", type=str,
                        help="Run a single test and generate its report")
    parser.add_argument("--all", "-a", action="store_true",
                        help="Run all tests and generate PDF report")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List all available tests")
    # Additive flags (ported from legacy run_tests.py)
    parser.add_argument("--memory", "-m", action="store_true",
                        help="Run only memory/cache subsystem tests")
    parser.add_argument("--cpu", "-c", action="store_true",
                        help="Run only CPU performance tests")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output")
    parser.add_argument("--no-cache-cleanup", action="store_true",
                        help="Skip drop_caches between tests (faster, noisier)")

    args = parser.parse_args()

    ensure_dirs()

    if args.list:
        list_tests()
        return

    clean_cache = not args.no_cache_cleanup

    if args.test:
        run_test(args.test, verbose=args.verbose)
    elif args.memory:
        run_all_tests(verbose=args.verbose, test_names=MEMORY_TESTS, clean_cache=clean_cache)
    elif args.cpu:
        run_all_tests(verbose=args.verbose, test_names=CPU_TESTS, clean_cache=clean_cache)
    elif args.all:
        run_all_tests(verbose=args.verbose, clean_cache=clean_cache)
    else:
        parser.print_help()
        print("\nExamples:")
        print("  python3 generate_report.py --list")
        print("  python3 generate_report.py --all")
        print("  python3 generate_report.py --test cache_hierarchy")
        print("  python3 generate_report.py --memory")
        print("  python3 generate_report.py --cpu")


if __name__ == "__main__":
    main()
