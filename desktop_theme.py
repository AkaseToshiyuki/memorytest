"""Native themes sharing the report/plot color tokens."""
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout, QStyleFactory
from desktop_widgets import AccessibleStyle
from presentation import THEMES

BACKGROUND, TEXT = THEMES["dark"]["background"], THEMES["dark"]["text"]


def apply_theme(theme="dark"):
    app = QApplication.instance()
    if not app.property("memorytestAccessibleStyle"):
        app.setStyle(AccessibleStyle(QStyleFactory.create("Fusion")))
        app.setProperty("memorytestAccessibleStyle", True)
    p = THEMES[theme]
    palette = QPalette()
    roles = {QPalette.Window: "background", QPalette.WindowText: "text", QPalette.Base: "base",
             QPalette.AlternateBase: "surface", QPalette.ToolTipBase: "surface", QPalette.ToolTipText: "text",
             QPalette.Text: "text", QPalette.Button: "button", QPalette.ButtonText: "text",
             QPalette.Highlight: "selection", QPalette.HighlightedText: "text", QPalette.Link: "accent",
             QPalette.PlaceholderText: "muted", QPalette.Light: "surface", QPalette.Mid: "border", QPalette.Dark: "background"}
    for role, key in roles.items():
        palette.setColor(role, QColor(p[key]))
        palette.setColor(QPalette.Disabled, role, QColor(p["disabled"] if key == "text" else p[key]))
    app.setPalette(palette)
    font = QFont()
    font.setFamilies(["Inter", "Segoe UI", "PingFang SC", "Noto Sans CJK SC", "DejaVu Sans"])
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(f"""
QWidget {{ color: {p['text']}; font-size: 13px; }}
QMainWindow, QWidget#shell {{ background: {p['background']}; }}
QLabel {{ background: transparent; }}
QLabel[role="brand"] {{ font-size: 21px; font-weight: 600; }}
QLabel[role="title"] {{ font-size: 24px; font-weight: 600; }}
QLabel[role="section"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="muted"], QLabel[role="status"], QLabel[role="version"] {{ color: {p['muted']}; }}
QLabel[role="version"] {{ font-size: 11px; }}
QLabel[role="metric"] {{ font-size: 23px; font-weight: 600; }}
QLabel[tone="passed"] {{ color: {p['success']}; }}
QLabel[tone="failed"], QLabel[tone="invalid_report"] {{ color: {p['error']}; }}
QFrame[role="card"] {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 10px; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {p['border']}; }}
QTabBar::tab {{ background: transparent; color: {p['muted']}; padding: 12px 13px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {p['text']}; border-bottom: 2px solid {p['accent']}; }}
QPushButton, QComboBox {{ background: {p['button']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 7px 11px; min-height: 18px; }}
QPushButton:hover, QComboBox:hover {{ border-color: {p['accent']}; }}
QPushButton:focus, QComboBox:focus, QSpinBox:focus, QLineEdit:focus {{ border: 2px solid {p['accent']}; }}
QPushButton[role="primary"] {{ background: {p['accent']}; color: {p['background']}; font-weight: 600; }}
QPushButton[role="quiet"] {{ background: transparent; color: {p['muted']}; }}
QPushButton:disabled, QComboBox:disabled, QSpinBox:disabled, QCheckBox:disabled {{ color: {p['disabled']}; background: {p['base']}; }}
QLineEdit, QSpinBox, QPlainTextEdit, QTextBrowser, QTableView {{ background: {p['base']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 5px; padding: 6px; selection-background-color: {p['selection']}; selection-color: {p['text']}; }}
QSpinBox {{ padding-right: 34px; }}
QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: 30px; height: 19px; background: {p['button']}; border-left: 1px solid {p['border']}; }}
QSpinBox::up-button {{ subcontrol-position: top right; border-bottom: 1px solid {p['border']}; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {p['selection']}; }}
QSpinBox::up-arrow, QSpinBox::down-arrow {{ width: 0; height: 0; }}
QPushButton[role="stress-tab"] {{ background: transparent; border: none; border-left: 1px solid {p['border']}; border-radius: 0; padding: 10px 14px; }}
QPushButton[role="stress-tab"]:checked {{ color: {p['accent']}; border-bottom: 2px solid {p['accent']}; }}
QComboBox QAbstractItemView {{ background: {p['surface']}; color: {p['text']}; selection-background-color: {p['selection']}; }}
QHeaderView::section {{ background: {p['button']}; color: {p['text']}; border: none; padding: 8px; }}
QTableView {{ gridline-color: {p['border']}; }}
QCheckBox {{ spacing: 8px; min-height: 22px; }}
QCheckBox::indicator, QTableView::indicator {{ width: 20px; height: 20px; }}
QProgressBar {{ background: {p['base']}; border: 1px solid {p['border']}; border-radius: 4px; text-align: center; min-height: 18px; max-height: 18px; }}
QProgressBar::chunk {{ background: {p['selection']}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: {p['background']}; width: 10px; }}
QScrollBar:horizontal {{ background: {p['background']}; height: 10px; }}
QScrollBar::handle {{ background: {p['border']}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QToolTip {{ background: {p['surface']}; color: {p['text']}; border: 1px solid {p['border']}; padding: 5px; }}
""")
    app.setProperty("memorytestDarkTheme", theme == "dark")


def label(text, role):
    widget = QLabel(text)
    widget.setProperty("role", role)
    return widget


def card():
    widget = QFrame()
    widget.setProperty("role", "card")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    return widget, layout


def set_tone(widget, tone):
    widget.setProperty("tone", tone)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
