"""Build chosen Pulse SVG into native icon containers; Qt build dependency only."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import shutil
import struct
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

SOURCE = Path(__file__).resolve().parent / "memorytest.svg"


def build_icons(destination):
    app = QApplication.instance() or QApplication([])
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    source = SOURCE.read_bytes()
    images = {}
    for size in [16, 24, 32, 48, 64, 128, 256, 512, 1024]:
        # Optical weight at tiny sizes: retain the chosen geometry, avoid 0.5px strokes.
        svg = source.replace(b'stroke-width="4"', b'stroke-width="7"') if size <= 24 else source
        renderer = QSvgRenderer(svg)
        canvas = QImage(size, size, QImage.Format_ARGB32)
        canvas.fill(Qt.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        if not canvas.save(buffer, "PNG"):
            raise RuntimeError("PNG icon encoder unavailable")
        images[size] = bytes(data)
        (destination / f"memorytest-{size}.png").write_bytes(images[size])
    sizes = [16, 24, 32, 48, 64, 128, 256]
    offset = 6 + 16 * len(sizes)
    directory = []
    for size in sizes:
        payload = images[size]
        directory.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(payload), offset))
        offset += len(payload)
    (destination / "memorytest.ico").write_bytes(struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(directory) + b"".join(images[s] for s in sizes))
    chunks = []
    for kind, size in [(b"icp4", 16), (b"icp5", 32), (b"icp6", 64), (b"ic07", 128),
                       (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024),
                       (b"ic11", 32), (b"ic12", 64), (b"ic13", 256), (b"ic14", 512)]:
        chunks.append(kind + struct.pack(">I", 8 + len(images[size])) + images[size])
    payload = b"".join(chunks)
    (destination / "memorytest.icns").write_bytes(b"icns" + struct.pack(">I", 8 + len(payload)) + payload)
    shutil.copy2(SOURCE, destination / "memorytest.svg")
    return destination


if __name__ == "__main__":
    print(build_icons(SOURCE.parent / "icons"))
