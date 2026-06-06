#!/usr/bin/env python3
from __future__ import annotations
"""Memorytest HTML report generator (zero dependencies, pure stdlib).

Produces a self-contained HTML file with:
  - System info section
  - Overall score with letter grade
  - Per-category scores with progress bars
  - All benchmark tables (parsed from reports/*.md)
  - Inline SVG charts:
    - Cache hierarchy latency vs size
    - Memory bandwidth by operation
    - ALU IPC by operation
    - SIMD ns/op by operation
  - Inline PNG charts (from reports/charts/*.png) if they exist
  - Score summary from report_score

Usage:
    python3 report_html.py                  # writes reports/benchmark_report.html
    python3 report_html.py --output path    # custom HTML output path

The output HTML has no external dependencies — all CSS, SVG, and base64
data URIs are inlined. It can be opened directly in a browser, archived,
or sent by email. Print-to-PDF is a one-click browser action; we don't
maintain a separate PDF rendering pipeline.
"""

import argparse
import base64
import datetime
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
REPORTS = ROOT / "reports"
CHARTS = REPORTS / "charts"
DEFAULT_OUTPUT = REPORTS / "benchmark_report.html"

try:
    from report_score import score_run, score_to_dict, format_score_summary, collect_display_data
    HAS_SCORE = True
except ImportError as e:
    HAS_SCORE = False
    _SCORE_ERR = e


# ============================================================================
# Markdown table parser (same as test_regression.py but standalone)
# ============================================================================

def _find_data_table(text: str, skip_first: bool = False):
    """Find the first markdown/ASCII data table.

    The binaries emit two flavours:
      1. Canonical markdown: `| H | H |` headers followed by `| --- | --- |`
      2. ASCII-aligned: `H | H |` headers followed by `--- | --- |`

    Both are recognised. `skip_first` is preserved for callers that
    explicitly want to bypass the first table. Sub-section headers like
    `-- ALU --` and blank lines are skipped within the body.
    """
    import re
    delim_re = re.compile(r"^[\s\-:|]+\|?$")
    section_re = re.compile(r"^--\s+.+\s+--$")
    lines = text.splitlines()
    i = 0
    found = 0
    while i < len(lines) - 1:
        line_i = lines[i]
        line_ip1 = lines[i + 1]
        if "|" in line_i and delim_re.match(line_ip1):
            cells = [c.strip().rstrip("*").strip() for c in line_i.split("|")]
            if cells and not cells[0]:
                cells = cells[1:]
            if cells and not cells[-1]:
                cells = cells[:-1]
            headers = cells
            rows = []
            j = i + 2
            # Row lines: skip blank sub-section separators AND section
            # headers, then stop at the first non-pipe, non-delimiter line.
            while j < len(lines):
                row_line = lines[j]
                if not row_line.strip():
                    j += 1
                    continue
                if section_re.match(row_line):
                    j += 1
                    continue
                if "|" not in row_line:
                    break
                if delim_re.match(row_line):
                    break
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


def _find_data_table_after(text: str, header_keyword: str):
    """Find the first data table whose header line contains header_keyword.

    Identical to `_find_data_table` but does not match the very first
    table in the file; it requires the keyword to appear in the header
    line itself. Returns (headers, rows) or None.
    """
    import re
    delim_re = re.compile(r"^[\s\-:|]+\|?$")
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        if header_keyword.lower() not in lines[i].lower():
            continue
        if "|" not in lines[i] or not delim_re.match(lines[i + 1]):
            continue
        return _parse_table_at(text, i)
    return None


def _parse_table_at(text: str, header_idx: int):
    """Parse the markdown/ASCII table whose header is at line `header_idx`."""
    import re
    delim_re = re.compile(r"^[\s\-:|]+\|?$")
    section_re = re.compile(r"^--\s+.+\s+--$")
    lines = text.splitlines()
    if header_idx >= len(lines) - 1:
        return None
    line_i = lines[header_idx]
    if "|" not in line_i or not delim_re.match(lines[header_idx + 1]):
        return None
    cells = [c.strip().rstrip("*").strip() for c in line_i.split("|")]
    if cells and not cells[0]: cells = cells[1:]
    if cells and not cells[-1]: cells = cells[:-1]
    headers = cells
    rows = []
    j = header_idx + 2
    while j < len(lines):
        rl = lines[j]
        if not rl.strip():
            j += 1; continue
        if section_re.match(rl):
            j += 1; continue
        if "|" not in rl: break
        if delim_re.match(rl): break
        cells = [c.strip().rstrip("*").strip() for c in rl.split("|")]
        if cells and not cells[0]: cells = cells[1:]
        if cells and not cells[-1]: cells = cells[:-1]
        if len(cells) == len(headers):
            rows.append(cells)
        j += 1
    return headers, rows


def _parse_all_tables(text: str) -> list:
    """Parse ALL data tables (not just the first) — used for cache_hierarchy which has multiple.

    Recognises both canonical markdown (`| H | H |`) and ASCII-aligned
    (`H | H |`) header lines, skips empty sub-section separator lines
    AND sub-section headers like `-- ALU --`.
    """
    import re
    out = []
    delim_re = re.compile(r"^[\s\-:|]+\|?$")
    section_re = re.compile(r"^--\s+.+\s+--$")
    lines = text.splitlines()
    i = 0
    while i < len(lines) - 1:
        line_i = lines[i]
        line_ip1 = lines[i + 1]
        if "|" in line_i and delim_re.match(line_ip1):
            cells = [c.strip().rstrip("*").strip() for c in line_i.split("|")]
            if cells and not cells[0]:
                cells = cells[1:]
            if cells and not cells[-1]:
                cells = cells[:-1]
            headers = cells
            rows = []
            j = i + 2
            while j < len(lines):
                row_line = lines[j]
                if not row_line.strip():
                    j += 1
                    continue
                if section_re.match(row_line):
                    j += 1
                    continue
                if "|" not in row_line:
                    break
                if delim_re.match(row_line):
                    break
                cells = [c.strip().rstrip("*").strip() for c in row_line.split("|")]
                if cells and not cells[0]:
                    cells = cells[1:]
                if cells and not cells[-1]:
                    cells = cells[:-1]
                if len(cells) == len(headers):
                    rows.append(cells)
                j += 1
            if rows:
                out.append((headers, rows))
            i = j
        else:
            i += 1
    return out


# ============================================================================
# HTML rendering
# ============================================================================

def _svg_bar(value: float, max_val: float, width: int = 200, height: int = 20,
             color: str = "#4a90e2", label: str = "") -> str:
    """Render a horizontal progress bar in SVG."""
    pct = max(0.0, min(1.0, value / max_val if max_val > 0 else 0))
    filled = int(pct * width)
    label_html = f'<text x="0" y="14" font-size="11" fill="#333">{html.escape(label)}</text>' if label else ""
    return f'''<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" style="vertical-align:middle">
  <rect x="0" y="2" width="{width}" height="14" fill="#eee" stroke="#ccc" stroke-width="0.5"/>
  <rect x="0" y="2" width="{filled}" height="14" fill="{color}"/>
  {label_html}
</svg>'''


def _svg_score_gauge(score: float, letter: str) -> str:
    """Render a large circular gauge for the overall score."""
    color = {"A": "#22c55e", "B": "#84cc16", "C": "#eab308", "D": "#f97316", "F": "#ef4444"}.get(letter, "#888888")
    # SVG arc from 0 to score (out of 100), starting at 9 o'clock going clockwise
    angle = (score / 100.0) * 360.0
    rad = (angle - 90) * 3.14159 / 180.0
    import math
    cx, cy, r = 100, 100, 80
    x = cx + r * math.cos(rad)
    y = cy + r * math.sin(rad)
    large = 1 if angle > 180 else 0
    arc = f'<path d="M {cx} {cy - r} A {r} {r} 0 {large} 1 {x:.1f} {y:.1f}" stroke="{color}" stroke-width="20" fill="none"/>'
    return f'''<svg width="200" height="200" xmlns="http://www.w3.org/2000/svg" style="display:block;margin:0 auto">
  <circle cx="100" cy="100" r="80" stroke="#eee" stroke-width="20" fill="none"/>
  {arc}
  <text x="100" y="95" text-anchor="middle" font-size="48" font-weight="bold" fill="#333">{score:.1f}</text>
  <text x="100" y="120" text-anchor="middle" font-size="20" fill="#666">/ 100</text>
  <text x="100" y="155" text-anchor="middle" font-size="36" font-weight="bold" fill="{color}">{letter}</text>
</svg>'''


