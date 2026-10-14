# Native onedir bundle. Dynamic Qt libraries remain replaceable (LGPL).
from pathlib import Path
import os
import sys
import subprocess

root = Path(SPECPATH).parent
sys.path.insert(0, str(root))
from app_version import VERSION
engine = Path(os.environ["MEMORYTEST_ENGINE_DIR"])
licenses = Path(os.environ["MEMORYTEST_LICENSE_DIR"])
a = Analysis([str(root / "desktop_app.py")], pathex=[str(root)],
    binaries=[(str(path), "bin") for path in engine.iterdir() if path.is_file()],
    datas=[(str(root / "tests.json"), "."), (str(licenses), "licenses")],
    hiddenimports=[], hookspath=[], runtime_hooks=[],
    excludes=["matplotlib", "numpy", "scipy", "pandas", "IPython", "tkinter",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtWebEngineCore",
              "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PyQt5", "PyQt6"],
    noarchive=False)
# Preserve the distribution's copyright notices for copied Linux dependencies.
# This covers e.g. fontconfig, FreeType, XCB, OpenSSL and GTK pulled by Qt plugins.
if sys.platform.startswith("linux"):
    seen = set()
    for _, source, _ in a.binaries:
        if not source.startswith(("/lib/", "/usr/lib/")):
            continue
        query = subprocess.run(["dpkg-query", "-S", source], capture_output=True, text=True)
        if query.returncode:
            alternate = source[4:] if source.startswith("/usr/lib/") else "/usr" + source
            query = subprocess.run(["dpkg-query", "-S", alternate], capture_output=True, text=True)
        if query.returncode:
            continue
        package = query.stdout.split(": ", 1)[0].split(":", 1)[0]
        copyright_file = Path("/usr/share/doc") / package / "copyright"
        if package not in seen and copyright_file.is_file():
            a.datas.append((f"licenses/system/{package}-copyright", str(copyright_file), "DATA"))
            seen.add(package)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MemoryTest",
          debug=False, strip=False, upx=False, console=True,
          hide_console="hide-early" if sys.platform == "win32" else None,
          codesign_identity=os.environ.get("MEMORYTEST_SIGN_IDENTITY"))
bundle = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="MemoryTest")
if sys.platform == "darwin":
    app = BUNDLE(bundle, name="MemoryTest.app", bundle_identifier="org.memorytest.desktop",
                 info_plist={"CFBundleShortVersionString": VERSION, "NSHighResolutionCapable": True,
                             "LSMinimumSystemVersion": "13.0"})
