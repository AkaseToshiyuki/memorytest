#!/usr/bin/env python3
from __future__ import annotations
"""Memorytest scoring system.

Rates each tracked metric on a 0-100 scale based on how the measured value
compares to (a) a known physical ideal, and (b) the local machine's typical
performance. The score for each metric is then aggregated into per-category
and overall grades A-F.

Scoring philosophy (deliberately simple, deliberately explainable):

  - Latency metrics (lower = better): score = 100 * (best_known / max(measured, best_known))
    where best_known is a physically-reasonable minimum.
  - Bandwidth / throughput (higher = better): score = 100 * (min(measured, best_known) / best_known)
  - IPC (higher = better for ALU): score = 100 * (measured / 1.0) clamped to [0, 100]
  - Branch latency ns (lower = better): score = 100 * (1.0 / max(measured, 0.5)) capped at 100

Reference values come from the system this was first calibrated on
(Kunpeng 920, ARM64, 3 GHz, 2-channel DDR4). They are not "world records"
but represent what well-tuned server hardware typically delivers. On
faster hardware, metrics exceeding the reference simply score 100.

Usage:
    from report_score import score_run, score_to_grade, format_score_summary
    summary = score_run()  # reads reports/*.md + history.jsonl
    print(format_score_summary(summary))
"""

import json
import statistics
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).parent.resolve()
REPORTS = ROOT / "reports"
HISTORY = ROOT / "history.jsonl"

# Per-metric scoring: key, direction ("lower" or "higher"), reference value, weight.
# Weight defaults to 1.0; raise it to give a metric more influence on the overall score.
@dataclass
class MetricSpec:
    key: str
    direction: str           # "lower" or "higher"
    reference: float        # the "good" baseline (lower-bound for latency, target for bw)
    weight: float = 1.0
    description: str = ""
    category: str = ""

# Reference values are calibrated against the dev host (Kunpeng 920, 3 GHz,
# 2-channel DDR4-2666). These are not theoretical peaks; they are "what you
# should typically see on a well-tuned machine" — useful for grading.
# If your machine is faster, you score 100; if slower, the score drops linearly.
METRICS = [
    # Latency (lower = better)
    MetricSpec("l1d_latency_ns", "lower", 3.0, 1.0, "L1D read latency",         "cache"),
    MetricSpec("l2_latency_ns",  "lower", 8.0, 1.0, "L2 read latency",          "cache"),
    MetricSpec("l3_latency_ns",  "lower", 25.0, 1.0, "L3 read latency",         "cache"),
    MetricSpec("ram_latency_ns", "lower", 80.0, 1.0, "RAM read latency",        "cache"),
    # Bandwidth (higher = better, units: MB/s)
    MetricSpec("read_mbps",   "higher", 20000.0, 1.0, "Memory read bandwidth",   "bandwidth"),
    MetricSpec("write_mbps",  "higher", 10000.0, 1.0, "Memory write bandwidth",  "bandwidth"),
    MetricSpec("copy_mbps",   "higher", 15000.0, 1.0, "Memory copy bandwidth",   "bandwidth"),
    # Inter-core latency (lower = better, intra-socket median of one-hop pairs)
    MetricSpec("cas_median_ns", "lower", 20.0, 1.0, "Median intra-socket CAS", "inter_core"),
    # ALU IPC (higher = better, but chained volatile sinks depress it; ~0.1-0.2 is realistic
    # because each iteration has a RAW dependency on the previous sum. We use 0.2 as the
    # "good" baseline for ALU with anti-opt volatile sinks — the theoretical peak (no dep
    # chain) is 1.0 but is unmeasurable without the sink.)
    MetricSpec("alu_add_ipc", "higher", 0.2, 0.7, "Integer add IPC",          "alu"),
    MetricSpec("alu_mul_ipc", "higher", 0.2, 0.7, "Integer mul IPC",          "alu"),
    MetricSpec("alu_div_ipc", "higher", 0.1, 0.5, "Integer div IPC",          "alu"),
    MetricSpec("alu_and_ipc", "higher", 0.2, 0.3, "Integer AND IPC",          "alu"),
    MetricSpec("alu_or_ipc",  "higher", 0.2, 0.3, "Integer OR IPC",           "alu"),
    MetricSpec("alu_xor_ipc", "higher", 0.2, 0.3, "Integer XOR IPC",          "alu"),
    # SIMD ns/op (lower = better; FMA/FAdd ~0.5 ns is good for 128-bit @ 3 GHz)
    MetricSpec("simd_fadd_ns", "lower", 0.6, 1.0, "SIMD float add latency",   "simd"),
    MetricSpec("simd_fmul_ns", "lower", 0.6, 1.0, "SIMD float mul latency",   "simd"),
    MetricSpec("simd_fma_ns",  "lower", 0.6, 1.0, "SIMD FMA latency",         "simd"),
    MetricSpec("simd_dot_ns",  "lower", 0.6, 1.0, "SIMD dot-product latency", "simd"),
]