def _svg_line_chart(points: list, width: int = 600, height: int = 200,
                    xlabel: str = "", ylabel: str = "", title: str = "",
                    log_y: bool = False) -> str:
    """Inline SVG line chart.

    points: list of (x_label, y_value) pairs.
    """
    if not points:
        return "<p>(no data)</p>"
    ys = [p[1] for p in points if p[1] is not None and p[1] > 0]
    if not ys:
        return "<p>(no valid data points)</p>"
    if log_y:
        ys_plot = [max(0.001, p[1] if p[1] is not None and p[1] > 0 else 0.001) for p in points]
        ymin, ymax = min(ys_plot), max(ys_plot)
        def yscale(v): return (math.log(v) - math.log(ymin)) / max(math.log(ymax) - math.log(ymin), 1e-9)
    else:
        ymin, ymax = min(ys), max(ys)
        def yscale(v): return (v - ymin) / max(ymax - ymin, 1e-9)
    import math
    n = len(points)
    pad_l, pad_r, pad_t, pad_b = 60, 20, 30, 50
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    def xscale(i): return pad_l + (i / max(n - 1, 1)) * plot_w
    path = []
    for i, (label, v) in enumerate(points):
        if v is None or v <= 0: continue
        x = xscale(i)
        y = pad_t + (1 - yscale(v)) * plot_h
        path.append(f"{'M' if i == 0 or points[i-1][1] is None else 'L'} {x:.1f} {y:.1f}")
    path_d = " ".join(path)
    # Y-axis labels
    y_ticks = [ymin, (ymin + ymax) / 2, ymax]
    if log_y: y_ticks = [ymin, math.sqrt(ymin * ymax), ymax]
    y_labels = []
    for t in y_ticks:
        y = pad_t + (1 - yscale(t)) * plot_h
        y_labels.append(f'<text x="{pad_l - 5}" y="{y + 3:.1f}" text-anchor="end" font-size="10" fill="#666">{t:.2g}</text>')
    # X-axis labels (show every other if too many)
    step = max(1, n // 8)
    x_labels = []
    for i, (label, _) in enumerate(points):
        if i % step != 0 and i != n - 1: continue
        x = xscale(i)
        x_labels.append(f'<text x="{x:.1f}" y="{pad_t + plot_h + 15}" text-anchor="middle" font-size="10" fill="#666" transform="rotate(-30 {x:.1f} {pad_t + plot_h + 15})">{html.escape(label)}</text>')
    title_html = f'<text x="{width/2}" y="20" text-anchor="middle" font-size="13" font-weight="bold" fill="#333">{html.escape(title)}</text>' if title else ""
    return f'''<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" style="display:block;margin:10px 0">
  <rect x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" fill="#fafafa" stroke="#ddd"/>
  {title_html}
  <path d="{path_d}" stroke="#4a90e2" stroke-width="2" fill="none"/>
  {"".join(y_labels)}
  {"".join(x_labels)}
  <text x="{pad_l - 45}" y="{pad_t + plot_h/2}" text-anchor="middle" font-size="11" fill="#666" transform="rotate(-90 {pad_l - 45} {pad_t + plot_h/2})">{html.escape(ylabel)}</text>
</svg>'''


def _svg_bar_chart(points: list, width: int = 600, height: int = 200,
                    xlabel: str = "", ylabel: str = "", title: str = "",
                    colors: list = None, zero_marker: bool = False) -> str:
    """Inline SVG bar chart for small categorical data (e.g. Read/Write/Copy).

    `points` is a list of (label, value) tuples. Values <= 0 are skipped
    from the bar but the x-axis label is still drawn. If `zero_marker` is
    True, zero/very-small values render as a short grey stub so the
    reader can see "this bar exists but has no measurable height".
    """
    if not points:
        return "<p>(no data)</p>"
    # ymax from real (> 0) values; sub-1 values need extra precision
    ys = [p[1] for p in points if p[1] is not None and p[1] > 0]
    if not ys:
        return "<p>(no valid data points)</p>"
    ymax = max(ys) * 1.15
    # Choose decimal places: 2 for sub-10, 1 for sub-100, 0 for >=100
    if ymax < 10: yfmt = ".2f"
    elif ymax < 100: yfmt = ".1f"
    else: yfmt = ".0f"
    if colors is None: colors = ["steelblue", "coral", "green", "orange", "purple"]
    pad_l, pad_r, pad_t, pad_b = 60, 20, 30, 50
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(points)
    bar_w = plot_w / n * 0.65
    gap   = plot_w / n * 0.35
    bars = []
    x_labels = []
    for i, (label, v) in enumerate(points):
        cx = pad_l + (i + 0.5) * (plot_w / n)
        # Always emit the x-axis label, even for zero bars
        x_labels.append(f'<text x="{cx:.1f}" y="{pad_t + plot_h + 18}" text-anchor="middle" font-size="10" fill="#666">{html.escape(label)}</text>')
        if v is None or v <= 0:
            # Skip the bar entirely if no zero_marker; otherwise draw a stub
            if zero_marker:
                stub_h = 6   # 6px grey stub
                x = cx - bar_w / 2
                y = pad_t + plot_h - stub_h
                bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{stub_h}" fill="#bbb"/>')
                bars.append(f'<text x="{cx:.1f}" y="{y - 3:.1f}" text-anchor="middle" font-size="10" fill="#999">0</text>')
            continue
        x = cx - bar_w / 2
        h = (v / ymax) * plot_h
        y = pad_t + plot_h - h
        c = colors[i % len(colors)]
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="{c}"/>')
        # Data label: match y precision so 0.99 isn't displayed as "1"
        bars.append(f'<text x="{cx:.1f}" y="{y - 3:.1f}" text-anchor="middle" font-size="10" fill="#333">{v:{yfmt}}</text>')
    # Y-axis gridlines + labels (5 ticks: 0, 25%, 50%, 75%, 100% of ymax)
    y_ticks = [ymax * f for f in (0, 0.25, 0.5, 0.75, 1.0)]
    y_labels = []
    for t in y_ticks:
        y = pad_t + (1 - t / ymax) * plot_h
        y_labels.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" stroke="#eee" stroke-width="1"/>')
        y_labels.append(f'<text x="{pad_l - 5}" y="{y + 3:.1f}" text-anchor="end" font-size="10" fill="#666">{t:{yfmt}}</text>')
    title_html = f'<text x="{width/2}" y="20" text-anchor="middle" font-size="13" font-weight="bold" fill="#333">{html.escape(title)}</text>' if title else ""
    return f'''<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" style="display:block;margin:10px 0">
  {title_html}
  {"".join(y_labels)}
  {"".join(bars)}
  {"".join(x_labels)}
  <text x="{pad_l - 45}" y="{pad_t + plot_h/2}" text-anchor="middle" font-size="11" fill="#666" transform="rotate(-90 {pad_l - 45} {pad_t + plot_h/2})">{html.escape(ylabel)}</text>
</svg>'''


def _table_html(headers: list, rows: list) -> str:
    h = "".join(f"<th>{html.escape(c)}</th>" for c in headers)
    body = ""
    for row in rows:
        cells = "".join(f"<td>{html.escape(c)}</td>" for c in row)
        body += f"<tr>{cells}</tr>"
    return f'<table class="data"><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>'


def _png_data_uri(path: Path) -> str | None:
    """Load PNG and return as base64 data URI, or None if file missing."""
    if not path.exists(): return None
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/png;base64,{data}"


def _collect_cache_chart_data() -> list:
    """Extract size vs RdLat for SVG line chart from cache_hierarchy report."""
    p = REPORTS / "cache_hierarchy_report.md"
    if not p.exists(): return []
    text = p.read_text()
    tbl = _find_data_table_after(text, "RdLat(ns)")
    if not tbl: return []
    headers, rows = tbl
    rd_col = None
    for i, h in enumerate(headers):
        if "rdlat" in h.lower() or "latency" in h.lower():
            rd_col = i; break
    if rd_col is None: rd_col = 1
    out = []
    for row in rows:
        try: v = float(row[rd_col])
        except (ValueError, IndexError): continue
        if v > 0: out.append((row[0], v))
    return out


def _collect_cache_kpis() -> list:
    """Extract L1D / L2 / L3 / RAM latency as KPI cards.

    The "Cache Hierarchy Scan" table has columns `Size | RdLat(ns) | WrLat |
    BW | Expected | Analysis`. We pick the first row tagged with each level
    and return the RdLat as the latency.  Level labels come from the
    **measured latency boundary** (the `Inferred` column of the
    "Inferred vs Reported" table) — NOT from system-detected cache sizes.
    """
    p = REPORTS / "cache_hierarchy_report.md"
    if not p.exists(): return []
    text = p.read_text()
    tbl = _find_data_table_after(text, "RdLat(ns)")
    if not tbl: return []
    headers, rows = tbl
    rd_col, exp_col = None, None
    for i, h in enumerate(headers):
        hl = h.lower()
        if "rdlat" in hl: rd_col = i
        elif hl == "expected": exp_col = i
    if rd_col is None or exp_col is None: return []

    # Parse the "Inferred vs Reported" table to get inferred sizes.
    # Format: | Level | Inferred (from latency) | Reported (sysfs) | Match |
    inferred = {}
    ir_tbl = _find_data_table_after(text, "Inferred (from latency)")
    if not ir_tbl:
        ir_tbl = _find_data_table_after(text, "Inferred")
    if ir_tbl:
        _, ir_rows = ir_tbl
        for row in ir_rows:
            if len(row) < 2: continue
            lvl = row[0].strip().lower()
            inf = row[1].strip()
            # Normalise level names to match Expected column
            if lvl.startswith("l1"):   inferred["L1"] = inf
            elif lvl.startswith("l2"): inferred["L2"] = inf
            elif lvl.startswith("l3"): inferred["L3"] = inf
            elif lvl.startswith("ram"): inferred["RAM"] = inf

    seen = set()
    out = []
    for row in rows:
        try:
            level = row[exp_col].strip()
            v = float(row[rd_col])
        except (ValueError, IndexError):
            continue
        if level in seen: continue
        seen.add(level)
        # Build label: prefer inferred boundary, fall back to level name
        if level in inferred:
            label = f"{level.upper()} ≤ {inferred[level]}"
        else:
            label = level.upper()
        out.append((label, f"{v:.2f}", "ns"))
    return out


def _system_info_html() -> str:
    """Top-of-page System Information card.

    Pulls the System Configuration table from cache_hierarchy_report.md
    (the canonical first source — every test binary writes the same fields).
    """
    p = REPORTS / "cache_hierarchy_report.md"
    if not p.exists():
        return '<div class="card"><h2>System Information</h2><p class="no-data">No report available</p></div>'
    text = p.read_text()
    # Find the table that follows "## System Configuration"
    tbl = _find_data_table_after(text, "Item")
    if not tbl:
        return '<div class="card"><h2>System Information</h2><p class="no-data">No system info parsed</p></div>'
    headers, rows = tbl
    body = "".join(
        f"<tr><td>{html.escape(h)}</td><td>{html.escape(v)}</td></tr>"
        for h, v in rows
    )
    return (
        '<div class="card">'
        '<h2>System Information</h2>'
        f'<table class="data"><thead><tr><th>{"</th><th>".join(headers)}</th></tr></thead>'
        f'<tbody>{body}</tbody></table>'
        '</div>'
    )


def _collect_bw_chart_data() -> list:
    """Collect (operation, bandwidth) points for the bandwidth chart.

    Reads the canonical "Operation | Bandwidth (MB/s)" table
    from `memory_bandwidth_report.md`. Falls back to the first table whose
    header mentions `Bandwidth` if the canonical table is absent, and to
    a textual summary (`Read: 1234.56 MB/s`) as a last resort.
    """
    p = REPORTS / "memory_bandwidth_report.md"
    if not p.exists(): return []
    text = p.read_text()
    # 1) Try the canonical "Bandwidth (MB/s)" table first
    tbl = _find_data_table_after(text, "Bandwidth (MB/s)")
    if not tbl:
        # Last-resort: first table with a "Bandwidth" column
        tbl = _find_data_table_after(text, "Bandwidth")
    if tbl:
        headers, rows = tbl
        op_col = 0
        bw_col = None
        for i, h in enumerate(headers):
            hl = h.lower()
            if "operation" in hl: op_col = i
            if "bandwidth" in hl: bw_col = i
        if bw_col is None and len(headers) > 1: bw_col = 1
        if bw_col is not None:
            out = []
            for row in rows:
                if op_col >= len(row) or bw_col >= len(row): continue
                op = row[op_col].strip()
                if op not in ("Read", "Write", "Copy"): continue
                try: v = float(row[bw_col].replace(",", ""))
                except ValueError: continue
                if v > 0: out.append((op, v))
            if out: return out
    # 2) Fallback: textual summary like `Read: 1234.56 MB/s`
    import re
    out = []
    for op in ("Read", "Write", "Copy"):
        m = re.search(rf"{op}:\s+([0-9]+\.[0-9]+)\s*MB/s", text)
        if m:
            try: out.append((op, float(m.group(1))))
            except ValueError: pass
    return out


def _collect_branch_chart_data() -> list:
    """Collect (pattern, ns_per_branch) points for the branch ns/bar chart.

    Reads the canonical "Pattern | ns/branch" table from `cpu_branch_report.md`.
    Patterns with ns/branch = 0.00 indicate the compiler statically proved
    the branch condition is tautological; they are still emitted (with a
    `*` suffix in the label) so the bar chart shows the lower bound.
    """
    p = REPORTS / "cpu_branch_report.md"
    if not p.exists(): return []
    text = p.read_text()
    tbl = _find_data_table_after(text, "Pattern")
    if not tbl: return []
    headers, rows = tbl
    pat_col = 0
    ns_col = None
    for i, h in enumerate(headers):
        hl = h.lower()
        if hl == "pattern": pat_col = i
        if "ns/branch" in hl: ns_col = i
    if ns_col is None: return []
    out = []
    for row in rows:
        if pat_col >= len(row) or ns_col >= len(row): continue
        try: v = float(row[ns_col].replace(",", ""))
        except ValueError: continue
        label = row[pat_col].strip()
        # Keep zero values as 0 (zero_marker=True will render them as a stub)
        out.append((label, v))
    return out


def _collect_alu_kpis() -> list:
    """Compute KPI cards for the CPU ALU section.

    Pulls IPC for each integer operation and returns top-level stats
    (max IPC, average IPC, # operations). Falls back to empty list
    when no data is available.
    """
    p = REPORTS / "cpu_alu_report.md"
    if not p.exists(): return []
    text = p.read_text()
    tbl = _find_data_table_after(text, "IPC")
    if not tbl: return []
    headers, rows = tbl
    op_col, ipc_col = 0, None
    for i, h in enumerate(headers):
        if h.lower() == "ipc": ipc_col = i; break
    if ipc_col is None: return []
    ipcs = []
    for row in rows:
        if op_col >= len(row) or ipc_col >= len(row): continue
        try: v = float(row[ipc_col])
        except ValueError: continue
        if v > 0: ipcs.append((row[op_col].strip(), v))
    if not ipcs: return []
    vals = [v for _, v in ipcs]
    best_op, best_v = max(ipcs, key=lambda x: x[1])
    return [
        ("Best IPC",          f"{best_v:.2f}",  f"{best_op}"),
        ("Avg IPC",           f"{sum(vals)/len(vals):.2f}", "instructions/cycle"),
        ("Min IPC",           f"{min(vals):.2f}", "instructions/cycle"),
        ("Operations",        f"{len(ipcs)}",   "integer ops"),
    ]


def _collect_simd_kpis() -> list:
    """KPI cards for the SIMD/Float section. Returns ns/op stats for SIMD ops only."""
    p = REPORTS / "cpu_float_report.md"
    if not p.exists(): return []
    text = p.read_text()
    tbl = _find_data_table_after(text, "ns/op")
    if not tbl: return []
    headers, rows = tbl
    op_col, ns_col = 0, None
    for i, h in enumerate(headers):
        hl = h.lower()
        if "ns/op" in hl or "ns_per_op" in hl: ns_col = i
    if ns_col is None: ns_col = 3
    simd = []
    for row in rows:
        if op_col >= len(row) or ns_col >= len(row): continue
        if "simd" not in row[op_col].lower(): continue
        try: v = float(row[ns_col])
        except ValueError: continue
        if v > 0: simd.append((row[op_col].strip(), v))
    if not simd: return []
    vals = [v for _, v in simd]
    best_op, best_v = min(simd, key=lambda x: x[1])
    return [
        ("Fastest SIMD",      f"{best_v:.2f}",  f"{best_op}"),
        ("Avg SIMD",          f"{sum(vals)/len(vals):.2f}", "ns/op"),
        ("Slowest SIMD",      f"{max(vals):.2f}", "ns/op"),
        ("SIMD ops",          f"{len(simd)}",   "operations"),
    ]


def _collect_bw_kpis() -> list:
    """KPI cards for the Memory Bandwidth section.

    Returns per-operation (Read/Write/Copy) bandwidth as KPI cards.
    Falls back to text-summary parsing if the canonical table is absent.
    """
    p = REPORTS / "memory_bandwidth_report.md"
    if not p.exists(): return []
    text = p.read_text()
    pts = _collect_bw_chart_data()
    if pts: return [(op, f"{v:.1f}", "MB/s") for op, v in pts]
    return []


def _collect_intercore_kpis() -> list:
    """KPI cards for the Inter-Core Latency section.

    Derives min / max / diagonal-off-diagonal mean from the CAS matrix
    in `inter_core_latency_report.md`. Requires a real, populated matrix.
    """
    p = REPORTS / "inter_core_latency_report.md"
    if not p.exists(): return []
    text = p.read_text()
    # Matrix starts with header row "| **Core** | 0 | 1 | 2 | ... |" then
    # row "| 0 | - | 32.2 | 32.2 | ... |" etc. We want all data rows.
    import re
    matrix_rows = []
    seen_indices = set()  # guard: stop at second table (latency then throughput)
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"): continue
        if line.startswith("| **Core**") or line.startswith("|---"): continue
        # Split off the leading row label, parse the rest as floats.
        parts = [c.strip() for c in line.strip("|").split("|")]
        if not parts: continue
        try: row_idx = int(parts[0])  # first col should be the row index
        except ValueError: continue
        if row_idx in seen_indices:
            # Duplicate row index → second table (throughput). Stop.
            break
        seen_indices.add(row_idx)
        row_vals = []
        for c in parts[1:]:
            if c in ("-", ""): row_vals.append(None); continue
            # Handle "VALUE [P25-P75]" format by extracting the median
            if "[" in c:
                c = c.split("[")[0].strip()
            try: row_vals.append(float(c))
            except ValueError: row_vals.append(None)
        if row_vals: matrix_rows.append(row_vals)
    if not matrix_rows: return []
    flat = [v for row in matrix_rows for v in row if v is not None]
    if not flat: return []
    # Diagonal entries are None (skip), so all flat values are off-diagonal.
    out = [
        ("Min CAS latency",   f"{min(flat):.1f}", "ns"),
        ("Max CAS latency",   f"{max(flat):.1f}", "ns"),
        ("Mean CAS latency",  f"{sum(flat)/len(flat):.1f}", "ns"),
        ("Matrix size",       f"{len(matrix_rows)}×{len(matrix_rows)}", "cores"),
    ]
    return out


def _collect_multi_kpis() -> list:
    """KPI cards for the Multi-Core Scaling section.

    Reads the per-operation per-thread scaling table and returns the
    best speedup achieved, the best efficiency, and the operation that
    achieved the best speedup.
    """
    p = REPORTS / "cpu_multi_core_report.md"
    if not p.exists(): return []
    text = p.read_text()
    tbl = _find_data_table_after(text, "Speedup")
    if not tbl: return []
    headers, rows = tbl
    op_col, thr_col, sp_col, eff_col = 0, 1, None, None
    for i, h in enumerate(headers):
        hl = h.lower()
        if "speedup" in hl: sp_col = i
        elif "efficiency" in hl: eff_col = i
    if sp_col is None: return []
    best_sp, best_op, best_thr, best_eff = 0.0, "", "", 0.0
    effs = []
    for row in rows:
        if sp_col >= len(row) or eff_col is None or eff_col >= len(row): continue
        try:
            sp = float(row[sp_col].replace("x", ""))
            eff = float(row[eff_col].replace("%", ""))
        except ValueError: continue
        effs.append(eff)
        if sp > best_sp:
            best_sp = sp; best_op = row[op_col]; best_thr = row[thr_col]; best_eff = eff
    if not best_sp: return []
    return [
        ("Best speedup",      f"{best_sp:.2f}x", f"{best_op} @ {best_thr}T"),
        ("Best efficiency",   f"{best_eff:.1f}", "%"),
        ("Avg efficiency",    f"{sum(effs)/len(effs):.1f}", "%"),
        ("Sampled points",    f"{len(rows)}", "operation×thread"),
    ]


def _collect_branch_kpis() -> list:
    """Compute KPI cards for the CPU Branch section.

    Distinguishes patterns with real branches (ns > 0) from those the
    compiler statically folded (ns == 0). Returns (label, value, unit).
    """
    p = REPORTS / "cpu_branch_report.md"
    if not p.exists(): return []
    text = p.read_text()
    tbl = _find_data_table_after(text, "Pattern")
    if not tbl: return []
    headers, rows = tbl
    pat_col, ns_col = 0, None
    for i, h in enumerate(headers):
        hl = h.lower()
        if hl == "pattern": pat_col = i
        if "ns/branch" in hl: ns_col = i
    if ns_col is None: return []

    real_ns, real_names, zero_names = [], [], []
    for row in rows:
        if pat_col >= len(row) or ns_col >= len(row): continue
        try: v = float(row[ns_col].replace(",", ""))
        except ValueError: continue
        name = row[pat_col].strip()
        if v <= 0:
            zero_names.append(name)
        else:
            real_ns.append(v)
            real_names.append(name)

    out = []
    if real_ns:
        fastest = min(real_ns); slowest = max(real_ns)
        avg = sum(real_ns) / len(real_ns)
        out.append(("Fastest",       f"{fastest:.2f}", "ns/branch"))
        out.append(("Slowest",       f"{slowest:.2f}", "ns/branch"))
        out.append(("Real-branch avg", f"{avg:.2f}",   "ns/branch"))
        out.append(("Real branches",  f"{len(real_ns)}", f"of {len(real_ns) + len(zero_names)}"))
    else:
        out.append(("Patterns measured", "0", "(all folded)"))
    return out


def _bandwidth_table_html(text: str) -> str | None:
    """Return HTML for the bandwidth table — markdown table OR textual summary."""
    tbl = _find_data_table_after(text, "Bandwidth (MB/s)")
    if tbl:
        return _table_html(*tbl)
    # Build a tiny table from the textual summary as a last resort
    import re
    rows = []
    for op in ("Read", "Write", "Copy"):
        m = re.search(rf"{op}:\s+([0-9]+\.[0-9]+)\s*MB/s", text)
        if m:
            rows.append((op, m.group(1)))
    if not rows: return None
    headers = ["Operation", "Bandwidth (MB/s)"]
    body = "".join(f"<tr><td>{o}</td><td>{v}</td></tr>" for o, v in rows)
    return f'<table class="data"><thead><tr><th>{"</th><th>".join(headers)}</th></tr></thead><tbody>{body}</tbody></table>'


def _collect_alu_chart_data() -> list:
    p = REPORTS / "cpu_alu_report.md"
    if not p.exists(): return []
    text = p.read_text()
    # The 2nd table (skip System Config). Look for the per-op header.
    tbl = _find_data_table_after(text, "IPC") or _find_data_table(text, skip_first=True)
    if not tbl: return []
    headers, rows = tbl
    ipc_col = None
    for i, h in enumerate(headers):
        if h.lower() == "ipc": ipc_col = i; break
    if ipc_col is None: return []
    out = []
    for row in rows:
        try: v = float(row[ipc_col])
        except (ValueError, IndexError): continue
        if v > 0: out.append((row[0], v))
    return out


def _collect_simd_chart_data() -> list:
    p = REPORTS / "cpu_float_report.md"
    if not p.exists(): return []
    text = p.read_text()
    # The 2nd table (skip System Config).
    tbl = _find_data_table_after(text, "ns/op") or _find_data_table(text, skip_first=True)
    if not tbl: return []
    headers, rows = tbl
    ns_col = None
    for i, h in enumerate(headers):
        if "ns/op" in h.lower() or "ns_per_op" in h.lower(): ns_col = i; break
    if ns_col is None: ns_col = 3
    out = []
    for row in rows:
        if "simd" not in row[0].lower(): continue
        try: v = float(row[ns_col])
        except (ValueError, IndexError): continue
        if v > 0: out.append((row[0], v))
    return out


# ============================================================================
# Main HTML assembly
# ============================================================================

CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  margin: 0; padding: 20px; background: #f5f7fa; color: #1a1a1a;
}
.container { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 28px; margin: 0 0 5px 0; color: #1a1a1a; }
h2 { font-size: 20px; margin: 30px 0 12px 0; padding-bottom: 6px; border-bottom: 2px solid #e0e0e0; }
h3 { font-size: 16px; margin: 20px 0 8px 0; color: #444; }
.subtitle { color: #666; font-size: 14px; margin-bottom: 20px; }
.card {
  background: white; border-radius: 8px; padding: 20px; margin-bottom: 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}
.score-grid {
  display: grid; grid-template-columns: 280px 1fr; gap: 30px; align-items: center;
}
.cat-row {
  display: grid; grid-template-columns: 120px 1fr 70px;
  gap: 10px; align-items: center; margin: 6px 0;
}
.cat-row .name { font-weight: 500; color: #333; }
.cat-row .letter {
  text-align: center; padding: 3px 8px; border-radius: 4px;
  font-weight: bold; color: white; font-size: 14px;
}
.letter-A { background: #22c55e; } .letter-B { background: #84cc16; }
.letter-C { background: #eab308; } .letter-D { background: #f97316; } .letter-F { background: #ef4444; }
table.data {
  width: 100%; border-collapse: collapse; font-size: 13px; margin: 10px 0;
}
table.data th, table.data td {
  padding: 6px 10px; text-align: left; border-bottom: 1px solid #eee;
}
table.data th { background: #f8f9fa; font-weight: 600; color: #444; }
table.data tr:hover { background: #fafbfc; }
.metric-row {
  display: grid; grid-template-columns: 200px 1fr 80px;
  gap: 10px; align-items: center; padding: 4px 0; border-bottom: 1px solid #f0f0f0;
  font-size: 13px;
}
.metric-row .key { color: #555; font-family: 'SF Mono', Menlo, monospace; font-size: 12px; }
.metric-row .score-text { text-align: right; color: #888; font-size: 12px; }
.metric-row .score-good { color: #22c55e; }
.metric-row .score-warn { color: #f97316; }
.metric-row .score-bad { color: #ef4444; }
.charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.chart-img { width: 100%; height: auto; border: 1px solid #eee; border-radius: 4px; }
.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin: 12px 0 18px; }
.kpi { background: linear-gradient(135deg, #1a2540 0%, #243254 100%); color: #e0e6f0; padding: 14px 16px; border-radius: 6px; border: 1px solid #3a4870; }
.kpi-label { font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: #8aa0c8; margin-bottom: 6px; }
.kpi-value { font-size: 26px; font-weight: 700; color: #fff; }
.kpi-unit { font-size: 14px; font-weight: 400; color: #8aa0c8; margin-left: 3px; }
.warn { color: #f97316; }
.info { color: #666; font-size: 12px; margin: 8px 0; }
.footer { color: #999; font-size: 11px; text-align: center; margin-top: 30px; }
.no-data { color: #999; font-style: italic; }
"""


def build_html(score_dict: dict | None) -> str:
    parts = ['<!DOCTYPE html>', '<html lang="en">', '<head>',
             '<meta charset="UTF-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<title>Memory Benchmark Report</title>',
             f'<style>{CSS}</style>', '</head>', '<body>', '<div class="container">']
    parts.append('<h1>Memory Benchmark Report</h1>')
    parts.append(f'<p class="subtitle">Generated: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>')

    # --- System info: pinned to the very top, before all score/chart sections ---
    parts.append(_system_info_html())

    # --- Score section ---
    if score_dict:
        parts.append('<div class="card">')
        parts.append('<h2>Overall Score</h2>')
        parts.append('<div class="score-grid">')
        parts.append(_svg_score_gauge(score_dict["overall"], score_dict["letter"]))
        cat_html = '<div>'
        for c in score_dict["categories"]:
            cat_color = {"A": "#22c55e", "B": "#84cc16", "C": "#eab308",
                         "D": "#f97316", "F": "#ef4444"}.get(c["letter"], "#888888")
            cat_html += f'''<div class="cat-row">
              <span class="name">{html.escape(c["name"])}</span>
              {_svg_bar(c["score"], 100, width=300, color=cat_color)}
              <span class="letter letter-{c["letter"]}">{c["letter"]} ({c["score"]:.0f})</span>
            </div>'''
            cat_html += f'<div class="info">{c["n_pass"]} pass / {c["n_warn"]} warn / {c["n_fail"]} fail of {c["n_metrics"]} metrics</div>'
        cat_html += f'<div class="info">{score_dict["n_metrics_scored"]}/{score_dict["n_metrics_total"]} metrics scored.</div>'
        for w in score_dict.get("warnings", []):
            cat_html += f'<div class="warn">⚠ {html.escape(w)}</div>'
        cat_html += '</div>'
        parts.append(cat_html)
        parts.append('</div>')  # score-grid
        parts.append('</div>')  # card

        # --- Per-metric scores ---
        parts.append('<div class="card">')
        parts.append('<h2>Metric Scores</h2>')
        for m in score_dict["metrics"]:
            if m["score"] is None:
                cls = "score-text"
                score_html = '<span class="no-data">(no data)</span>'
                bar = ""
            else:
                if m["score"] >= 80: cls = "score-good"
                elif m["score"] >= 60: cls = "score-warn"
                else: cls = "score-bad"
                score_html = f'<span class="{cls}">{m["score"]:.1f}/100</span>'
                color = {"good": "#22c55e", "warn": "#f97316", "bad": "#ef4444"}.get(cls.split("-")[1] if "-" in cls else "good", "#888888")
                bar = _svg_bar(m["score"], 100, width=300, color=color)
            unit = ""
            if m["value"] is not None:
                if "latency" in m["key"] or "ns" in m["key"]: unit = " ns"
                elif "mbps" in m["key"]: unit = " MB/s"
            val_str = f'{m["value"]:.2f}{unit}' if m["value"] is not None else "—"
            parts.append(f'''<div class="metric-row">
              <span class="key" title="{html.escape(m["description"])}">{m["key"]}</span>
              {bar}
              <span class="score-text">{val_str} &nbsp; {score_html}</span>
            </div>''')
        parts.append('</div>')

    # --- Cache hierarchy chart ---
    # --- Cache Hierarchy (KPI + chart + table + interpretation) ---
    parts.append('<div class="card">')
    parts.append('<h2>Cache Hierarchy</h2>')
    parts.append(
        '<p style="margin:8px 0 14px 0;color:#444;line-height:1.5">'
        'Pointer-chase latency over working-set sizes spanning 1 KB to several GB. '
        'When the working set exceeds the L1d / L2 / L3 capacity, latency '
        'jumps sharply as misses start to hit the next level (L2 / L3 / RAM). '
        'Cache-level boundaries are <b>determined from measured latency jumps</b> — '
        'the <i>Expected</i> column is the level inferred from the latency boundary; '
        '<i>Analysis</i> describes the transition.  System-detected cache sizes are '
        'shown in the comparison table below as a reference only.'
        '</p>'
    )
    # KPI cards (L1D / L2 / L3 / RAM latency) — concrete numbers, prominent
    kpis = _collect_cache_kpis()
    if kpis:
        cards = "".join(
            f'<div class="kpi"><div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value">{html.escape(value)}<span class="kpi-unit">{html.escape(unit)}</span></div></div>'
            for label, value, unit in kpis
        )
        parts.append(f'<div class="kpi-row">{cards}</div>')
    cache_pts = _collect_cache_chart_data()
    if cache_pts:
        parts.append(_svg_line_chart(cache_pts, width=700, height=240,
                                      xlabel="Buffer size", ylabel="RdLat (ns)",
                                      title="Latency vs working-set size",
                                      log_y=True))
    else:
        parts.append('<p class="no-data">No cache data</p>')
    # Also include the table
    p = REPORTS / "cache_hierarchy_report.md"
    if p.exists():
        text = p.read_text()
        tbl = _find_data_table_after(text, "RdLat(ns)")
        if tbl:
            parts.append(_table_html(*tbl))
        # Inferred vs Reported comparison table — shows both the measured
        # boundary (from latency jumps) and the system-reported cache size.
        ir_tbl = _find_data_table_after(text, "Inferred (from latency)")
        if not ir_tbl:
            ir_tbl = _find_data_table_after(text, "Inferred")
        if ir_tbl:
            parts.append('<h3 style="margin-top:24px">Inferred vs Reported</h3>')
            parts.append(_table_html(*ir_tbl))
    # Interpretation
    if kpis and len(kpis) >= 4:
        try:
            l1, l2, l3, ram = (float(k[1]) for k in kpis[:4])
            ratio_l1l2 = l2 / l1 if l1 > 0 else 0
            ratio_l2l3 = l3 / l2 if l2 > 0 else 0
            ratio_l3ram = ram / l3 if l3 > 0 else 0
            note = (
                f'L1→L2 transition is <b>{ratio_l1l2:.1f}×</b>, '
                f'L2→L3 is <b>{ratio_l2l3:.1f}×</b>, '
                f'L3→RAM is <b>{ratio_l3ram:.1f}×</b>. '
            )
            if 1.5 <= ratio_l1l2 <= 5 and 1.1 <= ratio_l2l3 <= 3 and 1.5 <= ratio_l3ram <= 10:
                note += 'All three transitions fall in the expected range for a healthy cache hierarchy.'
            elif ratio_l3ram < 1.3:
                note += 'L3→RAM transition is suspiciously small — likely the working set did not exceed L3.'
            else:
                note += 'Some transitions look unusual; check whether the working set was large enough.'
            parts.append(f'<p style="margin:10px 0 0 0;color:#555;font-size:0.92em">{note}</p>')
        except (ValueError, IndexError):
            pass
    parts.append('</div>')

    # --- Bandwidth chart (KPI + chart + table + interpretation) ---
    parts.append('<div class="card">')
    parts.append('<h2>Memory Bandwidth</h2>')
    parts.append(
        '<p style="margin:8px 0 14px 0;color:#444;line-height:1.5">'
        'Multi-threaded sustained bandwidth for <b>Read</b>, <b>Write</b> and '
        '<b>Copy</b> (read-modify-write) over a 256 MB buffer with one thread '
        'per logical core. The numbers reflect what real workloads can expect: '
        'Read is usually the highest, Copy is bottlenecked by write bandwidth.'
        '</p>'
    )
    bw_kpis = _collect_bw_kpis()
    if bw_kpis:
        cards = "".join(
            f'<div class="kpi"><div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value">{html.escape(value)}<span class="kpi-unit">{html.escape(unit)}</span></div></div>'
            for label, value, unit in bw_kpis
        )
        parts.append(f'<div class="kpi-row">{cards}</div>')
    bw_pts = _collect_bw_chart_data()
    if bw_pts:
        parts.append(_svg_bar_chart(bw_pts, width=600, height=200,
                                    xlabel="Operation", ylabel="Bandwidth (MB/s)",
                                    title="Multi-channel memory bandwidth"))
    p = REPORTS / "memory_bandwidth_report.md"
    if p.exists():
        tbl_html = _bandwidth_table_html(p.read_text())
        if tbl_html:
            parts.append(tbl_html)
    if bw_kpis and len(bw_kpis) >= 3:
        try:
            read_v, write_v, copy_v = (float(k[1]) for k in bw_kpis[:3])
            note = (
                f'<b>Read</b> is {read_v:.0f} MB/s, <b>Write</b> {write_v:.0f} MB/s, '
                f'<b>Copy</b> {copy_v:.0f} MB/s. '
            )
            r_w = read_v / write_v if write_v > 0 else 0
            if r_w > 1.5:
                note += f'Read is {r_w:.1f}× faster than Write — common on DDR systems with write-allocate.'
            elif r_w > 1.0:
                note += 'Read modestly outpaces Write — typical of balanced memory subsystems.'
            else:
                note += 'Write is faster than Read — uncommon, may indicate a write-combining buffer or measurement noise.'
            parts.append(f'<p style="margin:10px 0 0 0;color:#555;font-size:0.92em">{note}</p>')
        except (ValueError, IndexError):
            pass
    parts.append('</div>')

    # --- Inter-core (KPI + heatmap + interpretation) ---
    parts.append('<div class="card">')
    parts.append('<h2>Inter-Core Latency</h2>')
    parts.append(
        '<p style="margin:8px 0 14px 0;color:#444;line-height:1.5">'
        'Cross-core CAS (compare-and-swap) latency matrix. Each cell shows '
        'the round-trip time in nanoseconds for a ping-pong CAS between two '
        'cores. Diagonal entries are skipped (same-core).'
        '</p>'
    )
    ic_kpis = _collect_intercore_kpis()
    if ic_kpis:
        cards = "".join(
            f'<div class="kpi"><div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value">{html.escape(value)}<span class="kpi-unit">{html.escape(unit)}</span></div></div>'
            for label, value, unit in ic_kpis
        )
        parts.append(f'<div class="kpi-row">{cards}</div>')
    heatmap = CHARTS / "inter_core_heatmap.png"
    uri = _png_data_uri(heatmap)
    if uri:
        parts.append(f'<img class="chart-img" src="{uri}" alt="Inter-core heatmap">')
    else:
        parts.append('<p class="no-data">No inter-core heatmap (run generate_report.py first to generate charts/)</p>')
    tp_heatmap = CHARTS / "inter_core_throughput.png"
    tp_uri = _png_data_uri(tp_heatmap)
    if tp_uri:
        parts.append(f'<h3>CAS Throughput (MOPS)</h3>')
        parts.append(f'<img class="chart-img" src="{tp_uri}" alt="Inter-core throughput heatmap">')
    if ic_kpis and len(ic_kpis) >= 3:
        try:
            lo, hi, mean = float(ic_kpis[0][1]), float(ic_kpis[1][1]), float(ic_kpis[2][1])
            spread = hi - lo
            note = (
                f'Latency ranges from <b>{lo:.1f} ns</b> to <b>{hi:.1f} ns</b> '
                f'(spread {spread:.1f} ns, mean {mean:.1f} ns). '
            )
            if spread < mean * 0.3:
                note += 'Spread is small — likely a flat topology (single NUMA node or tightly-coupled mesh).'
            elif spread < mean * 0.8:
                note += 'Moderate spread — probably two NUMA nodes or chiplet halves with separate L3 slices.'
            else:
                note += 'Large spread — multiple NUMA domains with significantly different hop counts.'
            parts.append(f'<p style="margin:10px 0 0 0;color:#555;font-size:0.92em">{note}</p>')
        except (ValueError, IndexError):
            pass
    parts.append('</div>')

    # --- CPU ALU (KPI + chart + table + interpretation) ---
    parts.append('<div class="card">')
    parts.append('<h2>CPU ALU (Integer)</h2>')
    parts.append(
        '<p style="margin:8px 0 14px 0;color:#444;line-height:1.5">'
        'Throughput of common integer operations. IPC (instructions per cycle) '
        'is the primary signal: scalar ops typically max out at ~1 IPC on a '
        '1-wide issue CPU, or higher on superscalar designs. Sustained IPC '
        'close to the theoretical peak means the front-end and execution units '
        'are well fed; much lower IPC indicates stalls (cache misses, '
        'dependencies, branch mispredicts).'
        '</p>'
    )
    alu_kpis = _collect_alu_kpis()
    if alu_kpis:
        cards = "".join(
            f'<div class="kpi"><div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value">{html.escape(value)}<span class="kpi-unit">{html.escape(unit)}</span></div></div>'
            for label, value, unit in alu_kpis
        )
        parts.append(f'<div class="kpi-row">{cards}</div>')
    alu_pts = _collect_alu_chart_data()
    if alu_pts:
        parts.append(_svg_bar_chart(alu_pts, width=600, height=200,
                                     xlabel="Operation", ylabel="IPC (instructions/cycle)",
                                     title="Integer operation throughput"))
    p = REPORTS / "cpu_alu_report.md"
    if p.exists():
        text = p.read_text()
        tbl = _find_data_table_after(text, "IPC")
        if tbl:
            parts.append(_table_html(*tbl))
    if alu_kpis and len(alu_kpis) >= 3:
        try:
            best = float(alu_kpis[0][1])
            avg = float(alu_kpis[1][1])
            worst = float(alu_kpis[2][1])
            note = (
                f'Best IPC is <b>{best:.2f}</b>, average <b>{avg:.2f}</b>, '
                f'worst <b>{worst:.2f}</b>. '
            )
            if best >= 2.0:
                note += 'Superscalar execution confirmed — at least 2 integer ops dispatched per cycle.'
            elif best >= 1.0:
                note += 'Single-issue throughput — the core issues at most one integer op per cycle.'
            else:
                note += 'IPC below 1.0 — heavy register pressure or microcode stalls.'
            parts.append(f'<p style="margin:10px 0 0 0;color:#555;font-size:0.92em">{note}</p>')
        except (ValueError, IndexError):
            pass
    else:
        # KPI collector returned nothing — likely because PMU was restricted
        # and every row's IPC reads "N/A". Surface this so the user knows.
        parts.append(
            '<p style="margin:10px 0 0 0;color:#888;font-size:0.92em;font-style:italic">'
            'No IPC numbers available — PMU is restricted on this host '
            '(see <code>perf_event_paranoid</code> in System Information). '
            'The data table below shows <b>ns/op</b> as a fallback signal.'
            '</p>'
        )
    parts.append('</div>')

    # --- CPU Float (SIMD) (KPI + chart + table + interpretation) ---
    parts.append('<div class="card">')
    parts.append('<h2>CPU Floating Point &amp; SIMD (NEON)</h2>')
    parts.append(
        '<p style="margin:8px 0 14px 0;color:#444;line-height:1.5">'
        'Latency of floating-point and SIMD (128-bit NEON) operations. '
        'Lower ns/op is better; SIMD should be several times faster than '
        'the equivalent scalar code. SIMD-only operations are filtered '
        'out from the chart; scalar floats stay in the data table.'
        '</p>'
    )
    simd_kpis = _collect_simd_kpis()
    if simd_kpis:
        cards = "".join(
            f'<div class="kpi"><div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value">{html.escape(value)}<span class="kpi-unit">{html.escape(unit)}</span></div></div>'
            for label, value, unit in simd_kpis
        )
        parts.append(f'<div class="kpi-row">{cards}</div>')
    simd_pts = _collect_simd_chart_data()
    if simd_pts:
        parts.append(_svg_bar_chart(simd_pts, width=600, height=200,
                                     xlabel="Operation", ylabel="ns/op",
                                     title="SIMD (128-bit NEON) latency per op"))
    p = REPORTS / "cpu_float_report.md"
    if p.exists():
        text = p.read_text()
        tbl = _find_data_table_after(text, "ns/op")
        if tbl:
            parts.append(_table_html(*tbl))
    if simd_kpis and len(simd_kpis) >= 3:
        try:
            best = float(simd_kpis[0][1])
            avg = float(simd_kpis[1][1])
            worst = float(simd_kpis[2][1])
            ratio = worst / best if best > 0 else 0
            note = (
                f'Fastest SIMD op is <b>{best:.2f} ns/op</b>, average <b>{avg:.2f}</b>, '
                f'slowest <b>{worst:.2f}</b> ({ratio:.1f}× spread). '
            )
            if best < 1.0:
                note += 'SIMD pipeline is running at &lt;1 ns/op — multiple SIMD ops issued per cycle.'
            elif best < 4.0:
                note += 'Single-issue SIMD throughput — one SIMD op per few cycles.'
            else:
                note += 'Slow SIMD pipeline — likely reciprocal/sqrt or division microcode.'
            parts.append(f'<p style="margin:10px 0 0 0;color:#555;font-size:0.92em">{note}</p>')
        except (ValueError, IndexError):
            pass
    parts.append('</div>')

    # --- Multi-Core Scaling (KPI + PNG chart + table + interpretation) ---
    parts.append('<div class="card">')
    parts.append('<h2>Multi-Core Scaling</h2>')
    parts.append(
        '<p style="margin:8px 0 14px 0;color:#444;line-height:1.5">'
        'How well each operation scales as we add threads. <b>Speedup</b> is '
        'time(1 thread) / time(N threads); <b>Efficiency</b> is speedup / N. '
        'Memory-bound operations (Mul, FPU) typically keep &gt;80% efficiency '
        'until they hit the memory bandwidth wall; ALU bandwidth-bound ops '
        'saturate earlier.'
        '</p>'
    )
    multi_kpis = _collect_multi_kpis()
    if multi_kpis:
        cards = "".join(
            f'<div class="kpi"><div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value">{html.escape(value)}<span class="kpi-unit">{html.escape(unit)}</span></div></div>'
            for label, value, unit in multi_kpis
        )
        parts.append(f'<div class="kpi-row">{cards}</div>')
    multi_png = CHARTS / "multi_core_scaling.png"
    multi_uri = _png_data_uri(multi_png)
    if multi_uri:
        parts.append(f'<img class="chart-img" src="{multi_uri}" alt="Multi-core scaling">')
    else:
        parts.append('<p class="no-data">No multi-core chart (run generate_report.py first to generate charts/)</p>')
    p = REPORTS / "cpu_multi_core_report.md"
    if p.exists():
        text = p.read_text()
        tbl = _find_data_table_after(text, "Speedup")
        if tbl:
            parts.append(_table_html(*tbl))
        else:
            parts.append('<p class="no-data">No multi-core table</p>')
    else:
        parts.append('<p class="no-data">cpu_multi_core_report.md not found</p>')
    if multi_kpis and len(multi_kpis) >= 3:
        try:
            sp = float(multi_kpis[0][1].replace("x", ""))
            eff = float(multi_kpis[1][1])
            note = (
                f'Best speedup is <b>{sp:.2f}×</b> with <b>{eff:.1f}%</b> efficiency. '
            )
            if sp >= 8 and eff >= 80:
                note += 'Excellent scaling — the operation is genuinely parallel and not memory-bound.'
            elif sp >= 4 and eff >= 50:
                note += 'Decent scaling — some shared-resource contention (L3, memory bus) is showing up.'
            elif sp >= 2:
                note += 'Limited scaling — the workload is bandwidth- or contention-bound beyond a few threads.'
            else:
                note += 'Poor scaling — likely serialised on a shared resource (lock, atomic, single-port L3).'
            parts.append(f'<p style="margin:10px 0 0 0;color:#555;font-size:0.92em">{note}</p>')
        except (ValueError, IndexError):
            pass
    parts.append('</div>')

    # --- CPU Branch (KPI cards + bar chart + table + interpretation) ---
    parts.append('<div class="card"><h2>CPU Branch</h2>')
    # Header paragraph: what this test measures + caveat about compiler folding
    parts.append(
        '<p style="margin:8px 0 14px 0;color:#444;line-height:1.5">'
        'Each row measures wall-clock cost per branch across distinct branch patterns: '
        '<i>Predictable</i> (always/never taken) saturates the predictor, '
        '<i>Unpredictable</i> (random 50%) is the worst case, '
        '<i>Pattern</i> toggles test the pattern-history table, '
        '<i>Adaptive</i> tests hysteresis recovery. '
        'Rows with <code>0.00</code> ns/branch marked with <code>*</code> indicate '
        'the compiler statically proved the condition tautological and emitted '
        'no branch instruction (lower bound, not a predictor measurement). '
        'The Mispred Rate column requires unrestricted <code>perf_event_open</code> '
        '(N/A on this system — see <code>perf_event_paranoid</code> in Notes).'
        '</p>'
    )
    p = REPORTS / "cpu_branch_report.md"
    if p.exists():
        text = p.read_text()
        # The header is `| Pattern | Category | ...` (newer) or
        # `| Operation | Pattern | ...` (older). Try "Pattern" first
        # (always present, always a column) then fall back.
        tbl = _find_data_table_after(text, "Pattern")
        if not tbl:
            tbl = _find_data_table_after(text, "Operation")

        # KPI cards
        kpis = _collect_branch_kpis()
        if kpis:
            cards = "".join(
                f'<div class="kpi"><div class="kpi-label">{html.escape(label)}</div>'
                f'<div class="kpi-value">{html.escape(value)}<span class="kpi-unit">{html.escape(unit)}</span></div></div>'
                for label, value, unit in kpis
            )
            parts.append(f'<div class="kpi-row">{cards}</div>')

        # Bar chart of ns/branch per pattern (zero_marker draws a grey
        # stub for patterns the compiler folded, so they're visible).
        branch_pts = _collect_branch_chart_data()
        if branch_pts:
            parts.append(_svg_bar_chart(branch_pts, width=720, height=220,
                                        xlabel="Branch pattern",
                                        ylabel="ns / branch",
                                        title="Branch cost per pattern (lower = better; grey stub = no branch emitted)",
                                        zero_marker=True))

        # Data table (existing)
        if tbl:
            parts.append(_table_html(*tbl))

        # Interpretation paragraph (auto-derived from the data)
        if kpis and len(kpis) >= 3:
            real_avg = float(kpis[2][1])
            note = (
                f'Average cost on patterns with real branches is <b>{real_avg:.2f} ns/branch</b>. '
                f'On a {3000} MHz CPU, this is ~{real_avg * 3:.1f} cycles per branch. '
            )
            if real_avg < 1.0:
                note += 'The predictor is performing well — most branches resolve in &lt;1 cycle on average.'
            elif real_avg < 2.0:
                note += 'Predictor performance is reasonable; some miss penalty is visible.'
            else:
                note += 'High miss penalty — consider whether the workload has data-dependent control flow.'
            parts.append(f'<p style="margin:10px 0 0 0;color:#555;font-size:0.92em">{note}</p>')
    else:
        parts.append('<p class="no-data">cpu_branch_report.md not found</p>')
    parts.append('</div>')

    parts.append('<div class="footer">Memorytest v1.0.1 | Reports: reports/*.md | Source: 7 binaries under bin/</div>')
    parts.append('</div></body></html>')
    return "\n".join(parts)


def _ensure_charts() -> None:
    """Regenerate the matplotlib PNG charts in reports/charts/.

    The HTML report embeds three PNGs (cache latency, memory bandwidth,
    inter-core heatmap, multi-core scaling). They are produced by
    `generate_report.py`'s `create_*` functions. The C binaries also
    write markdown reports and a JSON, but they do NOT regenerate these
    PNGs (the C side no longer calls a separate heatmap script).
    So before the HTML pipeline runs, we call the create_* helpers to
    refresh the PNGs from the freshly-written markdown/JSON reports.

    Safe to call multiple times — each helper is idempotent.
    """
    try:
        import generate_report as gr
    except ImportError as e:
        print(f"# WARN: cannot import generate_report ({e}) — charts will be missing", file=sys.stderr)
        print("#       Install: pip3 install matplotlib --break-system-packages", file=sys.stderr)
        return
    CHARTS.mkdir(parents=True, exist_ok=True)
    cache_md = REPORTS / "cache_hierarchy_report.md"
    bw_md    = REPORTS / "memory_bandwidth_report.md"
    ic_md    = REPORTS / "inter_core_latency_report.md"
    ic_json  = REPORTS / "inter_core_heatmap_data.json"
    multi_md = REPORTS / "cpu_multi_core_report.md"
    alu_md   = REPORTS / "cpu_alu_report.md"
    float_md = REPORTS / "cpu_float_report.md"
    # Cache latency (no markdown consumer — chart from parsed table)
    if cache_md.exists():
        try:
            data = gr.parse_cache_report(str(cache_md))
            gr.create_cache_latency_chart(data, str(CHARTS / "cache_latency.png"))
        except Exception as e:
            print(f"# WARN: cache_latency chart failed: {e}", file=sys.stderr)
    # Memory bandwidth
    if bw_md.exists():
        try:
            gr.create_bandwidth_chart(str(bw_md), str(CHARTS / "memory_bandwidth.png"))
        except Exception as e:
            print(f"# WARN: memory_bandwidth chart failed: {e}", file=sys.stderr)
    # Inter-core heatmap (use the freshly-written JSON when present so the
    # self-sentinel nulls are honoured by the heatmap renderer; fall back
    # to the markdown otherwise).
    if ic_json.exists():
        try:
            gr.create_inter_core_heatmap_from_json(str(ic_json), str(CHARTS / "inter_core_heatmap.png"))
        except AttributeError:
            # older generate_report.py — parse from the markdown instead
            if ic_md.exists():
                try:
                    gr.create_inter_core_heatmap(str(ic_md), str(CHARTS / "inter_core_heatmap.png"))
                except Exception as e:
                    print(f"# WARN: inter-core heatmap failed: {e}", file=sys.stderr)
        except Exception as e:
            print(f"# WARN: inter-core heatmap failed: {e}", file=sys.stderr)
    elif ic_md.exists():
        try:
            gr.create_inter_core_heatmap(str(ic_md), str(CHARTS / "inter_core_heatmap.png"))
        except Exception as e:
            print(f"# WARN: inter-core heatmap failed: {e}", file=sys.stderr)
    # Inter-core throughput heatmap (MOPS)
    if ic_json.exists():
        try:
            gr.create_inter_core_throughput_heatmap_from_json(str(ic_json), str(CHARTS / "inter_core_throughput.png"))
        except Exception as e:
            print(f"# WARN: inter-core throughput heatmap failed: {e}", file=sys.stderr)
    # Multi-core scaling
    if multi_md.exists():
        try:
            data = gr.extract_multi_core_data(str(multi_md))
            gr.create_multi_core_scaling_chart(data, str(CHARTS / "multi_core_scaling.png"))
        except Exception as e:
            print(f"# WARN: multi-core scaling chart failed: {e}", file=sys.stderr)
    # CPU ALU / Float (matplotlib flavours — informational, HTML uses SVG)
    if alu_md.exists():
        try:
            gr.create_cpu_chart(str(alu_md), str(CHARTS / "cpu_alu.png"), chart_type="alu")
        except Exception as e:
            print(f"# WARN: cpu_alu chart failed: {e}", file=sys.stderr)
    if float_md.exists():
        try:
            gr.create_cpu_chart(str(float_md), str(CHARTS / "cpu_float.png"), chart_type="float")
        except Exception as e:
            print(f"# WARN: cpu_float chart failed: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", "-o", type=Path, default=DEFAULT_OUTPUT, help=f"Output HTML path (default: {DEFAULT_OUTPUT})")
    ap.add_argument("--skip-charts", action="store_true", help="Don't regenerate the matplotlib PNGs (HTML will use whatever is already in charts/)")
    args = ap.parse_args()

    if not args.skip_charts:
        _ensure_charts()

    score_dict = None
    if HAS_SCORE:
        try:
            summary = score_run()
            display = collect_display_data()
            score_dict = score_to_dict(summary, display)
            print(f"Score: {score_dict['overall']:.1f}/100 ({score_dict['letter']})")
        except Exception as e:
            print(f"# WARN: scoring failed: {e}", file=sys.stderr)
    else:
        print(f"# WARN: report_score not available: {_SCORE_ERR}", file=sys.stderr)

    html_str = build_html(score_dict)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_str)
    size_kb = args.output.stat().st_size / 1024
    print(f"HTML report written: {args.output}  ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
