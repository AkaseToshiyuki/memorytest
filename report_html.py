#!/usr/bin/env python3
"""Offline bilingual HTML reports from the shared native-desktop result model.

All tables and SVG plots are embedded. No Matplotlib, network assets, performance
interpretation or legacy diagnostic grades. ComputeMark v2 inputs remain strict.
"""
import argparse
import json
from pathlib import Path
import re
import sys
from benchmark_registry import TASKS as TESTS, select_modules, report_filename

ROOT = Path(__file__).parent.resolve()
REPORTS = ROOT / "reports"
CHARTS = REPORTS / "charts"  # retained for callers using the old module attribute
DEFAULT_OUTPUT = REPORTS / "benchmark_report.html"
_report_cache = {}


def _parse_all_tables(text):
    """Legacy Markdown/ASCII validator used by run_benchmarks, not a score parser."""
    out = []
    delimiter = re.compile(r"^[\s\-:|]+\|?$")
    section = re.compile(r"^--\s+.+\s+--$")
    lines = text.splitlines()
    split = lambda line: [cell.strip().rstrip("*").strip() for cell in line.strip().strip("|").split("|")]
    index = 0
    while index < len(lines) - 1:
        if "|" in lines[index] and delimiter.match(lines[index + 1]):
            headers, rows = split(lines[index]), []
            index += 2
            while index < len(lines):
                row = lines[index]
                if not row.strip() or section.match(row):
                    index += 1
                    continue
                if "|" not in row or delimiter.match(row):
                    break
                cells = split(row)
                if len(cells) == len(headers):
                    rows.append(cells)
                index += 1
            if rows:
                out.append((headers, rows))
        else:
            index += 1
    return out


def _read_report(stem):
    """Compatibility reader with per-directory, per-render caching."""
    path = REPORTS / f"{stem}_report.md"
    key = path.resolve()
    if key not in _report_cache:
        _report_cache[key] = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return _report_cache[key]


def build_html(score_dict=None, modules=None, manifest=None, legacy_scores=False, language=None, theme="dark"):
    """Compatibility signature; external/precomputed grades are deliberately ignored."""
    from report_view import build_report
    _report_cache.clear()
    return build_report(REPORTS, modules or select_modules(), manifest, language, theme)


def main():
    global REPORTS, CHARTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", "-o", type=Path, help="Output HTML path")
    parser.add_argument("--reports-dir", type=Path, default=REPORTS, help="Read this report directory only")
    parser.add_argument("--module", "-m", action="append", choices=TESTS)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--manifest", type=Path)
    source.add_argument("--run-dir", type=Path)
    source.add_argument("--latest-run", action="store_true")
    parser.add_argument("--legacy-scores", action="store_true", help="Deprecated compatibility flag; grades are not shown")
    parser.add_argument("--language", choices=["zh", "en"])
    parser.add_argument("--theme", choices=["light", "dark"], default="dark")
    parser.add_argument("--skip-charts", action="store_true", help="Compatibility flag; embedded SVG plots are always available")
    args = parser.parse_args()
    if args.latest_run:
        candidates = []
        for path in (args.reports_dir / "runs").glob("*/manifest.json"):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("finished_at"):
                    candidates.append(path)
            except (OSError, ValueError):
                continue
        if not candidates:
            parser.error("No completed run found; use make test or specify --reports-dir")
        args.run_dir = max(candidates, key=lambda path: path.stat().st_mtime_ns).parent
    if args.run_dir:
        args.manifest = args.run_dir / "manifest.json"
        args.reports_dir = args.run_dir / "reports"
    REPORTS = args.reports_dir.resolve()
    CHARTS = REPORTS / "charts"
    manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else None
    modules = select_modules(args.module or (manifest.get("selected_modules") if manifest else None))
    if manifest and not set(modules) <= set(manifest["selected_modules"]):
        parser.error("Requested module was not selected in this run")
    args.output = args.output or (args.run_dir or REPORTS) / report_filename(modules)
    content = build_html(None, modules, manifest, language=args.language, theme=args.theme)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"HTML report written: {args.output}  ({args.output.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
