"""Shared color tokens and SVG plots; standard library only."""
import colorsys
import html
import math
from i18n import display, tr

THEMES = {
    "dark": dict(background="#0c0e12", surface="#1a1d23", base="#111419", text="#e9edf2", muted="#adb7c4",
                 border="#3a424e", accent="#9dcde7", button="#29333f", selection="#374e63", disabled="#929dac", success="#a0d4c6", error="#f0abb4", warning="#e7c56e",
                 frontend="#c0c6cf", frontend_fill="#30343c", cache="#e39793", cache_fill="#43292b",
                 register="#7ac7de", register_fill="#173642", compute="#c6a0e5", compute_fill="#352341",
                 memory="#c7d391", memory_fill="#353d23"),
    "light": dict(background="#f0f2f5", surface="#ffffff", base="#f6f7f9", text="#223044", muted="#526378",
                  border="#bdc6d2", accent="#315d85", button="#e5eaf1", selection="#c5d9ee", disabled="#65758a", success="#236858", error="#a82c43", warning="#806000",
                  frontend="#53606c", frontend_fill="#e8ecf0", cache="#933c36", cache_fill="#f8e9e7",
                  register="#24647b", register_fill="#e5f1f6", compute="#6c428d", compute_fill="#eee5f5",
                  memory="#5b6c29", memory_fill="#edf1df"),
}


def module_category(name):
    """Shared semantic accents for native results and standalone reports."""
    if name.startswith("cache"): return "cache"
    if name.startswith("memory"): return "memory"
    if name in {"inter_core", "cpu_architecture"}: return "register"
    if name.startswith("cpu_"): return "compute"
    return "frontend"


def curve_color(index, theme="dark"):
    hue = (0.58 + index * 0.618033988749895) % 1
    rgb = colorsys.hls_to_rgb(hue, 0.72 if theme == "dark" else 0.35, 0.58)
    return "#" + "".join(f"{round(channel * 255):02x}" for channel in rgb)


def plot_svg(chart, language="en", theme="dark", css=False, width=720, height=280):
    get = (lambda key: getattr(chart, key)) if not isinstance(chart, dict) else chart.__getitem__
    kind, labels, series, unit = (get(key) for key in ("kind", "labels", "series", "unit"))
    color = lambda key: f"var(--{key})" if css else THEMES[theme][key]
    if kind == "architecture":
        from architecture_diagram import architecture_svg
        return architecture_svg(chart, language, color)
    esc = lambda value: html.escape(display(value, language), quote=True)
    if kind == "bar":
        height = max(height, 38 * len(labels) + 50)
    if kind == "heatmap":
        height = min(760, max(320, len(labels) * 16 + 80))
    elements = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(tr(get("title"), language))}">',
                f'<rect width="{width}" height="{height}" fill="{color("surface")}"/>']

    def text(x, y, value, anchor="start", size=12, fill=None):
        elements.append(f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" font-size="{size}" font-family="sans-serif" fill="{fill or color("text")}">{esc(value)}</text>')

    def line(x1, y1, x2, y2):
        elements.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" stroke="{color("border")}"/>')

    def paint(index):
        value = curve_color(index, theme)
        return f"var(--curve-{index}, {value})" if css else value

    if kind == "heatmap":
        matrix = series[0]["values"] if series else []
        finite = [v for row in matrix for v in row if isinstance(v, (int, float)) and math.isfinite(v)]
        low, high = (min(finite), max(finite)) if finite else (0, 1)
        cell = min((width - 110) / max(1, len(labels)), (height - 80) / max(1, len(labels)))
        skip = max(1, math.ceil(len(labels) / 20))
        for i, row in enumerate(matrix):
            for j, value in enumerate(row):
                present = isinstance(value, (int, float)) and math.isfinite(value)
                t = max(0, min(1, (value - low) / (high - low or 1))) if present else 0
                rgb = colorsys.hls_to_rgb(0.59 - 0.1 * t, 0.76 - 0.45 * t, 0.38)
                fill = "#" + "".join(f"{round(v * 255):02x}" for v in rgb) if present else color("base")
                elements.append(f'<rect x="{65 + j * cell:.2f}" y="{35 + i * cell:.2f}" width="{cell:.2f}" height="{cell:.2f}" fill="{fill}"><title>{esc(labels[i])} → {esc(labels[j])}: {esc(value)} {esc(unit)}</title></rect>')
            if i % skip == 0:
                text(56, 35 + (i + 0.65) * cell, labels[i], "end", 10)
                text(65 + (i + 0.5) * cell, 26, labels[i], "middle", 10)
        text(65, height - 12, f'{tr("minimum", language)} {low:.3g} · {tr("maximum", language)} {high:.3g} {unit}')
    elif kind == "bar":
        values = series[0]["values"] if series else []
        maximum = max((v for v in values if isinstance(v, (int, float)) and math.isfinite(v)), default=1) or 1
        left, usable = 225, width - 325
        for i, (label, value) in enumerate(zip(labels, values)):
            y = 28 + i * 38
            text(left - 12, y + 16, label, "end", 11)
            if value is None:
                text(left, y + 16, None)
                continue
            bar = max(0, value / maximum * usable)
            elements.append(f'<rect x="{left}" y="{y}" width="{bar:.2f}" height="22" rx="3" fill="{paint(i)}"/>')
            text(left + bar + 8, y + 16, f"{value:.4g}", size=11)
        text(width - 16, height - 8, unit, "end")
    else:
        left, right, top, bottom = 72, width - 24, 40, height - 55
        xs = get("x") or list(range(len(labels)))
        valid_x = [x for x in xs if isinstance(x, (int, float)) and math.isfinite(x)]
        log_x = get("log_x")
        transform = (lambda x: math.log10(max(x, 1e-12))) if log_x else (lambda x: x)
        lo, hi = min(map(transform, valid_x), default=0), max(map(transform, valid_x), default=1)
        finite = [v for item in series for v in item["values"] if isinstance(v, (int, float)) and math.isfinite(v)]
        ymax = max(finite, default=1) or 1
        for i in range(5):
            y = bottom - (bottom - top) * i / 4
            line(left, y, right, y)
            text(left - 9, y + 4, f"{ymax * i / 4:.3g}", "end", 11)
        if xs:
            for i in sorted(set(round(i * (len(xs) - 1) / 4) for i in range(5))):
                if xs[i] is not None:
                    x = left + (transform(xs[i]) - lo) / (hi - lo or 1) * (right - left)
                    text(x, bottom + 20, labels[i], "middle", 10)
        text(left, 18, unit)
        x_title = get("x_title")
        x_unit = " (KiB)" if log_x and x_title == "workset" else " (B)" if x_title == "arch_code_bytes" else ""
        text((left + right) / 2, height - 7, tr(x_title, language) + x_unit, "middle")
        for index, item in enumerate(series):
            text(right - index * 190, 18, item["label"], "end", 11, paint(index))
            segment = []
            def flush():
                if segment:
                    elements.append(f'<polyline points="{" ".join(segment)}" fill="none" stroke="{paint(index)}" stroke-width="2"/>')
                    segment.clear()
            for xvalue, value in zip(xs, item["values"]):
                if xvalue is None or value is None or not math.isfinite(value):
                    flush()
                    continue
                x = left + (transform(xvalue) - lo) / (hi - lo or 1) * (right - left)
                y = bottom - value / ymax * (bottom - top)
                segment.append(f"{x:.2f},{y:.2f}")
                elements.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="{paint(index)}"><title>{esc(item["label"])}: {esc(value)} {esc(unit)}</title></circle>')
            flush()
    return "".join(elements) + "</svg>"
