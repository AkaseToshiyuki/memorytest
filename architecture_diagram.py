"""Native SVG architecture evidence map, shared by Qt and offline HTML."""
import html
import math
from i18n import display, tr
from presentation import _text_width


def architecture_svg(chart, language, color):
    get = (lambda key: getattr(chart, key)) if not isinstance(chart, dict) else chart.__getitem__
    data = get("series")[0]["evidence"]
    caches, frontend = data["reported_cache_bytes"], data.get("frontend")
    resources, competition = data.get("resource_scan"), data.get("competition_scan")
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
        def fitted(value, preferred):
            return min(preferred, max(10, int((w-18)/max(1, _text_width(value, 1)))))
        text(x+w/2, y+24, title, fitted(title, 15))
        for i, line in enumerate(lines): text(x+w/2, y+48+i*23, line, fitted(line, 16 if line[:1].isdigit() else 14))
    def arrow(x1, y1, x2, y2):
        angle = math.atan2(y2-y1, x2-x1)
        left = (x2-7*math.cos(angle-.5), y2-7*math.sin(angle-.5))
        right = (x2-7*math.cos(angle+.5), y2-7*math.sin(angle+.5))
        parts.append(f'<path d="M{x1},{y1} L{x2},{y2} M{left[0]},{left[1]} L{x2},{y2} L{right[0]},{right[1]}" fill="none" stroke="{color("muted")}"/>')
    def cache(key):
        values = sorted({r[key] for r in data.get("cache_instances", []) if r.get(key)})
        if values:
            value = f'{values[0]/1024:g}' if len(values) == 1 else f'{values[0]/1024:g}–{values[-1]/1024:g}'
            return value + " KiB"
        return f'{caches[key]/1024:g} KiB' if caches[key] else tr("hardware_not_reported", language)
    text(30, 42, tr("architecture_diagram", language), 25, anchor="start")
    text(970, 42, data["isa"], 16, color("muted"), "end")
    scope = tr("arch_qos_scope", language) if data["placement"].startswith("macos") else (
        f'{tr("core", language)} {data["cpu_id"]}' if data["cpu_id"] >= 0 else tr("arch_os_scope", language))
    text(30, 69, scope, 14, color("muted"), "start")
    unknown = "— · " + tr("arch_unmeasured", language)
    def resource(family):
        if not resources: return unknown
        value = resources["candidates"].get(family, {"status": resources["status"]})
        if value["status"] == "candidate": return f'{value["lower"]}–{value["upper"]} instr · {tr("arch_candidate", language)}'
        if value.get("observed_interval"):
            low, high = value["observed_interval"]
            return f'{low}–{high} instr · {tr("arch_" + value["status"], language)}'
        return "— · " + tr("arch_" + value["status"], language)
    def width(key):
        value = frontend.get(key) if frontend else None
        if not value:
            if key == "fetch_width" and data.get("width_scan"):
                return "— · " + tr("arch_" + data["width_scan"]["status"], language)
            if frontend and frontend.get("counter_source") == "unavailable":
                return "— · " + tr("arch_counter_"+frontend.get("counter_status", "unavailable"), language)
            return unknown
        sign = "≥" if value["status"] == "lower_bound" else "≈"
        return f'{sign}{value["value"]:.2f} {value["unit"]} · {tr("arch_" + value["status"], language)}'
    box(350, 92, 300, 70, tr("arch_instruction_cache", language), [cache("l1i") + " · " + tr("arch_reported", language)], "cache")
    transitions = frontend["btb_transitions"] if frontend else []
    btb_lines = [tr("arch_btb_candidate", language)] + [f'{r["lower"]}–{r["upper"]}' for r in transitions[:2]] if transitions else [
        tr("arch_no_transition", language) if frontend else unknown]
    if data.get("btb_layout_scan"):
        transitions = data["btb_layout_scan"]["transitions"]
        btb_lines = [tr("arch_layout_transition", language)] + [f'{r["lower"]}–{r["upper"]} @ {r["stride"]} B' for r in transitions[:2]] if transitions else [tr("arch_no_transition", language)]
    box(25, 180, 285, 120, tr("arch_btb", language), btb_lines, "frontend")
    arrow(310, 215, 347, 215)
    arrow(500, 162, 500, 180)
    box(350, 183, 300, 72, tr("arch_fetch", language), [width("fetch_width")], "frontend", dashed=not (frontend and frontend["fetch_width"]))
    arrow(500, 255, 500, 280)
    box(350, 283, 300, 72, tr("arch_decode", language), [width("decode_width")], "frontend", dashed=not (frontend and frontend["decode_width"]))
    arrow(500, 355, 500, 380)
    rename = competition.get("rename_width") if competition else None
    rename_value = f'≥{rename["value"]:.2f} writes/cycle' if rename else unknown
    if competition and not rename:
        rename_value = "— · " + tr("arch_counter_"+competition.get("counter_status", "unavailable") if competition["counter_source"] == "unavailable" else "arch_unstable", language)
    box(350, 383, 300, 72, tr("arch_rename", language), [rename_value], "frontend", dashed=not rename)
    for x, title, family in ((25, "arch_int_window", "prf_i64"), (690, "arch_vec_window", "prf_vec128")):
        box(x, 383, 285, 72, tr(title if resources else "arch_int_registers" if x == 25 else "arch_fp_registers", language),
            [resource(family)], "register", dashed=not resources)
    arrow(310, 419, 347, 419)
    arrow(690, 419, 653, 419)
    for x in (175, 500, 825):
        arrow(500, 455, x, 490)
    for x, title in ((25, "integer"), (350, "arch_memory_path"), (675, "fp64")):
        lines = [tr("arch_scheduler", language) + " · —", tr("arch_ports", language) + " · —"]
        if resources:
            lines[0] = tr("arch_scheduler", language) + " · " + resource("sched_int" if title == "integer" else "sched_vector")
        if competition: lines[1] = tr("arch_ports", language) + " · " + tr("arch_underconstrained", language)
        if title == "arch_memory_path":
            lines = [tr("arch_load_window", language) + " · " + resource("load_ready"), tr("arch_store_window", language) + " · " + resource("store_ready")]
        box(x, 493, 300, 105, tr("arch_vector" if title == "fp64" and resources else title, language), lines, "memory" if title == "arch_memory_path" else "compute", dashed=True)
    box(350, 625, 300, 66, "L1D", [cache("l1d") + " · " + tr("arch_reported", language)], "cache")
    arrow(500, 598, 500, 622)
    for x in (175, 825): arrow(x, 598, x, 720)
    retire = tr("arch_window", language) + " · " + resource("rob_nop")
    if frontend and frontend.get("retirement_width"):
        retire += "     |     " + tr("arch_retire_rate", language) + " " + width("retirement_width")
    retire_lines = [retire]
    if resources and "rob_mixed" in resources["candidates"]:
        retire_lines.append(tr("arch_res_rob_mixed", language) + " · " + resource("rob_mixed"))
    box(25, 723, 950, 88 if len(retire_lines) > 1 else 72, tr("arch_retire", language), retire_lines, "register", dashed=True)
    if not data["stable"]:
        text(30, 825, tr("architecture_unstable", language), 13, color("warning"), "start")
    return "".join(parts) + "</svg>"
