"""Native SVG architecture evidence map, shared by Qt and offline HTML."""
import html
from i18n import display, tr


def architecture_svg(chart, language, color):
    get = (lambda key: getattr(chart, key)) if not isinstance(chart, dict) else chart.__getitem__
    data = get("series")[0]["evidence"]
    metrics, caches = data["metrics"], data["reported_cache_bytes"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 810" role="img" aria-label="{tr("architecture_diagram", language)}">',
             f'<rect x="1" y="1" width="998" height="808" rx="18" fill="{color("surface")}" stroke="{color("border")}"/>']
    def text(x, y, value, size=15, fill=None, anchor="middle"):
        escaped = html.escape(display(value, language), quote=True)
        parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" font-family="sans-serif" fill="{fill or color("text")}">{escaped}</text>')
    def box(x, y, w, h, title, lines=(), tone="border", dashed=False):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="{color("base")}" stroke="{color(tone)}"{dash}/>')
        text(x+w/2, y+24, title, 15, color(tone) if tone != "border" else None)
        for i, line in enumerate(lines): text(x+w/2, y+48+i*23, line, 14)
    def arrow(x1, y1, x2, y2):
        parts.append(f'<path d="M{x1},{y1} L{x2},{y2} m-4,-6 l4,6 l4,-6" fill="none" stroke="{color("muted")}"/>')
    def metric(name, rate=False):
        item = metrics[name]
        value = f'{item["gops" if rate else "ns"]:.3f}'
        unit = "Ginstr/s" if rate else "ns/op"
        return f'{value} {unit}' + (" *" if not item["stable"] else "")
    def cache(key):
        return f'{caches[key]/1024:g} KiB' if caches[key] else tr("hardware_not_reported", language)
    text(30, 42, tr("architecture_diagram", language), 25, anchor="start")
    text(970, 42, data["isa"], 16, color("muted"), "end")
    scope = tr("arch_qos_scope", language) if data["placement"].startswith("macos") else (
        f'{tr("core", language)} {data["cpu_id"]}' if data["cpu_id"] >= 0 else tr("arch_os_scope", language))
    text(30, 69, scope, 14, color("muted"), "start")
    box(350, 88, 300, 66, tr("arch_instruction_cache", language), [cache("l1i") + " · " + tr("arch_reported", language)], "warning")
    arrow(500, 154, 500, 177)
    box(25, 180, 950, 105, tr("arch_frontend", language), [
        tr("arch_nop_stream", language) + ": " + metric("nop_stream", True),
        tr("arch_decode_unknown", language)], "accent")
    arrow(500, 285, 500, 310)
    box(25, 313, 950, 44, tr("arch_rename_unknown", language), dashed=True)
    for x in (175, 500, 825): arrow(x, 357, x, 390)
    box(25, 393, 300, 215, tr("integer", language), [
        "ADD · " + metric("iadd_dep"), "ADD × 8 · " + metric("iadd_ind8", True),
        "MUL · " + metric("imul_dep"), "MUL × 8 · " + metric("imul_ind8", True),
        tr("arch_ilp_gain", language) + f': {metrics["iadd_dep"]["ns"] / metrics["iadd_ind8"]["ns"]:.2f}×'], "accent")
    box(350, 393, 300, 215, tr("arch_memory_path", language), [
        tr("arch_load32", language), metric("load_chain_32k"),
        tr("write", language) + " 64 B + fence · " + metric("write_ordered_32k"),
        tr("copy", language) + " 64 B + fence · " + metric("copy_ordered_64k"),
        "L1D: " + cache("l1d") + " · " + tr("arch_reported", language)], "success")
    box(675, 393, 300, 215, tr("fp64", language), [
        "FADD · " + metric("fadd_dep"), "FADD × 8 · " + metric("fadd_ind8", True),
        "FMUL · " + metric("fmul_dep"), "FMUL × 8 · " + metric("fmul_ind8", True),
        tr("arch_simd_unknown", language)], "accent")
    for x in (175, 500, 825): arrow(x, 608, x, 635)
    box(25, 638, 950, 66, tr("arch_retire_unknown", language), [tr("arch_not_floorplan", language)], dashed=True)
    text(30, 739, tr("arch_diagram_legend", language), 14, color("muted"), "start")
    text(30, 766, tr("arch_diagram_limit", language), 14, color("muted"), "start")
    text(30, 791, tr("arch_unstable_marker", language), 13, color("warning"), "start")
    return "".join(parts) + "</svg>"
