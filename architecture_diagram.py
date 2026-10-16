"""Native SVG architecture evidence map, shared by Qt and offline HTML."""
import html
import math
from i18n import display, tr


def architecture_svg(chart, language, color):
    get = (lambda key: getattr(chart, key)) if not isinstance(chart, dict) else chart.__getitem__
    data = get("series")[0]["evidence"]
    caches, frontend = data["reported_cache_bytes"], data.get("frontend")
    height = 850
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 {height}" role="img" aria-label="{tr("architecture_diagram", language)}">',
             f'<rect x="2" y="2" width="996" height="{height-4}" rx="22" fill="{color("surface")}" stroke="{color("border")}" stroke-width="2"/>',
             f'<rect x="20" y="176" width="960" height="289" rx="3" fill="{color("frontend_fill")}" fill-opacity="0.4"/>']
    def text(x, y, value, size=15, fill=None, anchor="middle"):
        escaped = html.escape(display(value, language), quote=True)
        parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" font-family="sans-serif" fill="{fill or color("text")}">{escaped}</text>')
    def box(x, y, w, h, title, lines=(), tone="border", dashed=False):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        fill = color(tone + "_fill") if tone in {"frontend", "cache", "register", "compute", "memory"} else color("base")
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" fill="{fill}" stroke="{color(tone)}"{dash}/>')
        text(x+w/2, y+24, title, 15)
        for i, line in enumerate(lines): text(x+w/2, y+48+i*23, line, 16 if line[:1].isdigit() else 14)
    def arrow(x1, y1, x2, y2):
        angle = math.atan2(y2-y1, x2-x1)
        left = (x2-7*math.cos(angle-.5), y2-7*math.sin(angle-.5))
        right = (x2-7*math.cos(angle+.5), y2-7*math.sin(angle+.5))
        parts.append(f'<path d="M{x1},{y1} L{x2},{y2} M{left[0]},{left[1]} L{x2},{y2} L{right[0]},{right[1]}" fill="none" stroke="{color("muted")}"/>')
    def cache(key):
        return f'{caches[key]/1024:g} KiB' if caches[key] else tr("hardware_not_reported", language)
    text(30, 42, tr("architecture_diagram", language), 25, anchor="start")
    text(970, 42, data["isa"], 16, color("muted"), "end")
    scope = tr("arch_qos_scope", language) if data["placement"].startswith("macos") else (
        f'{tr("core", language)} {data["cpu_id"]}' if data["cpu_id"] >= 0 else tr("arch_os_scope", language))
    text(30, 69, scope, 14, color("muted"), "start")
    unknown = "— · " + tr("arch_unmeasured", language)
    box(350, 92, 300, 70, tr("arch_instruction_cache", language), [cache("l1i") + " · " + tr("arch_reported", language)], "cache")
    transitions = frontend["btb_transitions"] if frontend else []
    btb_lines = [tr("arch_btb_candidate", language)] + [f'{r["lower"]}–{r["upper"]}' for r in transitions[:2]] if transitions else [
        tr("arch_no_transition", language) if frontend else unknown]
    box(25, 180, 285, 120, tr("arch_btb", language), btb_lines, "frontend")
    arrow(310, 215, 347, 215)
    arrow(500, 162, 500, 180)
    box(350, 183, 300, 72, tr("arch_fetch", language), [tr("arch_width", language) + " · " + unknown], "frontend", dashed=True)
    arrow(500, 255, 500, 280)
    box(350, 283, 300, 72, tr("arch_decode", language), [tr("arch_width", language) + " · " + unknown], "frontend", dashed=True)
    arrow(500, 355, 500, 380)
    box(350, 383, 300, 72, tr("arch_rename", language), [tr("arch_width", language) + " · " + unknown], "frontend", dashed=True)
    for x, title in ((25, "arch_int_registers"), (690, "arch_fp_registers")):
        box(x, 383, 285, 72, tr(title, language), [unknown], "register", dashed=True)
    arrow(310, 419, 347, 419)
    arrow(690, 419, 653, 419)
    for x in (175, 500, 825):
        arrow(500, 455, x, 490)
    for x, title in ((25, "integer"), (350, "arch_memory_path"), (675, "fp64")):
        lines = [tr("arch_scheduler", language) + " · —", tr("arch_ports", language) + " · —"]
        if title == "arch_memory_path":
            lines = [tr("arch_load_queue", language) + " · —", tr("arch_store_queue", language) + " · —"]
        box(x, 493, 300, 105, tr(title, language), lines, "memory" if title == "arch_memory_path" else "compute", dashed=True)
    box(350, 625, 300, 66, "L1D", [cache("l1d") + " · " + tr("arch_reported", language)], "cache")
    arrow(500, 598, 500, 622)
    for x in (175, 825): arrow(x, 598, x, 720)
    box(25, 723, 950, 72, tr("arch_retire", language), [tr("arch_capacity", language) + " · " + unknown], "register", dashed=True)
    if not data["stable"]:
        text(30, 825, tr("architecture_unstable", language), 13, color("warning"), "start")
    return "".join(parts) + "</svg>"
