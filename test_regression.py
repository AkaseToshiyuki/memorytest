#!/usr/bin/env python3
from __future__ import annotations
"""Memorytest Layer 3: Regression detection across runs.

Records each test run's metrics to history.jsonl and compares current
metrics against a baseline (default: median of last 3 runs). Detects
performance regressions where a metric deviates from baseline by more
than a configurable threshold.

Usage:
    python3 test_regression.py                # record + compare (default baseline=3)
    python3 test_regression.py --baseline 1   # compare to most recent
    python3 test_regression.py --baseline 5   # compare to median of last 5
    python3 test_regression.py --record-only  # just record, skip compare
    python3 test_regression.py --warn-pct 15 --fail-pct 30  # built-in defaults shown explicitly

Exit code 0 if all within tolerance, 1 if any metric fails the fail-pct.
"""

import argparse
import datetime
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
REPORTS = ROOT / "reports"
HISTORY = ROOT / "history.jsonl"


# ============================================================================
# Per-report parsers. Each returns dict {metric_key: float}.
# Column-aware: we read the header line to find which column index to take.
# This is more robust than label-matching across heterogeneous report formats.
# ============================================================================

def _find_data_table(text: str, skip_first: bool = True) -> tuple[list[str], list[list[str]]] | None:
    """Find a markdown data table with a header + separator.

    Returns (headers, rows) where headers is a list of stripped strings and
    rows is a list of lists of stripped cell strings. Returns None if no
    suitable table is found.

    skip_first=True: skip the first table (usually the System Info table
    with headers Item/Value) so we land on the actual benchmark data.
    """
    lines = text.splitlines()
    i = 0
    found = 0
    def _norm(l):
        """Normalise a table line: ensure it starts with | so both
        canonical (`| H |`) and ASCII-aligned (`H | H |`) formats work."""
        s = l.strip()
        return s if s.startswith("|") else "| " + s
    while i < len(lines) - 1:
        li = _norm(lines[i])
        li1 = _norm(lines[i + 1])
        if "|" in li and re.match(r"^\|[\s\-|:]+\|?\s*$", li1):
            headers = [c.strip().rstrip("*").strip() for c in li.split("|")[1:-1]]
            rows = []
            j = i + 2
            while j < len(lines):
                lj = _norm(lines[j])
                if "|" not in lj:
                    break
                cells = [c.strip().rstrip("*").strip() for c in lj.split("|")[1:-1]]
                if len(cells) == len(headers):
                    rows.append(cells)
                j += 1
            if found == 0 and skip_first:
                found = 1
                i = j
                continue
            return headers, rows
        i += 1
    return None


def _col_index(headers: list[str], *candidates: str) -> int | None:
    """Find first column whose header matches any candidate (case-insensitive)."""
    norm = [h.lower() for h in headers]
    for cand in candidates:
        cand_l = cand.lower()
        for i, h in enumerate(norm):
            if cand_l in h:
                return i
    return None


def _safe_float(s: str) -> float | None:
    s = s.replace(",", "").strip()
    try:
        v = float(s)
        return v if v == v else None  # NaN check
    except (ValueError, TypeError):
        return None


def _row_label(row: list[str]) -> str:
    """First cell of row, normalized."""
    return row[0].strip().rstrip("*").strip() if row else ""


def parse_cache_hierarchy(text: str) -> dict:
    """Pick one latency reading per cache level: L1 (16KB), L2 (256KB), L3 (24MB), RAM (256MB)."""
    out = {}
    tbl = _find_data_table(text)
    if not tbl:
        return out
    headers, rows = tbl
    rd_col = _col_index(headers, "rdlat", "read latency", "latency")
    if rd_col is None:
        rd_col = 1  # second column is usually RdLat

    # We want rows that match these size labels
    targets = {
        "l1d_latency_ns": ["16KB", "32KB"],
        "l2_latency_ns":  ["256KB", "512KB"],
        "l3_latency_ns":  ["12MB", "24MB"],
        "ram_latency_ns": ["48MB", "64MB", "256MB"],
    }
    for row in rows:
        lbl = _row_label(row)
        for key, candidates in targets.items():
            if key in out:
                continue
            if any(c in lbl for c in candidates):
                v = _safe_float(row[rd_col])
                if v is not None and 0.1 < v < 1000:  # sanity
                    out[key] = v
                break
    return out