@dataclass
class MetricScore:
    key: str
    value: float | None
    score: float | None     # 0-100
    weight: float
    description: str
    category: str
    note: str = ""          # e.g. "no-baseline" or "missing"


@dataclass
class CategoryScore:
    name: str
    score: float            # weighted avg 0-100
    letter: str             # A-F
    n_metrics: int
    n_pass: int
    n_warn: int
    n_fail: int


@dataclass
class ScoreSummary:
    overall: float
    letter: str
    categories: list[CategoryScore]
    metrics: list[MetricScore]
    n_metrics_total: int
    n_metrics_scored: int
    warnings: list[str] = field(default_factory=list)


def score_metric(value: float, spec: MetricSpec) -> float:
    """Score a single value 0-100 against a metric spec."""
    if value <= 0 or value != value:  # NaN check
        return 0.0
    if spec.direction == "lower":
        # As measured → reference, score = 100. As measured → 2*reference, score = 50.
        # As measured → 4*reference, score = 25. Etc.
        ratio = spec.reference / value
        return min(100.0, 100.0 * ratio)
    else:  # higher
        ratio = value / spec.reference
        return min(100.0, 100.0 * ratio)


def score_to_letter(score: float) -> str:
    """Convert 0-100 score to letter grade (A-F)."""
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 70: return "C"
    if score >= 60: return "D"
    return "F"


# ============================================================================
# Report parsing — small, dependency-free, similar to test_regression.py but
# focused on grabbing values for scoring (a single value per metric, not
# a history-aware comparison).
# ============================================================================

def _find_data_table(text: str, skip_first: bool = True) -> tuple[list[str], list[list[str]]] | None:
    """Find the first markdown data table (skipping the System Info Item/Value table)."""
    import re
    lines = text.splitlines()
    i = 0
    found = 0
    # Match markdown tables in either canonical form (`| h | h |`) or
    # the ASCII-aligned flavour our binaries emit (`H | H |` with a
    # `--- | ---` delimiter). Canonical: header line starts with |.
    # Binary flavour: header line is plain text but contains at least
    # one `|`, and the next line is a delimiter of `-` `|` `:` `|`.
    delim_re = re.compile(r"^[\s\-:|]+\|?$")
    while i < len(lines) - 1:
        line_i = lines[i]
        line_ip1 = lines[i + 1]
        if "|" in line_i and delim_re.match(line_ip1):
            # Split on |, drop first/last empty fragments from leading/trailing |
            cells = [c.strip().rstrip("*").strip() for c in line_i.split("|")]
            if cells and not cells[0]:
                cells = cells[1:]
            if cells and not cells[-1]:
                cells = cells[:-1]
            headers = cells
            rows = []
            j = i + 2
            # Row lines: canonical starts with |, binary flavour is plain
            # text containing |. Use the delimiter regex to confirm a line
            # is still part of the table (data lines always contain | and
            # are not the delimiter itself).
            while j < len(lines):
                row_line = lines[j]
                if not row_line.strip() or "|" not in row_line:
                    break
                if delim_re.match(row_line):
                    break  # hit the next table's delimiter
                cells = [c.strip().rstrip("*").strip() for c in row_line.split("|")]
                if cells and not cells[0]:
                    cells = cells[1:]
                if cells and not cells[-1]:
                    cells = cells[:-1]
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
    norm = [h.lower() for h in headers]
    for cand in candidates:
        cand_l = cand.lower()
        for i, h in enumerate(norm):
            if cand_l in h:
                return i
    return None


