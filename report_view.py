"""Self-contained bilingual HTML using the desktop result model and SVG renderer."""
import html
import json
from i18n import tr, display, default_language
from presentation import THEMES, curve_color, plot_svg, module_category
from result_model import GROUPS, load_results, system_data, split_legacy_result


def table_html(table, language):
    esc = lambda value: html.escape(display(value, language))
    headers = "".join(f'<th scope="col">{esc(c["label"])}{(" (" + esc(c["unit"]) + ")") if c["unit"] else ""}</th>' for c in table.columns)
    def cell(value):
        kind = ' class="numeric"' if isinstance(value, (int, float)) else ""
        return f"<td{kind}>{esc(value)}</td>"
    rows = "".join("<tr>" + "".join(cell(value) for value in row) + "</tr>" for row in table.rows)
    return f'<h3>{tr(table.title, language)}</h3><div class="table-scroll"><table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></div>'


def module_html(result, language):
    if result.name in {"cache_hierarchy", "memory_bandwidth"} and result.status == "passed":
        return (f'<div id="{language}-{result.name}" data-module="{result.name}">' +
                "".join(module_html(part, language) for part in split_legacy_result(result)) + "</div>")
    esc = lambda value: html.escape(display(value, language))
    bits = [f'<section class="card" id="{language}-{result.name}" data-module="{result.name}" data-category="{module_category(result.name)}">',
            f'<header><h2>{tr(result.name, language)}</h2><span class="badge {result.status}">{tr(result.status, language)}</span></header>']
    if result.duration is not None:
        bits.append(f'<p class="muted">{tr("duration", language)}: {result.duration:.3f} s</p>')
    if result.status != "passed":
        bits.append(f'<p>{tr(result.reason or "no_complete", language)}</p>')
        if result.exit_code is not None:
            bits.append(f'<p>{tr("exit_code", language)}: {result.exit_code}</p>')
    else:
        if result.notice:
            bits.append(f'<p class="muted">{tr(result.notice, language)}</p>')
        if result.kpis:
            bits.append('<div class="kpis">')
            for item in result.kpis:
                unit = tr("points", language) if item["unit"] == "points" else item["unit"]
                category = html.escape(item.get("category", module_category(result.name)), quote=True)
                bits.append(f'<div class="kpi" data-category="{category}"><span>{esc(item["label"])}</span><strong>{esc(item["value"])}</strong><small>{esc(unit)} {esc(item.get("detail", ""))}</small></div>')
            bits.append('</div>')
        if result.conditions:
            bits.append('<dl>' + ''.join(f'<dt>{esc(key)}</dt><dd>{esc(value)}</dd>' for key, value in result.conditions) + '</dl>')
        for chart in result.charts:
            caption = "" if chart.kind == "architecture" else f'<figcaption>{tr(chart.title, language)}</figcaption>'
            bits.append(f'<figure class="{chart.kind}">{caption}{plot_svg(chart, language, css=True)}</figure>')
        for table in result.tables:
            bits.append(table_html(table, language))
    return "".join(bits) + "</section>"