def parse_memory_bandwidth(text: str) -> dict:
    """Read/Write/Copy bandwidth (second column)."""
    out = {}
    tbl = _find_data_table(text)
    if not tbl:
        return out
    headers, rows = tbl
    bw_col = _col_index(headers, "bandwidth")
    if bw_col is None:
        bw_col = 1

    for row in rows:
        lbl = _row_label(row).lower()
        if "read" in lbl and "read_mbps" not in out:
            v = _safe_float(row[bw_col])
            if v and v > 100:
                out["read_mbps"] = v
        elif "write" in lbl and "write_mbps" not in out:
            v = _safe_float(row[bw_col])
            if v and v > 100:
                out["write_mbps"] = v
        elif "copy" in lbl and "copy_mbps" not in out:
            v = _safe_float(row[bw_col])
            if v and v > 100:
                out["copy_mbps"] = v
    return out


def parse_inter_core(text: str) -> dict:
    """Average same-socket (intra) and cross-socket (inter) CAS latency.

    The matrix is N×N where row[0] is the source core id (0-indexed in
    the table) and cells are ns; self-cell is marked '-'.
    """
    tbl = _find_data_table(text)
    if not tbl:
        return {}
    headers, rows = tbl
    # The matrix is wide: header is "| 0 | 1 | 2 | ... |"
    # Each row's first cell is the source core index.
    intra = []
    for row in rows:
        try:
            src = int(_row_label(row))
        except ValueError:
            continue
        # Find non-self cells (the cell whose column index matches src)
        for col_idx in range(1, len(row)):
            if col_idx - 1 == src:
                continue  # self
            cell = row[col_idx]
            if cell in ("-", ""):
                continue
            v = _safe_float(cell)
            if v is not None and 1 < v < 5000:
                intra.append(v)
    if intra:
        return {
            "cas_avg_ns": sum(intra) / len(intra),
            "cas_min_ns": min(intra),
            "cas_max_ns": max(intra),
        }
    return {}


def parse_cpu_alu(text: str) -> dict:
    """Pick IPC for Add / Mul / Div (6th column typically)."""
    out = {}
    tbl = _find_data_table(text)
    if not tbl:
        return out
    headers, rows = tbl
    ipc_col = _col_index(headers, "ipc")
    if ipc_col is None:
        # Fall back to last numeric column
        ipc_col = len(headers) - 2  # skip "Data Source"

    targets = {"add": "alu_add_ipc", "mul": "alu_mul_ipc", "div": "alu_div_ipc",
               "and": "alu_and_ipc", "or": "alu_or_ipc", "xor": "alu_xor_ipc"}
    for row in rows:
        lbl = _row_label(row).lower()
        for tgt_lbl, key in targets.items():
            if lbl == tgt_lbl and key not in out:
                v = _safe_float(row[ipc_col])
                if v is not None and 0 < v < 10:
                    out[key] = v
                break
    return out


def parse_cpu_float(text: str) -> dict:
    """Pick ns/op for SIMD FAdd/FMul/FMA (4th column)."""
    out = {}
    tbl = _find_data_table(text)
    if not tbl:
        return out
    headers, rows = tbl
    ns_col = _col_index(headers, "ns/op", "ns_per_op", "nsec/op")
    if ns_col is None:
        ns_col = 3

    targets = {"simd fadd": "simd_fadd_ns", "simd fmul": "simd_fmul_ns",
               "simd fma":  "simd_fma_ns",  "simd dot":  "simd_dot_ns"}
    for row in rows:
        lbl = _row_label(row).lower()
        for tgt_lbl, key in targets.items():
            if lbl == tgt_lbl and key not in out:
                v = _safe_float(row[ns_col])
                if v is not None and 0.01 < v < 1000:
                    out[key] = v
                break
    return out


PARSERS = {
    "cache_hierarchy": parse_cache_hierarchy,
    "memory_bandwidth": parse_memory_bandwidth,
    "inter_core_latency": parse_inter_core,
    "cpu_alu": parse_cpu_alu,
    "cpu_float": parse_cpu_float,
}


