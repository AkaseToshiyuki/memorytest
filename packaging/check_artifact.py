"""Verify the newest locally built installer bundle without installing as root."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import zipfile
import plistlib
import shutil
import time

ROOT = Path(__file__).resolve().parent.parent


def smoke(binary):
    subprocess.run([str(binary), "--smoke-gui"], check=True, timeout=30,
                   env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    subprocess.run([str(binary), "--cli", "--list"], check=True, timeout=15)


def check_install_payloads(info, data):
    """Test the actual container contents, not only PyInstaller's staging tree."""
    for asset in data["assets"]:
        package = info.parent / asset["file"]
        with tempfile.TemporaryDirectory(prefix="memorytest-installer-") as directory:
            root = Path(directory)
            if package.suffix == ".deb":
                subprocess.run(["dpkg-deb", "--extract", str(package), str(root)], check=True)
                smoke(root / "opt/memorytest/MemoryTest")
            elif package.suffix == ".rpm":
                subprocess.run(["bsdtar", "-xf", str(package), "-C", str(root)], check=True)
                smoke(root / "opt/memorytest/MemoryTest")
            elif package.suffix == ".dmg":
                mounted = subprocess.check_output(["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", str(package)])
                mounts = [entry["mount-point"] for entry in plistlib.loads(mounted)["system-entities"] if "mount-point" in entry]
                try:
                    smoke(Path(mounts[0]) / "MemoryTest.app/Contents/MacOS/MemoryTest")
                finally:
                    for mount in mounts:
                        subprocess.run(["hdiutil", "detach", mount], check=True)
            elif package.suffix == ".zip":
                with zipfile.ZipFile(package) as archive:
                    archive.extractall(root)  # archive just created by this build
                smoke(root / "MemoryTest/MemoryTest.exe")
            elif package.name.endswith(".tar.gz"):
                import tarfile
                with tarfile.open(package) as archive:
                    archive.extractall(root, filter="data")
                smoke(root / "MemoryTest/MemoryTest")
            elif package.suffix == ".exe" and os.environ.get("GITHUB_ACTIONS") == "true":
                # CI-only: temporary per-user install and uninstall. User data is
                # not touched; do not alter a developer's existing installation.
                installed = root / "MemoryTest"
                subprocess.run([str(package), "/S", f"/D={installed}"], check=True, timeout=120)
                try:
                    smoke(installed / "MemoryTest.exe")
                finally:
                    # Match NSIS's documented external-copy + _?= protocol so
                    # its original executable is not locked during removal.
                    # https://nsis.sourceforge.io/Docs/AppendixD.html
                    uninstaller = root / "uninstall-check.exe"
                    shutil.copy2(installed / "Uninstall.exe", uninstaller)
                    subprocess.run([str(uninstaller), "/S", f"_?={installed}"], check=True, timeout=60)
                    if (installed / "MemoryTest.exe").exists() or (installed / "_internal").exists():
                        raise RuntimeError("Uninstaller left the installed application behind")
                    # Windows/ARM64 emulation and AV can briefly retain the
                    # image handle after process exit. Retry only this known
                    # temporary executable, never hide a cleanup failure.
                    for attempt in range(50):
                        try:
                            uninstaller.unlink()
                            break
                        except PermissionError:
                            if attempt == 49:
                                raise
                            time.sleep(0.1)


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
    if any(asset["bytes"] > 100 * 1024 * 1024 for asset in data["assets"]) or data["bundle_bytes"] > 250 * 1024 * 1024:
        raise RuntimeError("Desktop package exceeded 100 MiB compressed / 250 MiB bundle budget")
    check_install_payloads(info, data)
    # Exercise the packaged supervisor/report imports without CPU/GPU load.
    with tempfile.TemporaryDirectory(prefix="memorytest-packaged-monitor-") as directory:
        check = subprocess.run([str(binary), "--stress-worker", "--no-load", "--duration", "1.2",
                                "--threads", "1", "--memory-mib", "1", "--output-dir", directory],
                               capture_output=True, text=True, timeout=30)
        (info.parent / "stress-smoke.log").write_text(check.stdout + check.stderr, encoding="utf-8")
        if check.returncode:
            raise RuntimeError("Packaged monitor failed; see stress-smoke.log")
        manifest = json.loads(next(Path(directory).glob("stress-runs/*/manifest.json")).read_text())
        if manifest["status"] != "stopped" or manifest["reason"] != "duration_stop":
            raise RuntimeError("Packaged monitor did not stop normally")
        report = next(Path(directory).glob("stress-runs/*/stress_report.html"))
        html = report.read_text(encoding="utf-8")
        if 'lang="zh"' not in html or 'lang="en"' not in html or '<svg' not in html:
            raise RuntimeError("Packaged bilingual sensor report missing")
    subprocess.run([sys.executable, str(ROOT / "packaging/measure_desktop.py"), str(binary),
                    "--output", str(info.parent / "resources.json")], check=True)
    print(f"Artifact checks passed: {info.parent}")


if __name__ == "__main__":
    main()