def document(bodies, language=None, theme="dark", title="MemoryTest", preferences_key="memorytest-report"):
    language = language or default_language()
    language = language if language in {"en", "zh"} else "en"
    theme = theme if theme in THEMES else "dark"
    variables = []
    for name, tokens in THEMES.items():
        values = dict(tokens, **{f"curve-{i}": curve_color(i, name) for i in range(64)})
        variables.append(f'html[data-theme="{name}"]' + "{" + ";".join(f"--{key}:{value}" for key, value in values.items()) + "}")
    category_styles = "".join(f'.card[data-category="{key}"],.kpi[data-category="{key}"]' + '{--module-color:var(--' + key + ');--module-fill:var(--' + key + '_fill);}'
                              for key in ("frontend", "cache", "register", "compute", "memory"))
    vocabulary = {lang: {key: tr(key, lang) for key in ["language", "theme", "en", "zh", "dark", "light"]} for lang in ["en", "zh"]}
    # Two language trees ensure table/chart labels all switch together. No remote assets.
    panels = ''.join(f'<main class="language-block" lang="{lang}">{bodies[lang]}</main>' for lang in ["en", "zh"])
    return f'''<!doctype html>
<html lang="{language}" data-language="{language}" data-theme="{theme}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>{''.join(variables)}{category_styles}
*{{box-sizing:border-box}}body{{margin:0;background:var(--background);color:var(--text);font:15px/1.55 system-ui,"Segoe UI","PingFang SC",sans-serif}}
.toolbar,main,footer{{max-width:1240px;margin:auto;padding:20px 28px}}.toolbar{{display:flex;gap:16px;align-items:center;flex-wrap:wrap}}
.toolbar strong{{font-size:22px;margin-right:auto}}label{{display:flex;align-items:center;gap:8px}}select{{font:inherit;padding:7px 12px;border:1px solid var(--border);border-radius:6px;color:var(--text);background:var(--surface)}}
a{{color:var(--accent)}}a:focus,select:focus{{outline:2px solid var(--accent);outline-offset:3px}}h1{{font-size:28px}}h2{{font-size:21px;margin:0}}h3,figcaption{{font-size:16px;font-weight:600}}.muted,small{{color:var(--muted)}}
.card{{--module-color:var(--frontend);--module-fill:var(--frontend_fill);padding:24px;background:var(--surface);border:1px solid var(--border);border-radius:14px;margin-bottom:22px;overflow:hidden}}header{{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:16px}}header h2{{border-left:3px solid var(--module-color);padding-left:12px}}
.badge{{color:var(--muted);font-size:12px;padding:3px 9px;border:1px solid var(--border);border-radius:3px}}.failed,.invalid_report,.timeout{{color:var(--error)}}.passed{{color:var(--success)}}nav{{display:flex;gap:18px;flex-wrap:wrap;margin:16px 0 24px}}.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr));gap:8px;margin:20px 0}}
.kpi{{padding:16px;background:var(--module-fill);border:1px solid var(--module-color);border-radius:3px;min-width:0}}.kpi span,.kpi strong,.kpi small{{display:block}}.kpi span,.kpi small{{font-size:12px;color:var(--muted)}}.kpi strong{{font-size:30px;font-weight:600;margin:7px 0;line-height:1.2;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}}figure{{margin:18px 0;padding:14px 16px;background:var(--base);border:1px solid var(--border);border-top:2px solid var(--module-color);border-radius:5px}}figcaption{{margin-bottom:12px}}svg{{display:block;width:100%;height:auto;max-height:850px}}.table-scroll{{overflow:auto;margin:12px 0;border:1px solid var(--border);border-radius:3px}}
figure.architecture{{padding:0;border:0;background:transparent}}figure.architecture svg{{max-height:none}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{text-align:left;white-space:nowrap;border-bottom:1px solid var(--border);padding:9px 12px}}th{{background:var(--module-fill);font-weight:600;color:var(--muted);font-size:12px}}td.numeric{{text-align:right}}tbody tr:nth-child(even){{background:var(--base)}}tbody tr:hover{{background:var(--button)}}dl{{display:grid;grid-template-columns:minmax(120px,auto) 1fr;gap:5px 20px;font-size:12px;padding:12px 0;border-bottom:1px solid var(--border)}}dt{{color:var(--muted)}}dd{{margin:0;overflow-wrap:anywhere}}
html[data-language="en"] .language-block[lang="zh"],html[data-language="zh"] .language-block[lang="en"]{{display:none}}
@media(max-width:640px){{main,.toolbar{{padding:14px}}.card{{padding:16px}}.kpi strong{{font-size:23px}}}}
@media print{{.toolbar select,.toolbar label{{display:none}}.card{{break-inside:avoid}}}}
</style></head><body><div class="toolbar"><strong>MemoryTest</strong><label><span id="language-label">{tr('language', language)}</span><select id="language"><option value="zh">{tr('zh', language)}</option><option value="en">{tr('en', language)}</option></select></label><label><span id="theme-label">{tr('theme', language)}</span><select id="theme"><option value="dark">{tr('dark', language)}</option><option value="light">{tr('light', language)}</option></select></label></div>
{panels}<footer class="muted">MemoryTest · 0.1.1</footer>
<script>
(()=>{{"use strict";const words={json.dumps(vocabulary, ensure_ascii=True)};const root=document.documentElement;const key={json.dumps(preferences_key)};
const lang=document.getElementById('language'), theme=document.getElementById('theme');
function apply(l,t){{l=words[l]?l:'en';t=t==='light'?'light':'dark';root.lang=l;root.dataset.language=l;root.dataset.theme=t;lang.value=l;theme.value=t;
document.getElementById('language-label').textContent=words[l].language;document.getElementById('theme-label').textContent=words[l].theme;
for(const option of lang.options)option.textContent=words[l][option.value];for(const option of theme.options)option.textContent=words[l][option.value];
try{{localStorage.setItem(key,JSON.stringify({{language:l,theme:t}}))}}catch(e){{}}}}
let settings={{language:root.dataset.language,theme:root.dataset.theme}};try{{const saved=JSON.parse(localStorage.getItem(key)||'null');if(saved&&words[saved.language]&&['light','dark'].includes(saved.theme))settings=saved}}catch(e){{}}
apply(settings.language,settings.theme);lang.addEventListener('change',()=>apply(lang.value,theme.value));theme.addEventListener('change',()=>apply(lang.value,theme.value));
}})();
</script></body></html>'''


def build_report(reports, modules, manifest=None, language=None, theme="dark"):
    results = load_results(reports, modules, manifest)
    system = system_data(reports, modules, manifest)
    bodies = {}
    for lang in ("en", "zh"):
        esc = lambda value: html.escape(display(value, lang))
        bits = [f'<h1>{tr("overview", lang)}</h1>']
        if manifest:
            bits.append(f'<p class="muted">{tr("run_id", lang)}: {esc(manifest.get("run_id", ""))}</p>')
            if not manifest.get("success", False):
                bits.append(f'<p class="failed">{tr("incomplete", lang)}</p>')
        if system:
            bits.append('<dl>' + ''.join(f'<dt>{esc(key)}</dt><dd>{esc(value)}</dd>' for key, value in system) + '</dl>')
        available = {result.name: result for result in results}
        groups = {key: [name for name in names if name in available] for key, names in GROUPS.items()}
        bits.append('<nav>' + ''.join(f'<a href="#{lang}-{group}">{tr(group, lang)}</a>' for group, names in groups.items() if names) + '</nav>')
        for group, names in groups.items():
            if names:
                bits.append(f'<h1 id="{lang}-{group}">{tr(group, lang)}</h1>')
                bits.extend(module_html(available[name], lang) for name in names)
        bodies[lang] = ''.join(bits)
    return document(bodies, language, theme)
