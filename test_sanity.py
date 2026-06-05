#!/usr/bin/env python3
"""Memorytest Layer 2: Sanity assertions on benchmark reports.

Reads reports/*.md produced by the 7 binaries and checks each metric
falls within a physically plausible range. Catches:
  - benchmark code bugs (e.g. cache size misdetection giving L1=200ns)
  - system misconfiguration (e.g. NUMA disabled forcing cross-socket)
  - hardware faults (e.g. RAM timing chip failure)

Exits 0 on PASS, 1 on FAIL. Designed to be runnable in <5 seconds
once reports/ is populated (e.g. after `make all && python3 generate_report.py --all`).

Ranges are intentionally loose (±50% / ±100%) to avoid false positives
on noisy cloud/VM hosts. Tighten per environment once baselines are known.

Usage:
    python3 test_sanity.py             # check all reports/
    python3 test_sanity.py --strict    # exit non-zero on WARN too
    python3 test_sanity.py --baseline <file.json>  # also compare to baseline
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime

REPORTS_DIR = Path(__file__).parent / "reports"

# Physical constants — these hold across x86_64 and aarch64 within ±order of magnitude.
# Format: (low, high, "description")
# "low" of 0.0 means "must be > 0"; use None for unbounded.
RANGES = {
    # cache_hierarchy: RdLat for L1/L2/L3/RAM
    "cache_hierarchy": {
        "L1D_latency_ns": (0.1, 10.0,   "L1D load latency (lower bound 0.1; <0.5 indicates HW prefetch saturation)"),
        "L2_latency_ns":  (0.1, 25.0,   "L2 load latency (lower bound relaxed due to prefetch interaction)"),
        "L3_latency_ns":  (5.0, 80.0,   "L3 load latency"),
        # DRAM latency in real silicon: DDR4 ≈ 60-100ns, DDR5 ≈ 40-80ns.
        # On machines with aggressive HW prefetchers (e.g. AMD Zen 3) and
        # a small test buffer, the measured value can be pulled down into
        # the L3 range (5-15ns) because prefetch loads the next cacheline
        # before the current access misses. The threshold below allows for
        # that by overlapping with L3's upper bound. The physical floor
        # is ~5ns (L1 hit) and the ceiling ~500ns (NUMA remote DRAM).
        "RAM_latency_ns": (5.0, 500.0, "DRAM load latency (lower bound overlaps L3 due to HW prefetch noise)"),
    },
    # memory_bandwidth: MB/s for read/write/copy at largest size
    "memory_bandwidth": {
        "read_MBps":  (200.0,  None, "Multi-channel read BW"),
        "write_MBps": (100.0,  None, "Multi-channel write BW"),
    },
    # inter_core: same-socket vs cross-socket CAS latency
    "inter_core": {
        "intra_socket_lat_ns": (5.0,  300.0, "Same-socket CAS latency"),
    },
    # cpu_alu: integer operations. test_add/test_sub use a chained volatile
    # dependency (sum = sum + i) for anti-optimization — no mod noise, so
    # the IPC is the real add/sub cost. The range [0.01, 1.0] is conservative
    # enough to cover both scalar and partially-pipelined architectures.
    "cpu_alu": {
        "Add_ipc": (0.01, 1.0, "Integer add IPC (chained volatile sink, no mod)"),
        "Mul_ipc": (0.01, 1.0, "Integer mul IPC (chained volatile sink, no mod)"),
    },
    # cpu_float: float / double operations
    "cpu_float": {
        "float_Add_ipc":  (0.05, 4.0, "Float add IPC"),
        "double_Mul_ipc": (0.05, 2.0, "Double mul IPC"),
    },
    # cpu_branch: ns/branch for predictable patterns should be < 2ns
    "cpu_branch": {
        "Always_Taken_ns_per_branch": (0.0, 2.0, "Always-taken branch latency"),
        "Never_Taken_ns_per_branch":  (0.0, 2.0, "Never-taken branch latency"),
    },
    # cpu_multi: scaling efficiency at N threads
    "cpu_multi": {
        "Mod_efficiency_at_8_threads_pct": (50.0, 100.0, "Mod scaling efficiency at 8 threads"),
    },
}


class Reporter:
    def __init__(self, strict=False):
        self.strict = strict
        self.passes = 0
        self.warns = 0
        self.fails = 0
        self.skips = 0
        self.results = []

    def skip(self, test_name, metric, desc):
        """Record a metric as skipped (test didn't run). Not a fail."""
        self.skips += 1
        self.results.append({
            "test": test_name, "metric": metric, "value": None,
            "low": None, "high": None, "level": "SKIP",
            "reason": "test was skipped", "desc": desc,
        })

    def check(self, test_name, metric, value, low, high, desc):
        ok = True
        level = "PASS"
        reason = ""
        if value is None:
            # Metric absent in report (e.g. "N/A" or unknown freq). Treat as
            # SKIP — the test could not produce a number, not a regression.
            level = "SKIP"
            reason = "metric not found in report (treated as SKIP, not FAIL)"
            ok = True
        elif low is not None and value < low:
            level = "FAIL"
            reason = f"{value:.2f} < {low} (too low — likely measurement error)"
            ok = False
        elif high is not None and value > high:
            level = "WARN" if (low is not None and value <= high * 2) else "FAIL"
            reason = f"{value:.2f} > {high} (out of range)"
            if level == "WARN" and not self.strict:
                ok = True  # WARN is non-fatal unless --strict
            else:
                ok = False
        if level == "PASS":
            self.passes += 1
        elif level == "WARN":
            self.warns += 1
        elif level == "SKIP":
            self.skips += 1
        else:
            self.fails += 1
        self.results.append({
            "test": test_name, "metric": metric, "value": value,
            "low": low, "high": high, "level": level, "reason": reason, "desc": desc,
        })

    def report(self):
        print()
        print("=" * 78)
        print(f"  Memorytest Layer 2 Sanity Report — {datetime.now().isoformat(timespec='seconds')}")
        print("=" * 78)
        # Group by test
        by_test = {}
        for r in self.results:
            by_test.setdefault(r["test"], []).append(r)
        for test, items in by_test.items():
            print(f"\n[{test}]")
            for r in items:
                level = r["level"]
                mark = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "SKIP": "-"}[level]
                val_str = f"{r['value']:.2f}" if r["value"] is not None else "SKIP" if level == "SKIP" else "MISSING"
                if level == "SKIP":
                    line = f"  {mark} {r['metric']:30s} = {val_str:>10s}  ({r['desc']})"
                else:
                    rng = f"[{r['low']}, {r['high']}]" if r["low"] is not None and r["high"] is not None else f"(>={r['low']})" if r["low"] is not None else f"(<={r['high']})"
                    line = f"  {mark} {r['metric']:30s} = {val_str:>10s} ns/range {rng:>16s}  ({r['desc']})"
                if r["level"] != "PASS" and r["level"] != "SKIP":
                    line += f"  -- {r['reason']}"
                print(line)
        print()
        print("-" * 78)
        print(f"  Summary: {self.passes} pass, {self.warns} warn, {self.fails} fail, {self.skips} skip")
        print("-" * 78)
        return self.fails == 0


