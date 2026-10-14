"""Verify the newest locally built installer bundle without installing as root."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    candidates = list((ROOT / "release/desktop").glob("*/build-info.json"))
    info = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    data = json.loads(info.read_text())
    bundle = Path(data["bundle"])
    binary = bundle / ("Contents/MacOS/MemoryTest" if sys.platform == "darwin" else "MemoryTest.exe" if os.name == "nt" else "MemoryTest")
    prohibited = [str(path) for path in bundle.rglob("*") if "licenses" not in path.parts and any(word in path.name.lower() for word in
                  ["webengine", "chromium", "qtqml", "qtquick", "matplotlib", "numpy"])]
    if prohibited:
        raise RuntimeError(f"Unexpected heavyweight runtime: {prohibited[:10]}")
    # Resource budgets are package guards, not claims about every machine's RSS.
    if data["package_bytes"] > 100 * 1024 * 1024 or data["bundle_bytes"] > 250 * 1024 * 1024:
        raise RuntimeError("Desktop package exceeded 100 MiB compressed / 250 MiB bundle budget")
    subprocess.run([sys.executable, str(ROOT / "packaging/measure_desktop.py"), str(binary),
                    "--output", str(info.parent / "resources.json")], check=True)
    print(f"Artifact checks passed: {info.parent}")


if __name__ == "__main__":
    main()
