"""Render opt-in icon concepts; never overwrite the application's icon."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent / "icon-concepts"


def main():
    app = QApplication.instance() or QApplication([])
    canvas = QImage(1200, 820, QImage.Format_ARGB32)
    canvas.fill(QColor("#e9eef4"))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QColor("#223044"))
    painter.setFont(QFont("sans-serif", 23, QFont.DemiBold))
    painter.drawText(QRectF(32, 18, 1136, 50), "MemoryTest  /  图标方向")
    painter.setFont(QFont("sans-serif", 12))
    painter.drawText(QRectF(32, 68, 1136, 30), "矢量候选 · 浅底 / 深底 / 16 · 24 · 32 px · 尚未替换应用图标")
    for index, (name, label) in enumerate([("core", "01  芯核"), ("tiers", "02  层阶"), ("paths", "03  通路"), ("pulse", "04  脉冲")]):
        x = 24 + index * 294
        renderer = QSvgRenderer(str(ROOT / (name + ".svg")))
        painter.setPen(QColor("#223044"))
        painter.setFont(QFont("sans-serif", 18, QFont.DemiBold))
        painter.drawText(QRectF(x, 118, 270, 40), Qt.AlignCenter, label)
        for y, background in [(172, "#f7f9fc"), (448, "#111a27")]:
            painter.fillRect(QRectF(x, y, 270, 250), QColor(background))
            renderer.render(painter, QRectF(x + 63, y + 19, 144, 144))
            for small, dx in [(16, 60), (24, 119), (32, 180)]:
                renderer.render(painter, QRectF(x + dx, y + 199 - small/2, small, small))
        painter.setPen(QColor("#526378"))
        painter.setFont(QFont("sans-serif", 11))
        painter.drawText(QRectF(x, 713, 270, 30), Qt.AlignCenter, "16 px       24 px       32 px")
    painter.end()
    canvas.save(str(ROOT / "preview.png"))
    print(ROOT / "preview.png")


if __name__ == "__main__":
    main()
