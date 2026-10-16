"""Hand-calculated fixtures and visible-language checks; no real benchmarks."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from i18n import tr, ref
from presentation import THEMES, curve_color, plot_svg
from report_view import build_report, module_html, document
from result_model import build_module, ModuleResult

CACHE = """# Cache report
## Cache Hierarchy Scan
| Size | RdLat(ns) | WrLat(ns) | BW(MB/s) | Expected |
|---|---|---|---|---|
| 4KB | 1.25 | 2.5 | 12000 | L1 |
| 1MB | 4.5 | 6 | 8000 | L2 |
"""

MODERN_CACHE = """# Cache report
- Samples: 5
- Warmup minimum: 50 ms
- Sample target: 150 ms
## Cache Bandwidth Test
| Size | Operation | Bandwidth (MB/s) | Time(ms) | Bytes | Spread(%) |
|---|---|---|---|---|---|
| 8 KiB | Read | 12000 | 150 | 1800000000 | 2 |
| 8 KiB | Write | 8000 | 150 | 1200000000 | 3 |
| 8 KiB | Copy | 9000 | 150 | 1350000000 | 4 |
| 1 MiB | Read | 10000 | 150 | 1500000000 | 2 |
| 1 MiB | Write | 7000 | 150 | 1050000000 | 3 |
| 1 MiB | Copy | 8000 | 150 | 1200000000 | 4 |
## Cache Latency Test
| Size | RdLat(ns) | Time(ms) | Operations | Spread(%) |
|---|---|---|---|---|
| 8 KiB | 1.5 | 150 | 100000000 | 2 |
| 1 MiB | 5 | 150 | 30000000 | 3 |
## Cache Hierarchy Detection
| Transition | Lower(KiB) | Upper(KiB) | Before(ns) | After(ns) | Ratio |
|---|---|---|---|---|---|
| 1 | 128 | 192 | 1.5 | 5 | 3.333 |
## Reported Cache Hierarchy
| Level | Reported(KiB) |
|---|---|
| L1D | 128 |
| L2 | 4096 |
"""


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "cache_hierarchy_report.md").write_text(CACHE)

    def test_manual_cache_numbers_and_units(self):
        result = build_module("cache_hierarchy", self.root)
        self.assertEqual(result.status, "passed")
        self.assertEqual([k["value"] for k in result.kpis], [1.25, 4.5])
        self.assertEqual(result.charts[0].x, [4, 1024])
        self.assertEqual(result.charts[0].series[0]["values"], [1.25, 4.5])
        self.assertEqual(result.charts[1].series[0]["values"], [12, 8])
        self.assertEqual(result.charts[1].unit, "GB/s")
        self.assertFalse(any("L3" in str(table.rows) for table in result.tables))
        svg = plot_svg(result.charts[0])
        # One 24px legend row; 1.25 / 6 on a 185px axis => y=210.4583.
        self.assertIn('cx="72.00" cy="210.46"', svg)

    def test_heatmap_cell_values_round_half_up_and_keep_raw_precision(self):
        import copy
        import xml.etree.ElementTree as ET
        from result_model import Chart
        values = [[None, 28.49, 28.5], [100.5, None, 191.1], [0.5, 99.99, None]]
        original = copy.deepcopy(values)
        chart = Chart("observed_cas_latency", "heatmap", "ns", [0, 4, 9], [{"values": values}])
        ns = {"s": "http://www.w3.org/2000/svg"}
        for theme in ("dark", "light"):
            for css in (False, True):
                svg = ET.fromstring(plot_svg(chart, theme=theme, css=css))
                labels = svg.findall('s:text[@class="heatmap-value"]', ns)
                self.assertEqual([item.text for item in labels], ["28", "29", "101", "191", "1", "100"])
                cells = [item for item in svg.findall('s:rect', ns) if item.find('s:title', ns) is not None]
                present = [item for item, value in zip(cells, sum(values, [])) if value is not None]
                for label, cell in zip(labels, present):
                    self.assertAlmostEqual(float(label.attrib['x']), float(cell.attrib['x']) + float(cell.attrib['width']) / 2, delta=.01)
                    self.assertLess(float(label.attrib['y']), float(cell.attrib['y']) + float(cell.attrib['height']))
                    rgb = [int(cell.attrib['fill'][i:i+2], 16) / 255 for i in (1, 3, 5)]
                    lum = sum((c/12.92 if c <= .04045 else ((c+.055)/1.055)**2.4)*w for c, w in zip(rgb, (.2126, .7152, .0722)))
                    contrast = (lum + .05) / .05 if label.attrib['fill'] == '#000000' else 1.05 / (lum + .05)
                    self.assertGreaterEqual(contrast, 4.5)
        self.assertEqual(values, original)

    def test_bandwidth_decimal_scaling_all_views_and_cap(self):
        from result_model import bandwidth_scale, split_legacy_result
        for value, expected, unit in ((10000, 10, "GB/s"), (1000, 1, "GB/s"),
                                      (999, 999, "MB/s"), (0.5, 500, "kB/s"),
                                      (0.0005, 500, "B/s"), (10_000_000, 10000, "GB/s")):
            factor, actual_unit = bandwidth_scale([value])
            self.assertEqual(actual_unit, unit)
            self.assertEqual(value * factor, expected)
        content = ("# Memory report\n## Memory Bandwidth Test Results\n"
                   "| Operation | Bandwidth (MB/s) |\n|---|---|\n| Read | 10000 |\n| Write | 500 |\n"
                   "## Memory Latency\n| Operation | ns/op |\n|---|---|\n| Dependent random read | 85 |\n")
        path = self.root / "memory_bandwidth_report.md"
        path.write_text(content)
        model = build_module("memory_bandwidth", self.root)
        model.duration = 123
        self.assertEqual(model.tables[0].columns[1]["unit"], "GB/s")
        self.assertEqual([row[1] for row in model.tables[0].rows], [10, 0.5])
        self.assertEqual(model.charts[0].series[0]["values"], [10, 0.5])
        self.assertEqual([(k["value"], k["unit"]) for k in model.kpis], [(10, "GB/s"), (500, "MB/s"), (85, "ns/op")])
        parts = split_legacy_result(model)
        self.assertEqual([p.name for p in parts], ["memory_bandwidth_test", "memory_latency_test"])
        self.assertEqual([len(p.charts) for p in parts], [1, 1])
        self.assertEqual([p.duration for p in parts], [None, None])
        self.assertEqual(model.duration, 123)
        cache = build_module("cache_hierarchy", self.root)
        cache.duration = 456
        self.assertTrue(all(p.duration is None for p in split_legacy_result(cache)))
        self.assertEqual(path.read_text(), content)
        for language in ("zh", "en"):
            html = module_html(model, language)
            self.assertIn("GB/s", html)
            self.assertIn('<td class="numeric">10</td>', html)
            self.assertNotIn('<td class="numeric">10,000</td>', html)

    def test_three_cache_parts_have_separate_numbers_and_localized_labels(self):
        from result_model import cache_parts
        (self.root / "cache_hierarchy_report.md").write_text(MODERN_CACHE)
        model = build_module("cache_hierarchy", self.root)
        parts = cache_parts(model)
        self.assertEqual([part.name for part in parts], ["cache_bandwidth_test", "cache_latency_test", "cache_detection"])
        self.assertEqual(parts[0].charts[0].series[2]["values"], [9, 8])
        self.assertEqual(parts[1].charts[0].series[0]["values"], [1.5, 5])
        self.assertEqual(parts[2].tables[0].rows[0], [1, 128, 192, 1.5, 5, 3.333])
        self.assertEqual(parts[2].tables[1].rows[0], [ref("l1d"), 128])
        self.assertEqual(parts[0].kpis, [])
        self.assertEqual(len(parts[1].kpis), 2)
        self.assertEqual(parts[2].charts, [])
        for lang in ("zh", "en"):
            html = module_html(model, lang)
            self.assertIn('data-module="cache_hierarchy"', html)
            for part in parts:
                self.assertIn(tr(part.name, lang), html)

    def test_failed_cancelled_unsupported_never_show_stale_data(self):
        for status in ["failed", "interrupted", "unsupported", "timeout", "not_run"]:
            result = build_module("cache_hierarchy", self.root, {"status": status})
            self.assertEqual(result.status, status)
            self.assertEqual(result.kpis, [])
            self.assertEqual(result.tables, [])

    def test_all_four_html_combinations_no_mixed_visible_product_text(self):
        for language in ["en", "zh"]:
            for theme in ["dark", "light"]:
                text = build_report(self.root, ["cache_hierarchy"], language=language, theme=theme)
                self.assertIn(f'data-language="{language}" data-theme="{theme}"', text)
                visible = re.search(r'<main class="language-block" lang="' + language + r'">(.*?)</main>', text, re.S)[1]
                plain = re.sub(r'<[^>]+>', ' ', visible)
                if language == "en":
                    self.assertNotRegex(plain, r'[\u3400-\u9fff]')
                else:
                    self.assertNotRegex(plain, r'Performance|Completed|Read latency|Working set|Language|Dark')
                self.assertNotRegex(text, r'<(?:script|link|img)[^>]+(?:src|href)="https?://')
                self.assertNotIn("Comparability", text)
                self.assertNotIn("可比性", text)

    @unittest.skipUnless(shutil.which("node"), "Node is an optional JS-only verification tool")
    def test_offline_switches_when_localstorage_is_denied(self):
        text = document({"en": "English", "zh": "中文"}, "zh", "dark")
        script = re.search(r'<script>(.*?)</script>', text, re.S)[1]
        harness = '''const assert=require('node:assert/strict');
const elements={language:{options:[{value:'zh'},{value:'en'}]},theme:{options:[{value:'dark'},{value:'light'}]},'language-label':{},'theme-label':{}};
for(const element of [elements.language,elements.theme]) element.addEventListener=(_,f)=>element.change=f;
global.document={documentElement:{dataset:{language:'zh',theme:'dark'}},getElementById:id=>elements[id]};
global.localStorage={getItem:()=>{throw Error('denied')},setItem:()=>{throw Error('denied')}};
'''
        harness += script + '''
assert.equal(elements.language.value,'zh'); elements.language.value='en';elements.language.change();
assert.equal(document.documentElement.lang,'en');assert.equal(elements.language.options[0].textContent,'Chinese');
elements.theme.value='light';elements.theme.change();assert.equal(document.documentElement.dataset.theme,'light');
'''
        subprocess.run(["node", "-e", harness], check=True, capture_output=True, timeout=30)

    def test_palette_text_contrast_and_unique_stable_colors(self):
        def luminance(color):
            values = [int(color[i:i+2], 16)/255 for i in (1, 3, 5)]
            linear = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in values]
            return sum(v*w for v, w in zip(linear, [.2126, .7152, .0722]))
        for theme, palette in THEMES.items():
            self.assertEqual(len({curve_color(i, theme) for i in range(64)}), 64)
            for key in ["text", "muted", "disabled"]:
                a, b = sorted([luminance(palette[key]), luminance(palette["surface"])])
                self.assertGreaterEqual((b+.05)/(a+.05), 4.5, (theme, key))
            for category in ("frontend", "cache", "register", "compute", "memory"):
                for key in ("text", "muted"):
                    a, b = sorted([luminance(palette[key]), luminance(palette[category + "_fill"])])
                    self.assertGreaterEqual((b+.05)/(a+.05), 4.5, (theme, category, key))


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QCheckBox
    from desktop_window import MainWindow
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "Qt desktop test dependency")
class NativePresentationTests(unittest.TestCase):
    def test_line_legend_geometry_and_offset_styles(self):
        import xml.etree.ElementTree as ET
        from PySide6.QtGui import QFont, QFontMetricsF
        from PySide6.QtSvg import QSvgRenderer
        from result_model import Chart
        series = [{"label": f"{stride} B / +{offset} B", "values": [index+1, index+2],
                   "color_index": index, "dashed": bool(offset)}
                  for offset in (0, 32) for index, stride in enumerate((4, 8, 16, 32, 64))]
        chart = Chart("arch_btb_layout", "line", "ns/branch", [16, 4096], series, [16, 4096], "arch_target_count", True)
        ns = {"s": "http://www.w3.org/2000/svg"}
        for language in ("zh", "en"):
            for theme in ("dark", "light"):
                for width in (360, 720, 1100):
                    with self.subTest(language=language, theme=theme, width=width):
                        content = plot_svg(chart, language, theme, width=width)
                        self.assertTrue(QSvgRenderer(content.encode()).isValid())
                        root = ET.fromstring(content)
                        headers = root.findall('s:text[@class="legend-label"]', ns) + root.findall('s:text[@class="chart-unit"]', ns)
                        bounds = []
                        for element in headers:
                            font = QFont("sans-serif"); font.setPixelSize(int(element.get("font-size")))
                            metrics = QFontMetricsF(font)
                            rect = metrics.boundingRect(element.text)
                            rect.translate(float(element.get("x")), float(element.get("y")))
                            self.assertGreaterEqual(rect.left(), 8)
                            self.assertLessEqual(rect.right(), width-8)
                            self.assertTrue(all(not rect.intersects(other) for other in bounds))
                            bounds.append(rect)
                        grid_top = min(float(e.get("y1")) for e in root.findall("s:line", ns) if not e.get("class"))
                        self.assertLess(max(rect.bottom() for rect in bounds), grid_top)
                        self.assertEqual(len(root.findall('s:line[@class="legend-marker"]', ns)), 10)
                        curves = root.findall("s:polyline", ns)
                        self.assertEqual(len(curves), 10)
                        self.assertEqual(sum(bool(c.get("stroke-dasharray")) for c in curves), 5)
                        self.assertEqual([c.get("stroke") for c in curves[:5]], [c.get("stroke") for c in curves[5:]])

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QCoreApplication.setApplicationName("MemoryTestPresentationTest")

    def setUp(self):
        self.window = MainWindow(autodetect=False)
        self.addCleanup(self.window.close)

    def test_native_cache_tabs_preserve_selection_and_split_results(self):
        from benchmark_registry import ROOT
        from result_model import cache_parts
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "cache_hierarchy_report.md").write_text(MODERN_CACHE)
            for model in cache_parts(build_module("cache_hierarchy", path)):
                self.window.pages[model.name].model = model
                self.window.pages[model.name].retranslate()
            tabs = self.window.group_tabs["cache_group"]
            self.assertEqual(tabs.count(), 3)
            self.window.tabs.setCurrentIndex(1)
            self.window.resize(1080, 720)
            self.window.show()
            output = ROOT / "build/ui-revision"
            output.mkdir(parents=True, exist_ok=True)
            for lang in ("zh", "en"):
                self.window.language_combo.setCurrentIndex(self.window.language_combo.findData(lang))
                for theme in ("dark", "light"):
                    self.window.theme_combo.setCurrentIndex(self.window.theme_combo.findData(theme))
                    for index, key in enumerate(("cache_bandwidth_test", "cache_latency_test", "cache_detection")):
                        page = self.window.pages[key]
                        tabs.setCurrentIndex(index)
                        page.retranslate()
                        self.app.processEvents()
                        self.assertEqual(tabs.currentIndex(), index)
                        self.assertEqual(tabs.tabText(index), tr(key, lang))
                        self.window.grab().save(str(output / f"cache-{index}-{lang}-{theme}.png"))
            self.assertEqual(self.window.pages["cache_bandwidth_test"].model.charts[0].unit, "GB/s")
            self.assertEqual(self.window.pages["cache_latency_test"].model.charts[0].unit, "ns")
            self.assertFalse(self.window.pages["cache_detection"].model.charts)

    def test_language_theme_preserve_selection_running_state_and_result(self):
        self.window.modules["cpu_float"].setChecked(False)
        page = self.window.pages["cpu_alu"]
        page.started()
        self.window.benchmark_active = True
        try:
            for lang in ["zh", "en"]:
                for theme in ["light", "dark"]:
                    self.window.language_combo.setCurrentIndex(self.window.language_combo.findData(lang))
                    self.window.theme_combo.setCurrentIndex(self.window.theme_combo.findData(theme))
                    self.window.refresh_controls()
                    self.assertFalse(self.window.modules["cpu_float"].isChecked())
                    self.assertTrue(self.window.benchmark_active)
                    self.assertEqual(page.progress.maximum(), 0)
                    for cls in [QLabel, QPushButton, QCheckBox]:
                        for widget in self.window.findChildren(cls):
                            if lang == "en":
                                self.assertNotRegex(widget.text(), r'[\u3400-\u9fff]')
                    self.assertFalse(self.window.stress_page.start_button.isEnabled())
        finally:
            self.window.benchmark_active = False

    def test_old_stop_timer_cannot_kill_new_session(self):
        page = self.window.stress_page
        page.active, page.generation = True, 2
        try:
            with patch.object(page, "kill_tree") as kill:
                page.force_stop(1)
                kill.assert_not_called()
                page.force_stop(2)
                kill.assert_called_once()
        finally:
            page.active = False

    def test_application_icon_and_native_containers(self):
        from PySide6.QtGui import QImageReader, QIcon
        from benchmark_registry import ROOT
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_icons", ROOT / "packaging/build_icons.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        self.assertFalse(self.window.windowIcon().isNull())
        with tempfile.TemporaryDirectory() as directory:
            root = builder.build_icons(directory)
            self.assertEqual((root / "memorytest.svg").read_bytes(), builder.SOURCE.read_bytes())
            for name in ["memorytest.ico", "memorytest.icns"]:
                reader = QImageReader(str(root / name))
                try:
                    self.assertTrue(reader.canRead(), name)
                    self.assertFalse(reader.read().isNull(), name)
                finally:
                    # Release the native file handle before Windows temp cleanup.
                    del reader
            self.assertEqual([size.width() for size in QIcon(str(root / "memorytest.ico")).availableSizes()],
                             [16, 24, 32, 48, 64, 128, 256])

    def test_plot_window_hide_and_statistics_are_independent(self):
        page = self.window.stress_page
        page.catalog = {"s": {"label": ref("cpu_usage"), "kind": "utilization", "unit": "%"}}
        page.plot.catalog = page.catalog
        page.plot.colors["s"] = 3
        page.plot.visible = {"s"}
        page.stats = {"s": dict(current=30, minimum=10, maximum=30, average=20, count=1000)}
        for elapsed in range(1001):
            page.plot.append(elapsed, {"s": elapsed})
        self.assertEqual(len(page.plot.samples), 601)
        self.assertEqual(page.plot.samples[0][0], 400)
        page.refresh_table()
        from PySide6.QtCore import qInstallMessageHandler
        warnings = []
        previous = qInstallMessageHandler(lambda _type, _context, message: warnings.append(message))
        try:
            page.refresh_table()
            page.refresh_table()
        finally:
            qInstallMessageHandler(previous)
        self.assertEqual(warnings, [])
        page.table.item(0, 0).setCheckState(Qt.Unchecked)
        self.assertFalse(page.plot.visible)
        self.assertEqual(page.stats["s"]["average"], 20)
        self.assertEqual(page.plot.colors["s"], 3)

    def test_monitoring_window_temperature_order_and_all_sensors(self):
        page = self.window.stress_page
        catalog = {"u": {"label": ref("cpu_usage"), "kind": "utilization", "unit": "%"},
                   "f": {"label": ref("cpu_active_frequency"), "kind": "frequency", "unit": "MHz"}}
        catalog.update({f"t{i}": {"label": ref("identified_sensor", name=ref("thermal_zone"), identity=f"T{i}"),
                                  "kind": "temperature", "unit": "°C"} for i in range(30)})
        values = {key: 40 for key in catalog}
        page.sample({"catalog": catalog, "values": values, "elapsed": 0})
        self.assertEqual(page.plot.panel_units(), ["°C", "%", "MHz"])
        self.assertTrue(set(catalog) <= page.plot.visible)
        for elapsed in range(1, 1001):
            page.plot.append(elapsed, values)
        self.assertEqual(page.window_minutes.value(), 5)
        self.assertEqual(page.plot.visible_samples()[0][0], 700)
        page.window_minutes.setValue(1)
        self.assertEqual(page.plot.visible_samples()[0][0], 940)
        page.window_minutes.setValue(10)
        self.assertEqual(page.plot.visible_samples()[0][0], 400)
        page.window_minutes.setValue(999)
        self.assertEqual(page.window_minutes.value(), 10)
        self.assertLessEqual(len(page.plot.samples), 601)

    def test_monitor_columns_controls_and_four_visual_combinations(self):
        from PySide6.QtCore import QPoint, QProcess
        from PySide6.QtWidgets import QHeaderView, QStyle, QStyleOptionSpinBox
        from benchmark_registry import ROOT
        page = self.window.stress_page
        page.caps = {"modes": {"cpu": True}, "threads": 1}
        page.run_dir = Path("/fixture")
        preview = patch.object(page, "start_preview")  # No real sensors in UI fixtures.
        preview.start()
        self.addCleanup(preview.stop)
        page.threads.setValue(1)
        for item in page.modes.values():
            item.setEnabled(True)
        page.sample({"catalog": {"temperature": {"label": ref("identified_sensor", name=ref("p_zone"), identity="Tp01"),
            "kind": "temperature", "unit": "°C"}}, "values": {"temperature": 42}, "elapsed": 1,
            "has_temperature": True,
            "statistics": {"temperature": dict(current=42, minimum=40, maximum=44, average=42)}})
        self.window.resize(880, 720)
        self.window.show()
        self.window.stress_button.click()
        self.app.processEvents()
        self.assertTrue(page.isVisible())
        self.assertEqual(self.window.process.state(), QProcess.NotRunning)
        self.assertEqual(page.table.horizontalHeader().sectionResizeMode(0), QHeaderView.Fixed)
        widths = [page.table.columnWidth(column) for column in range(1, 7)]
        self.assertLessEqual(max(widths) - min(widths), 1)
        for spin in (page.threads, page.memory, page.duration, page.window_minutes):
            option = QStyleOptionSpinBox()
            spin.initStyleOption(option)
            rect = spin.style().subControlRect(QStyle.CC_SpinBox, option, QStyle.SC_SpinBoxUp, spin)
            self.assertGreaterEqual(rect.width(), 28)
            self.assertGreaterEqual(rect.height(), 18)
        page.table.horizontalHeader().resizeSection(1, 207)
        page.refresh_table(); self.window.resize(1120, 850); self.app.processEvents()
        self.assertEqual(page.table.columnWidth(1), 207)
        # Screenshots show the default layout, not the manual-resize fixture.
        page.columns_user_resized = False
        page.columns_initialized = False
        page.equalize_columns()
        evidence = ROOT / "build/ui-revision"
        evidence.mkdir(parents=True, exist_ok=True)
        for language in ("zh", "en"):
            for theme in ("dark", "light"):
                self.window.language_combo.setCurrentIndex(self.window.language_combo.findData(language))
                self.window.theme_combo.setCurrentIndex(self.window.theme_combo.findData(theme))
                self.app.processEvents()
                self.window.grab().save(str(evidence / f"stress-{language}-{theme}.png"))
                for widget in (page.start_button, page.stop_button, page.window_minutes):
                    self.assertTrue(self.window.rect().contains(widget.mapTo(self.window, QPoint(0, 0))))

    def test_hardware_card_at_compact_window_in_all_themes(self):
        from PySide6.QtCore import QPoint
        from benchmark_registry import ROOT
        self.window.hardware_info = {"cpu_model": "Example processor with a very long model name 123456789",
            "physical": 12, "logical": 16, "performance": 8, "efficiency_cores": 4,
            "memory_total": 24.0, "nominal_frequency": "3200 MHz", "memory_rate": "6400 MT/s",
            "gpu": "Example integrated GPU / Example additional discrete graphics device"}
        self.window.resize(880, 720)
        self.window.show()
        self.window.tabs.setCurrentIndex(0)
        evidence = ROOT / "build/ui-revision"
        evidence.mkdir(parents=True, exist_ok=True)
        for language in ("zh", "en"):
            for theme in ("dark", "light"):
                self.window.language_combo.setCurrentIndex(self.window.language_combo.findData(language))
                self.window.theme_combo.setCurrentIndex(self.window.theme_combo.findData(theme))
                self.window.retranslate()
                self.app.processEvents()
                self.window.grab().save(str(evidence / f"home-{language}-{theme}.png"))
                for widget in self.window.hardware_labels.values():
                    self.assertTrue(widget.text())
                    self.assertTrue(self.window.rect().contains(widget.mapTo(self.window, QPoint(widget.width(), widget.height()))))

    def test_standard_startup_uses_fallback_without_any_permission_request(self):
        from PySide6.QtWidgets import QMessageBox
        self.window.administrator = False
        self.window.stress_page.enhanced_enabled = False
        with patch("desktop_window.QMessageBox.question", return_value=QMessageBox.No) as prompt, \
             patch.object(self.window.monitor_access, "start") as start:
            self.window.start_monitor_access()
            start.assert_not_called()
            prompt.assert_not_called()
        self.assertFalse(self.window.stress_page.enhanced_enabled)
        self.assertFalse(self.window.benchmark_active)

    def test_reader_failure_is_not_shown_as_ready(self):
        self.window.monitor_access.status = "error"
        self.window.stress_page.access_status_key = "monitor_standard"
        self.window.monitor_access_changed()
        self.assertEqual(self.window.stress_page.access_status_key, "monitor_failed")
        self.assertFalse(self.window.monitor_state_timer.isActive())

    def test_admin_startup_automatically_starts_once_and_never_exposes_mode_controls(self):
        access, page = self.window.monitor_access, self.window.stress_page
        self.window.administrator = True
        page.enhanced_enabled = True
        with patch.object(access, "available", return_value=True), patch.object(access, "start") as start, \
                patch("desktop_window.QMessageBox.question") as prompt:
            self.window.start_monitor_access()
            self.window.start_monitor_access()
            start.assert_called_once()
            prompt.assert_not_called()
        access.authorized = True
        access.status = "ready"
        self.window.monitor_access_changed()
        self.assertTrue(page.enhanced_enabled)
        self.assertFalse(hasattr(page, "enhanced"))
        self.assertFalse(hasattr(page, "retry_monitor"))
        access.status = "error"
        self.window.monitor_access_changed()
        self.assertTrue(page.enhanced_enabled)
        self.assertFalse(hasattr(page, "enhanced"))
        self.assertFalse(hasattr(page, "retry_monitor"))
        self.assertEqual(page.access_status_key, "monitor_failed")

    def test_normal_monitoring_has_no_engineering_prose_or_empty_notice_rows(self):
        page = self.window.stress_page
        for language in ("zh", "en"):
            self.window.language = language
            for state in ("monitor_standard",):
                page.access_status_key = state
                page.retranslate()
                self.assertEqual(page.access_status.text(), "")
                self.assertTrue(page.access_status.isHidden())
                self.assertTrue(page.warning.isHidden())
            self.assertFalse(hasattr(page, "enhanced"))
        page.modes["cpu"].setEnabled(True)
        page.active = True
        try:
            page.refresh_warning()
            self.assertFalse(page.warning.isHidden())
            page.modes["cpu"].setChecked(False)
            page.refresh_warning()
            self.assertTrue(page.warning.isHidden())
        finally:
            page.active = False


if __name__ == "__main__":
    unittest.main()