def _find_data_table_after(text: str, header_keyword: str) -> tuple[list[str], list[list[str]]] | None:
    """Find the first markdown table whose header row contains `header_keyword`,
    OR the first markdown table that appears after a heading matching
    `## <header_keyword>` (or `### <header_keyword>`). The heading is
    treated as a section anchor; the table is the one immediately after
    it.

    Implementation: two passes.
      Pass 1 — direct: walk lines; if a line is a table header (has `|`,
                contains header_keyword, and is followed by a delim line),
                parse and return that table.
      Pass 2 — anchored: walk lines; record the index of the first line
                that exactly matches `## <header_keyword>` (or `### ...`);
                then return the first table that appears after that index.
    """
    import re
    lines = text.splitlines()
    delim_re = re.compile(r"^[\s\-:|]+\|?$")

    def _parse_table_at(i: int):
        """Parse a markdown table starting at line i (header row). Returns
        (headers, rows) or None if i doesn't look like a table header or
        the table is empty."""
        if i + 1 >= len(lines) or not delim_re.match(lines[i + 1]):
            return None
        cells = [c.strip().rstrip("*").strip() for c in lines[i].split("|")]
        if cells and not cells[0]:
            cells = cells[1:]
        if cells and not cells[-1]:
            cells = cells[:-1]
        headers = cells
        rows = []
        j = i + 2
        while j < len(lines):
            rline = lines[j]
            if not rline.strip() or "|" not in rline:
                break
            if delim_re.match(rline):
                break
            rcells = [c.strip() for c in rline.split("|")]
            if rcells and not rcells[0]:
                rcells = rcells[1:]
            if rcells and not rcells[-1]:
                rcells = rcells[:-1]
            rows.append(rcells)
            j += 1
        if not rows:
            return None
        return headers, rows

    kw_lower = header_keyword.lower()

    # Pass 1: direct match — table header line itself contains the keyword.
    for i, line in enumerate(lines):
        if "|" in line and kw_lower in line.lower():
            tbl = _parse_table_at(i)
            if tbl is not None:
                return tbl

    # Pass 2: anchored match — `## <header_keyword>` heading, then next table.
    anchor_idx = -1
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            continue
        # match `#+` then space then header_keyword then EOL
        if not re.match(r"^#+\s", stripped):
            continue
        # Compare body (after the leading #s + whitespace) case-insensitively.
        body = re.sub(r"^#+\s*", "", stripped).strip().rstrip(":").strip()
        if body.lower() == kw_lower:
            anchor_idx = i
            break
    if anchor_idx >= 0:
        for i in range(anchor_idx + 1, len(lines)):
            if "|" in lines[i]:
                tbl = _parse_table_at(i)
                if tbl is not None:
                    return tbl

    return None


def _safe_float(s: str) -> float | None:
    s = s.replace(",", "").strip()
    try:
        v = float(s)
        return v if v == v else None  # NaN check
    except (ValueError, TypeError):
        return None


def _row_label(row: list[str]) -> str:
    return row[0].strip().rstrip("*").strip() if row else ""