def collect_all_metrics() -> dict:
    """Parse all reports/*.md, return {metric_key: value}."""
    metrics = {}
    for category, parser in PARSERS.items():
        # Try exact match first, then prefix match
        candidates = list(REPORTS.glob(f"{category}_report.md"))
        if not candidates:
            candidates = list(REPORTS.glob(f"{category}*report*.md"))
        if not candidates:
            continue
        text = candidates[0].read_text()
        try:
            metrics.update(parser(text))
        except Exception as e:
            print(f"# WARN: parser {category} failed: {e}", file=sys.stderr)
    return metrics


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, text=True, timeout=5
        ).strip()
    except Exception:
        return "unknown"


def record(metrics: dict) -> dict:
    """Append current metrics to history.jsonl, return the record."""
    record_obj = {
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "git": git_sha(),
        "metrics": metrics,
    }
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as f:
        f.write(json.dumps(record_obj) + "\n")
    return record_obj


def load_history() -> list:
    if not HISTORY.exists():
        return []
    out = []
    for line in HISTORY.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def baseline_value(history: list, metric_key: str, n_baseline: int) -> float | None:
    """Return baseline for metric: median of last n_baseline values."""
    values = []
    for r in history[-n_baseline:]:
        v = r.get("metrics", {}).get(metric_key)
        if v is not None and v > 0:
            values.append(v)
    if not values:
        return None
    return statistics.median(values)


def compare(current: dict, history: list, n_baseline: int, warn_pct: float, fail_pct: float):
    """Compare current metrics vs baseline. Return (pass, warn, fail, details)."""
    pass_n = warn_n = fail_n = 0
    details = []
    for key, val in sorted(current.items()):
        base = baseline_value(history, key, n_baseline)
        if base is None:
            details.append((key, val, None, "no-baseline", None))
            continue
        delta_pct = (val - base) / base * 100.0
        if abs(delta_pct) >= fail_pct:
            fail_n += 1
            status = "FAIL"
        elif abs(delta_pct) >= warn_pct:
            warn_n += 1
            status = "WARN"
        else:
            pass_n += 1
            status = "pass"
        details.append((key, val, base, status, delta_pct))
    return pass_n, warn_n, fail_n, details


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", type=int, default=3, help="Number of recent runs to use as baseline (default 3, median)")
    ap.add_argument("--warn-pct", type=float, default=15.0, help="Warn threshold %% deviation (default 15)")
    ap.add_argument("--fail-pct", type=float, default=30.0, help="Fail threshold %% deviation (default 30)")
    ap.add_argument("--record-only", action="store_true", help="Just record current metrics, skip compare")
    ap.add_argument("--list", action="store_true", help="List history records and exit")
    args = ap.parse_args()

    if args.list:
        history = load_history()
        print(f"# {len(history)} history records in {HISTORY.name}")
        for r in history[-10:]:
            print(f"  {r['ts']}  git={r['git']}  metrics={len(r.get('metrics', {}))}")
        return 0

    current = collect_all_metrics()
    if not current:
        print("ERROR: no metrics found in reports/*.md — run 'make tests && bash test_smoke.sh' first", file=sys.stderr)
        return 1

    history = load_history()
    rec = record(current)
    print(f"# recorded {len(current)} metrics  ts={rec['ts']}  git={rec['git']}")
    print(f"# history: {len(history) + 1} total records (file: {HISTORY.name})")
    print(f"# current metrics: {sorted(current.keys())}")

    if args.record_only:
        return 0

    if len(history) < 1:
        print("# no history yet — this is the first run, recorded as baseline")
        return 0

    pass_n, warn_n, fail_n, details = compare(current, history, args.baseline, args.warn_pct, args.fail_pct)

    print(f"\n# compare to: median of {min(args.baseline, len(history))} most recent")
    print(f"# {pass_n} passed, {warn_n} warnings, {fail_n} failed (warn={args.warn_pct}% / fail={args.fail_pct}%)")
    print()
    print(f"  {'metric':<22} {'baseline':>12} {'latest':>12} {'delta':>10}  status")
    print(f"  {'-'*22} {'-'*12} {'-'*12} {'-'*10}  {'-'*6}")
    for d in details:
        key, val, base, status = d[:4]
        delta_pct = d[4] if len(d) > 4 else None
        if base is None:
            print(f"  {key:<22} {'(no baseline)':>12} {val:>12.4f} {'-':>10}  {status}")
        else:
            sign = "+" if delta_pct > 0 else ""
            print(f"  {key:<22} {base:>12.4f} {val:>12.4f} {sign}{delta_pct:>8.1f}%  {status}")

    return 1 if fail_n > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
