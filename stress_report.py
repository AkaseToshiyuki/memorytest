"""Offline stress report; bounded plot samples, full-session statistics from the archive."""
import html
import json
from collections import deque
from pathlib import Path
from i18n import display, ref, tr
from presentation import plot_svg
from report_view import document, table_html
from result_model import Chart, Table, column


def build_stress_report(directory, output=None, language=None, theme="dark", window_seconds=None):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    samples_path = directory / "samples.jsonl"
    samples = []
    if samples_path.is_file():
        # Keep only the bounded live window; statistics remain full-session.
        with samples_path.open() as stream:
            samples = [json.loads(line) for line in deque(stream, maxlen=601)]
        seconds = min(600, max(60, window_seconds or manifest.get("plot_window_seconds", 300)))
        cutoff = samples[-1]["elapsed"] - seconds if samples else 0
        samples = [sample for sample in samples if sample["elapsed"] >= cutoff]
    catalog, stats = manifest.get("catalog", {}), manifest.get("statistics", {})
    rows = [[item["label"], item["unit"], *[stats.get(key, {}).get(field) for field in ("current", "minimum", "maximum", "average")]] for key, item in catalog.items()]
    table = Table("statistics", [column(key) for key in ("sensors", "unit", "current", "minimum", "maximum", "average")], rows)
    charts = []
    order = {"°C": 0, "%": 1, "MHz": 2, "W": 3, "V": 4, "RPM": 5}
    for unit in sorted({item["unit"] for item in catalog.values()}, key=lambda value: (order.get(value, 99), value)):
        series = [{"label": item["label"], "values": [sample["values"].get(key) for sample in samples]} for key, item in catalog.items() if item["unit"] == unit]
        for start in range(0, len(series), 3):
            xs = [round(sample["elapsed"], 1) for sample in samples]
            charts.append(Chart("monitoring", "line", unit, xs, series[start:start + 3], xs, "seconds"))
    bodies = {}
    for lang in ("en", "zh"):
        esc = lambda value: html.escape(display(value, lang))
        bits = [f'<h1>{tr("stress", lang)}</h1><section class="card"><p>{tr("run_id", lang)}: {esc(manifest["run_id"])}</p>',
                f'<p>{tr("status", lang)}: {tr(manifest["status"], lang)} · {tr(manifest.get("reason", "user_stop"), lang)}</p>',
                f'<p>{tr("duration", lang)}: {manifest.get("elapsed", 0):.1f} s · {tr("threads", lang)}: {manifest["threads"]}</p></section>',
                '<section class="card">' + table_html(table, lang) + '</section>']
        for chart in charts:
            bits.append('<section class="card">' + plot_svg(chart, lang, css=True) + '</section>')
        events = Table("events", [column("elapsed", "s"), column("status"), column("loads")],
                       [[entry["elapsed"], ref(entry["event"]), " · ".join(tr(mode, lang) for mode in entry.get("modes", []))] for entry in manifest.get("events", [])])
        bits.append('<section class="card">' + table_html(events, lang) + '</section>')
        bodies[lang] = ''.join(bits)
    content = document(bodies, language, theme, title="MemoryTest", preferences_key="memorytest-report")
    if output:
        Path(output).write_text(content, encoding="utf-8")
    return content