def parse_cache_hierarchy(text: str) -> dict:
    out = {}
    # The cache_hierarchy_report.md contains several tables. We want the one
    # under the "Cache Hierarchy Scan" heading — that one has columns
    # | Size | RdLat(ns) | WrLat(ns) | BW(MB/s) | Expected | Analysis |.
    # The first table (System Configuration) and the second ("Cache Boundary
    # Detection" transitions) are not what we want. Use the anchor-based
    # finder so we always land on the right table.
    tbl = _find_data_table_after(text, "Cache Hierarchy Scan")
    if not tbl: return out
    headers, rows = tbl
    # Header is "RdLat(ns)"; match case-insensitively.
    rd_col = _col_index(headers, "rdlat", "rdlat(ns)", "read latency", "latency")
    if rd_col is None: rd_col = 1
    # Match by size in KB: pick the row whose size is in [lo, hi] KB. This
    # is more robust than string-matching "16KB" / "32KB" because the size
    # column often reads "32.0KB" or "16.5KB" with a decimal.
    # L1D ~ 32KB, L2 ~ 512KB, L3 ~ 32MB = 32768 KB, RAM >= 64MB = 65536 KB.
    targets = [
        ("l1d_latency_ns",  16,    64),     # 16-64 KB
        ("l2_latency_ns",   128,   1024),   # 128KB - 1MB
        ("l3_latency_ns",   4096,  65536),  # 4MB - 64MB
        ("ram_latency_ns",  65537, 1048576),# 64MB - 1GB
    ]
    def _size_to_kb(label: str) -> float | None:
        """Parse '32.0KB', '1.5MB', '2.0GB' into KB. Returns None on failure."""
        import re
        m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([KMGT]?B)\s*$", label, re.I)
        if not m:
            return None
        v = float(m.group(1))
        unit = m.group(2).upper()
        if unit == "KB": return v
        if unit == "MB": return v * 1024
        if unit == "GB": return v * 1024 * 1024
        if unit == "TB": return v * 1024 * 1024 * 1024
        if unit == "B":  return v / 1024
        return None
    for row in rows:
        lbl = _row_label(row)
        size_kb = _size_to_kb(lbl)
        if size_kb is None:
            continue
        v = _safe_float(row[rd_col] if rd_col < len(row) else None)
        if v is None or not (0.1 < v < 1000):
            continue
        for key, lo, hi in targets:
            if key in out:
                continue
            if lo <= size_kb <= hi:
                out[key] = v
                break
    return out