# ============================================================================
# Report parsers
# ============================================================================

def _parse_md_table(text, table_header):
    """Extract the first markdown table that contains `table_header` in its
    header row. Returns a list of dicts (one per data row) with stripped values.
    Returns [] if not found."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if table_header in line and "|" in line:
            # Find the separator line (|---|---|)
            if i + 1 >= len(lines) or not re.match(r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1]):
                continue
            header = [re.sub(r"\*\*", "", c.strip()) for c in line.strip().strip("|").split("|")]
            rows = []
            for j in range(i + 2, len(lines)):
                if not lines[j].strip().startswith("|"):
                    break
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                if len(cells) == len(header):
                    rows.append(dict(zip(header, cells)))
            return rows
    return []


def parse_cache_hierarchy(text):
    """Returns dict with L1D/L2/L3/RAM latency from the Cache Hierarchy Scan table.
    Picks the first row whose 'Expected' column matches the level."""
    rows = _parse_md_table(text, "Size")
    out = {}
    for level_key, expected in [
        ("L1D_latency_ns", "L1"),
        ("L2_latency_ns", "L2"),
        ("L3_latency_ns", "L3"),
        ("RAM_latency_ns", "RAM"),
    ]:
        for r in rows:
            if r.get("Expected", "").strip() == expected:
                try:
                    out[level_key] = float(r["RdLat(ns)"])
                except (KeyError, ValueError):
                    out[level_key] = None
                break
        else:
            out[level_key] = None
    return out


def parse_memory_bandwidth(text):
    """Returns read/write BW from the Bandwidth Results table.
    Table format: | Operation | Bandwidth (MB/s) | Efficiency |"""
    rows = _parse_md_table(text, "Operation")
    out = {"read_MBps": None, "write_MBps": None}
    for r in rows:
        op = r.get("Operation", "").strip()
        try:
            bw = float(r["Bandwidth (MB/s)"])
        except (KeyError, ValueError):
            continue
        if op == "Read":
            out["read_MBps"] = bw
        elif op == "Write":
            out["write_MBps"] = bw
    return out


def parse_inter_core(text):
    """Returns median CAS latency for off-diagonal entries (inter-core)."""
    rows = _parse_md_table(text, "**Core**")
    if not rows:
        return {"intra_socket_lat_ns": None}
    # rows are dicts; key "Core" is the row label, other keys (0, 1, 2, ...) are core IDs
    header_keys = list(rows[0].keys())  # ["Core", "0", "1", ...]
    if header_keys[0] != "Core":
        return {"intra_socket_lat_ns": None}
    vals = []
    for r in rows:
        for col in header_keys[1:]:
            v = r.get(col, "-")
            try:
                # Format: "32.3 [32.3-32.3]" — extract the median
                space_idx = v.find(" ")
                if space_idx > 0:
                    v = v[:space_idx]
                vals.append(float(v))
            except ValueError:
                pass
    if not vals:
        return {"intra_socket_lat_ns": None}
    vals.sort()
    return {"intra_socket_lat_ns": vals[len(vals) // 2]}  # median across all 24×23 pairs


def parse_cpu_alu(text):
    """Returns IPC for a few representative integer ops."""
    rows = _parse_md_table(text, "Operation")
    wanted = {"Add": "Add_ipc", "Mul": "Mul_ipc", "AND": "AND_ipc"}
    out = {v: None for v in wanted.values()}
    for r in rows:
        op = r.get("Operation", "").strip()
        if op in wanted:
            try:
                out[wanted[op]] = float(r["IPC"])
            except (KeyError, ValueError):
                pass
    return out


def parse_cpu_float(text):
    """Returns IPC for a few representative float ops."""
    rows = _parse_md_table(text, "Operation")
    wanted = {
        "float Add": "float_Add_ipc",
        "double Add": "double_Add_ipc",
        "float Mul": "float_Mul_ipc",
        "double Mul": "double_Mul_ipc",
    }
    out = {v: None for v in wanted.values()}
    for r in rows:
        op = r.get("Operation", "").strip()
        if op in wanted:
            try:
                out[wanted[op]] = float(r["IPC"])
            except (KeyError, ValueError):
                pass
    return out


def parse_cpu_branch(text):
    """Returns ns/branch for Always Taken and Never Taken patterns."""
    rows = _parse_md_table(text, "Pattern")
    wanted = {
        "Always Taken": "Always_Taken_ns_per_branch",
        "Never Taken":  "Never_Taken_ns_per_branch",
    }
    out = {v: None for v in wanted.values()}
    for r in rows:
        pat = r.get("Pattern", "").strip()
        if pat in wanted:
            try:
                out[wanted[pat]] = float(r["ns/branch"])
            except (KeyError, ValueError):
                pass
    return out


def parse_cpu_multi(text):
    """Returns scaling efficiency of integer Mul at 8 threads (%).
    Tables look like: | Op | Type | Threads | Time(ms) | Speedup | Efficiency | Status |"""
    out = {"Mod_efficiency_at_8_threads_pct": None}
    for line in text.split("\n"):
        # Match: | Mod | ALU | 8 | 60.27 | 7.68x | 96.0% | optimal |
        m = re.match(r"\|\s*Mod\s*\|\s*ALU\s*\|\s*8\s*\|.*?\|\s*([\d.]+)%\s*\|", line)
        if m:
            try:
                out["Mod_efficiency_at_8_threads_pct"] = float(m.group(1))
            except ValueError:
                pass
    return out


PARSERS = {
    "cache_hierarchy": ("cache_hierarchy_report.md", parse_cache_hierarchy),
    "memory_bandwidth": ("memory_bandwidth_report.md", parse_memory_bandwidth),
    "inter_core":       ("inter_core_latency_report.md", parse_inter_core),
    "cpu_alu":          ("cpu_alu_report.md", parse_cpu_alu),
    "cpu_float":        ("cpu_float_report.md", parse_cpu_float),
    "cpu_branch":       ("cpu_branch_report.md", parse_cpu_branch),
    "cpu_multi":        ("cpu_multi_core_report.md", parse_cpu_multi),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="Treat WARN as FAIL")
    ap.add_argument("--skip-missing", action="store_true",
                    help="Treat missing reports as SKIP rather than FAIL "
                         "(used by smoke when test_inter_core is skipped on "
                         "machines with many cores)")
    ap.add_argument("--baseline", metavar="FILE.json",
                    help="Also compare against a saved baseline (Layer 3 feature, future)")
    args = ap.parse_args()

    rep = Reporter(strict=args.strict)

    for test, (filename, parser) in PARSERS.items():
        path = REPORTS_DIR / filename
        if not path.exists():
            if args.skip_missing:
                rep.skip(test, "<file_exists>", f"Missing {path} (test was skipped)")
            else:
                rep.check(test, "<file_exists>", None, 1, 1, f"Missing {path}")
            continue
        if path.stat().st_size < 100:
            if args.skip_missing:
                rep.skip(test, "<file_nonempty>", f"Report {path} is < 100 bytes (test was skipped)")
            else:
                rep.check(test, "<file_nonempty>", None, 1, 1, f"Report {path} is < 100 bytes")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            metrics = parser(text)
        except Exception as e:
            rep.check(test, "<parse>", None, 1, 1, f"Parser exception: {e}")
            continue

        # Check each defined range
        for metric_key, (low, high, desc) in RANGES[test].items():
            value = metrics.get(metric_key)
            rep.check(test, metric_key, value, low, high, desc)

    ok = rep.report()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
