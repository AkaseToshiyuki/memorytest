"""Offline stress report; bounded plot samples, full-session statistics from the archive."""
import html
import json
from pathlib import Path
from i18n import display, ref, tr
from presentation import plot_svg
from report_view import document, table_html
from result_model import Chart, Table, column


def build_stress_report(directory, output=None, language=None, theme="dark"):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    samples_path = directory / "samples.jsonl"
    samples = []
    if samples_path.is_file():
        # A manual session may run for days. Do not load its full time series into RAM.
        with samples_path.open() as stream:
            count = sum(1 for _ in stream)
        stride = max(1, (count + 1199) // 1200)
        with samples_path.open() as stream:
            for index, line in enumerate(stream):
                if index % stride == 0 or index == count - 1:
                    samples.append(json.loads(line))
    catalog, stats = manifest.get("catalog", {}), manifest.get("statistics", {})
    rows = [[item["label"], item["unit"], *[stats.get(key, {}).get(field) for field in ("current", "minimum", "maximum", "average")]] for key, item in catalog.items()]
    table = Table("statistics", [column(key) for key in ("sensors", "unit", "current", "minimum", "maximum", "average")], rows)
    charts = []
    for unit in dict.fromkeys(item["unit"] for item in catalog.values()):
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
