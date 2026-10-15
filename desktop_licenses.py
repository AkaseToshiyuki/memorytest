"""Offline, plain-text license reader; no browser or file-manager dependency."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout
from benchmark_registry import ROOT
from i18n import tr


def license_files(root=ROOT):
    root = Path(root)
    directory = root / "licenses"
    if directory.is_dir():
        return sorted((str(path.relative_to(directory)), path) for path in directory.rglob("*")
                      if path.is_file() and not path.is_symlink())
    return [(name, path) for name, path in (("MemoryTest · MIT", root / "LICENSE"),
            ("Stats · MIT", root / "packaging/Stats-MIT.txt")) if path.is_file()]


class LicenseDialog(QDialog):
    def __init__(self, parent, root=ROOT):
        super().__init__(parent)
        self.setWindowTitle(tr("licenses", parent.language))
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(820, 620)
        layout = QVBoxLayout(self)
        self.files = license_files(root)
        self.selector = QComboBox()
        self.selector.setAccessibleName(tr("licenses", parent.language))
        for name, _ in self.files:
            self.selector.addItem(name)
        layout.addWidget(self.selector)
        self.reader = QPlainTextEdit()
        self.reader.setReadOnly(True)
        layout.addWidget(self.reader, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.selector.currentIndexChanged.connect(self.read_license)
        self.read_license(0)

    def read_license(self, index):
        try:
            content = self.files[index][1].read_text(encoding="utf-8", errors="replace")
        except (OSError, IndexError):
            content = tr("read_error", self.parent().language)
        self.reader.setPlainText(content)
        self.reader.moveCursor(self.reader.textCursor().MoveOperation.Start)
