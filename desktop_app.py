#!/usr/bin/env python3
"""Qt Widgets desktop; benchmark/report helpers never import Qt."""
import sys


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1 and sys.argv[1] in {"--stress-worker", "--gpu-worker", "--sensor-worker"}:
        if sys.argv[1] == "--stress-worker":
            from run_stress import main as worker
        elif sys.argv[1] == "--gpu-worker":
            from stress_gpu import main as worker
        else:
            from telemetry import main as worker
        return worker(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in {"--worker", "--cli"}:
        from run_benchmarks import main as worker
        return worker(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        from report_html import main as report
        sys.argv.pop(1)
        return report() or 0
    from PySide6.QtCore import QCoreApplication, QTimer
    from PySide6.QtWidgets import QApplication
    QCoreApplication.setOrganizationName("MemoryTest")
    QCoreApplication.setApplicationName("MemoryTest")
    app = QApplication(sys.argv)
    from desktop_window import MainWindow
    window = MainWindow()
    window.show()
    if "--smoke-gui" in sys.argv:
        QTimer.singleShot(300, app.quit)
    if "--idle-seconds" in sys.argv:
        seconds = int(sys.argv[sys.argv.index("--idle-seconds") + 1])
        QTimer.singleShot(seconds * 1000, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