def parse_memory_bandwidth(text: str) -> dict:
    """Parse the memory bandwidth report.

    The bandwidth binary now writes a markdown table of the form::

        | Operation | Bandwidth (MB/s) |
        | Read      | 36382.30          |
        | Write     | 17250.72          |
        | Copy      | 23904.88          |

    Falls back to a textual `Read: 1234.56 MB/s` regex for older reports.
    """
    out = {}
    # 1) Markdown table (current format)
    tbl = _find_data_table_after(text, "Bandwidth (MB/s)")
    if tbl:
        headers, rows = tbl
        op_col = _col_index(headers, "operation")
        bw_col = _col_index(headers, "bandwidth (mb/s)", "bandwidth")
        if op_col is not None and bw_col is not None:
            for row in rows:
                op = _row_label(row).lower()
                v = _safe_float(row[bw_col] if bw_col < len(row) else None)
                if v is not None and v > 100:
                    if op.startswith("read"):  out["read_mbps"]  = v
                    elif op.startswith("write"): out["write_mbps"] = v
                    elif op.startswith("copy"):  out["copy_mbps"]  = v
            if out: return out
    # 2) Textual fallback (older format)
    import re
    patterns = {
        "read_mbps":  r"Read:\s+([0-9]+\.[0-9]+)\s*MB/s",
        "write_mbps": r"Write:\s+([0-9]+\.[0-9]+)\s*MB/s",
        "copy_mbps":  r"Copy:\s+([0-9]+\.[0-9]+)\s*MB/s",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            v = float(m.group(1))
            if v > 100:
                out[key] = v
    return out


def parse_inter_core(text: str) -> dict:
    """Parse the NxN inter-core latency matrix.

    The inter-core binary writes a 24x24 markdown table where row/col
    index = core id. Self-sentinels are `-` (a literal dash), and the
    1-hop latency for each row is the non-zero minimum of the cells
    adjacent to that row's self-sentinel. We compute the average and
    minimum across all 24 row minima and return `cas_avg_ns` / `cas_min_ns`.
    """
    tbl = _find_data_table_after(text, "**Core**")
    if not tbl: return {}
    headers, rows = tbl
    # The first header cell is the row-label sentinel (e.g. "**Core**")
    # and the rest are column indices 0..N-1.
    try:
        n_cols = len(headers) - 1
    except Exception:
        return {}
    if n_cols < 2: return {}
    one_hop = []
    for row in rows:
        # First cell is the row index (core id)
        try:
            src = int(row[0])
        except (ValueError, IndexError):
            continue
        if len(row) < n_cols + 1: continue
        # The cell at position src+1 corresponds to column src (self)
        sentinel_col = src + 1
        candidates = []
        for k in (sentinel_col - 1, sentinel_col + 1):
            if 1 <= k <= n_cols:
                try:
                    cell = row[k]
                    # Format: "32.3 [32.3-32.3]" — extract median
                    space_idx = cell.find(" ")
                    if space_idx > 0:
                        cell = cell[:space_idx]
                    v = float(cell)
                    if 1 < v < 5000:
                        candidates.append(v)
                except (ValueError, IndexError):
                    pass
        if candidates:
            one_hop.append(min(candidates))
    if one_hop:
        return {
            "cas_median_ns": sum(one_hop) / len(one_hop),
        }
    return {}



def parse_cpu_alu(text: str) -> dict:
    out = {}
    tbl = _find_data_table(text, skip_first=True)
    if not tbl: return out
    headers, rows = tbl
    ipc_col = _col_index(headers, "ipc")
    if ipc_col is None: ipc_col = len(headers) - 2
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
    out = {}
    tbl = _find_data_table(text, skip_first=True)
    if not tbl: return out
    headers, rows = tbl
    ns_col = _col_index(headers, "ns/op", "ns_per_op", "nsec/op")
    if ns_col is None: ns_col = 3
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


def collect_metrics() -> dict:
    """Parse all reports/*.md and return {metric_key: value}."""
    metrics = {}
    for category, parser in PARSERS.items():
        candidates = list(REPORTS.glob(f"{category}_report.md"))
        if not candidates:
            candidates = list(REPORTS.glob(f"{category}*report*.md"))
        if not candidates:
            continue
        text = candidates[0].read_text()
        try:
            metrics.update(parser(text))
        except Exception as e:
            print(f"# WARN: parser {category} failed: {e}", file=__import__("sys").stderr)
    return metrics


def score_run() -> ScoreSummary:
    """Score the current run: read reports, score each metric, aggregate by category."""
    values = collect_metrics()
    metric_scores: list[MetricScore] = []
    for spec in METRICS:
        v = values.get(spec.key)
        if v is None:
            metric_scores.append(MetricScore(
                key=spec.key, value=None, score=None,
                weight=spec.weight, description=spec.description,
                category=spec.category, note="missing",
            ))
            continue
        s = score_metric(v, spec)
        metric_scores.append(MetricScore(
            key=spec.key, value=v, score=s,
            weight=spec.weight, description=spec.description,
            category=spec.category,
        ))

    # Group by category
    by_cat: dict[str, list[MetricScore]] = {}
    for ms in metric_scores:
        by_cat.setdefault(ms.category, []).append(ms)

    cat_scores: list[CategoryScore] = []
    for cat, items in by_cat.items():
        weighted_sum = 0.0
        weight_sum = 0.0
        n_pass = n_warn = n_fail = 0
        for it in items:
            if it.score is None:
                continue
            weighted_sum += it.score * it.weight
            weight_sum += it.weight
            if it.score >= 80: n_pass += 1
            elif it.score >= 60: n_warn += 1
            else: n_fail += 1
        if weight_sum == 0:
            cat_score = 0.0
        else:
            cat_score = weighted_sum / weight_sum
        cat_scores.append(CategoryScore(
            name=cat, score=cat_score, letter=score_to_letter(cat_score),
            n_metrics=len(items), n_pass=n_pass, n_warn=n_warn, n_fail=n_fail,
        ))

    # Overall = weighted average of category scores (equal weight per category)
    if cat_scores:
        overall = statistics.mean(c.score for c in cat_scores)
    else:
        overall = 0.0

    n_scored = sum(1 for m in metric_scores if m.score is not None)
    n_total = len(metric_scores)
    warnings = []
    if n_scored < n_total:
        missing = [m.key for m in metric_scores if m.score is None]
        warnings.append(f"{n_total - n_scored} metrics missing: {', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}")

    return ScoreSummary(
        overall=overall, letter=score_to_letter(overall),
        categories=cat_scores, metrics=metric_scores,
        n_metrics_total=n_total, n_metrics_scored=n_scored,
        warnings=warnings,
    )


def format_score_summary(summary: ScoreSummary) -> str:
    """Format summary for terminal / log / inline inclusion in HTML/PDF."""
    lines = []
    lines.append(f"=== Score Summary ===")
    lines.append(f"Overall: {summary.overall:.1f}/100  (Grade: {summary.letter})")
    lines.append(f"Metrics: {summary.n_metrics_scored}/{summary.n_metrics_total} scored")
    if summary.warnings:
        for w in summary.warnings:
            lines.append(f"  WARN: {w}")
    lines.append("")
    lines.append("By category:")
    for c in summary.categories:
        lines.append(f"  {c.name:<12} {c.score:>5.1f}/100  ({c.letter})  "
                     f"[{c.n_pass} pass / {c.n_warn} warn / {c.n_fail} fail of {c.n_metrics}]")
    lines.append("")
    lines.append("Per-metric:")
    for m in summary.metrics:
        if m.score is None:
            lines.append(f"  {m.key:<22} (no data)")
        else:
            unit = "ns" if "latency" in m.key or "ns" in m.key else ("MB/s" if "mbps" in m.key else "")
            if m.value is not None:
                lines.append(f"  {m.key:<22} {m.value:>10.3f} {unit:<6}  →  {m.score:>5.1f}/100")
    return "\n".join(lines)


def score_to_dict(summary: ScoreSummary) -> dict:
    """JSON-serializable dict (for embedding in HTML/PDF)."""
    return {
        "overall": round(summary.overall, 2),
        "letter": summary.letter,
        "n_metrics_total": summary.n_metrics_total,
        "n_metrics_scored": summary.n_metrics_scored,
        "warnings": summary.warnings,
        "categories": [
            {"name": c.name, "score": round(c.score, 2), "letter": c.letter,
             "n_metrics": c.n_metrics, "n_pass": c.n_pass, "n_warn": c.n_warn, "n_fail": c.n_fail}
            for c in summary.categories
        ],
        "metrics": [
            {"key": m.key, "value": m.value, "score": round(m.score, 2) if m.score is not None else None,
             "weight": m.weight, "description": m.description, "category": m.category, "note": m.note}
            for m in summary.metrics
        ],
    }


if __name__ == "__main__":
    # Ignore SIGPIPE so `python3 report_score.py | head` doesn't raise
    # BrokenPipeError at process exit (cosmetic; doesn't affect the HTML
    # pipeline which doesn't pipe through head).
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError):
        pass
    s = score_run()
    print(format_score_summary(s))
    print()
    print("JSON:")
    print(json.dumps(score_to_dict(s), indent=2))
