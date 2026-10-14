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
        self.assertEqual(result.charts[1].series[0]["values"], [12000, 8000])
        self.assertEqual(result.charts[1].unit, "MB/s")
        self.assertFalse(any("L3" in str(table.rows) for table in result.tables))
        svg = plot_svg(result.charts[0])
        # 1.25 / maximum 6 on a 185px axis => y=186.4583; x=72 at 4 KiB.
        self.assertIn('cx="72.00" cy="186.46"', svg)

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
        subprocess.run(["node", "-e", harness], check=True, capture_output=True, timeout=10)

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
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QCoreApplication.setApplicationName("MemoryTestPresentationTest")

    def setUp(self):
        self.window = MainWindow()
        self.addCleanup(self.window.close)

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

    def test_selected_pulse_icon_and_native_containers(self):
        from PySide6.QtGui import QImageReader, QIcon
        from benchmark_registry import ROOT
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_icons", ROOT / "packaging/build_icons.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        self.assertEqual((ROOT / "packaging/memorytest.svg").read_bytes(),
                         (ROOT / "packaging/icon-concepts/pulse.svg").read_bytes())
        self.assertFalse(self.window.windowIcon().isNull())
        with tempfile.TemporaryDirectory() as directory:
            root = builder.build_icons(directory)
            for name in ["memorytest.ico", "memorytest.icns"]:
                reader = QImageReader(str(root / name))
                self.assertTrue(reader.canRead(), name)
                self.assertFalse(reader.read().isNull(), name)
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


if __name__ == "__main__":
    unittest.main()
