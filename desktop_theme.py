"""Small, code-native dark theme; no images, web engine or animation timers."""
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout

BACKGROUND = "#0c111b"
SURFACE = "#131c29"
TEXT = "#e5ecf5"
MUTED = "#a0afc3"
ACCENT = "#a9c9ed"

STYLE = """
QWidget { color: #e5ecf5; font-size: 13px; }
QMainWindow, QWidget#shell { background: #0c111b; }
QLabel { background: transparent; }
QLabel[role="brand"] { font-size: 21px; font-weight: 600; }
QLabel[role="title"] { font-size: 25px; font-weight: 600; }
QLabel[role="section"] { font-size: 15px; font-weight: 600; }
QLabel[role="muted"] { color: #a0afc3; font-size: 12px; }
QLabel[role="version"] { color: #a0afc3; font-size: 11px; }
QLabel[role="status"] { color: #a0afc3; font-size: 12px; padding: 3px 0; }
QLabel[tone="running"] { color: #b5d8ff; }
QLabel[tone="passed"] { color: #a0d4c6; }
QLabel[tone="failed"], QLabel[tone="invalid_report"], QLabel[tone="build_failed"] { color: #f0abb4; }
QLabel[tone="interrupted"], QLabel[tone="timeout"] { color: #dcc394; }
QFrame[role="card"] { background: #131c29; border: 1px solid #263446; border-radius: 12px; }
QFrame[role="moduleRow"] { border: none; border-bottom: 1px solid #263446; border-radius: 0; }
QLabel#resultSummary { font-size: 14px; line-height: 1.5; }
QTabWidget::pane { border: none; border-top: 1px solid #263446; background: #0c111b; }
QTabBar::tab { background: transparent; color: #a0afc3; border: none; border-bottom: 2px solid transparent;
    padding: 13px 15px; margin-right: 4px; }
QTabBar::tab:selected { color: #d4e6fa; border-bottom: 2px solid #a9c9ed; }
QTabBar::tab:hover { background: #182333; color: #e5ecf5; }
QTabBar::tab:focus { border-top: 1px solid #a9c9ed; }
QPushButton { background: #1c2a3b; border: 1px solid #33465e; border-radius: 7px;
    padding: 8px 13px; min-height: 18px; font-weight: 500; }
QPushButton:hover { background: #26394f; border-color: #5c7695; }
QPushButton:pressed { background: #30475f; }
QPushButton:focus { border: 1px solid #b5d8ff; }
QPushButton[role="primary"] { background: #a9c9ed; color: #101c2d; border-color: #a9c9ed; font-weight: 600; }
QPushButton[role="primary"]:hover { background: #c2dcf8; }
QPushButton[role="primary"]:pressed { background: #91b5de; }
QPushButton[role="quiet"] { background: transparent; border-color: transparent; color: #aebfd4; }
QPushButton[role="quiet"]:hover { background: #1c2a3b; border-color: #33465e; }
QPushButton:disabled, QPushButton[role="primary"]:disabled { background: #172130; color: #718298; border-color: #263446; }
QLineEdit, QSpinBox, QPlainTextEdit, QTextBrowser { background: #0e1622; color: #e5ecf5;
    border: 1px solid #2b3b51; border-radius: 7px; padding: 8px; selection-background-color: #355579;
    selection-color: #ffffff; }
QLineEdit, QSpinBox { min-height: 18px; }
QLineEdit:focus, QSpinBox:focus, QTextBrowser:focus, QPlainTextEdit:focus { border-color: #819fc1; }
QLineEdit:disabled, QSpinBox:disabled { color: #718298; border-color: #263446; }
QSpinBox { padding-right: 26px; }
QCheckBox { spacing: 10px; min-height: 20px; }
QCheckBox:disabled { color: #718298; }
QCheckBox::indicator { width: 17px; height: 17px; }
QCheckBox::indicator:unchecked { background: #0e1622; border: 1px solid #526780; border-radius: 4px; }
QCheckBox:focus { color: #b5d8ff; }
QProgressBar { background: #0e1622; border: 1px solid #2b3b51; border-radius: 5px;
    min-height: 18px; max-height: 18px; text-align: center; color: #e5ecf5; font-size: 11px; }
QProgressBar::chunk { background: #426688; border-radius: 4px; }
QScrollBar:vertical { background: #111a27; width: 10px; margin: 0; }
QScrollBar:horizontal { background: #111a27; height: 10px; margin: 0; }
QScrollBar::handle { background: #3b4e66; border-radius: 4px; min-height: 28px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QToolTip { background: #223247; color: #e5ecf5; border: 1px solid #527092; padding: 5px; }
"""


def apply_theme():
    app = QApplication.instance()
    if app.property("memorytestDarkTheme"):
        return
    # Fusion makes palette/contrast predictable, including on light OS desktops.
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in {
        QPalette.Window: BACKGROUND, QPalette.WindowText: TEXT,
        QPalette.Base: "#0e1622", QPalette.AlternateBase: SURFACE,
        QPalette.ToolTipBase: SURFACE, QPalette.ToolTipText: TEXT,
        QPalette.Text: TEXT, QPalette.Button: "#1c2a3b", QPalette.ButtonText: TEXT,
        QPalette.BrightText: "#ffffff", QPalette.Highlight: "#355579",
        QPalette.HighlightedText: "#ffffff", QPalette.Link: ACCENT,
        QPalette.LinkVisited: "#c1b7db", QPalette.PlaceholderText: MUTED,
        QPalette.Light: "#425870", QPalette.Mid: "#2b3b51", QPalette.Dark: BACKGROUND,
    }.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor("#718298"))
    app.setPalette(palette)
    font = QFont()
    font.setFamilies(["Inter", "Segoe UI", "PingFang SC", "Noto Sans CJK SC", "DejaVu Sans"])
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(STYLE)
    app.setProperty("memorytestDarkTheme", True)


def label(text, role):
    widget = QLabel(text)
    widget.setProperty("role", role)
    return widget


def card():
    widget = QFrame()
    widget.setProperty("role", "card")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    return widget, layout


def set_tone(widget, tone):
    widget.setProperty("tone", tone)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
