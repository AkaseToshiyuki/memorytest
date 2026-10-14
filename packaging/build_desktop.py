#!/usr/bin/env python3
"""Build on the target OS: Linux DEB, macOS DMG, Windows per-user installer.

No publish, upload, root install, benchmark or release signing by default.
All work lives in a unique staging directory, preserving previous artifacts.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from benchmark_registry import TESTS
from compute_score import FLAGS, CLANG_FLAGS
from legal_notices import collect_qt_notices
from app_version import VERSION
import tarfile


def architecture():
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"amd64", "x86_64"}:
        return "x64"
    raise RuntimeError(f"Unsupported desktop package architecture: {machine}")


def run(command, **kwargs):
    print("+", shlex.join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def engines(directory, modules=None):
    directory.mkdir(parents=True, exist_ok=True)
    compiler = shlex.split(os.environ.get("CC", "clang" if sys.platform == "darwin" else "gcc"))
    macros = run(compiler + ["-dM", "-E", "-"], input="", text=True, capture_output=True).stdout
    clang = "__clang__" in macros
    windows = "_WIN32" in macros
    darwin = "__APPLE__" in macros
    if windows != (os.name == "nt") or darwin != (sys.platform == "darwin"):
        raise RuntimeError("Desktop bundling requires a native compiler on the target OS")
    suffix = ".exe" if windows else ""
    source_hash = hashlib.sha256((ROOT / "src/compute_kernel.c").read_bytes()).hexdigest()
    for key, spec in TESTS.items():
        if modules is not None and key not in modules:
            continue
        name = spec["bin"]
        output = directory / (name + suffix)
        if name == "test_cpu_multi":
            flags = CLANG_FLAGS if clang else FLAGS
            args = shlex.split(flags) + ['-DCM_BUILD_PROFILE="portable-scalar-v2"',
                f'-DCM_BUILD_FLAGS="{flags}"', f'-DCM_SOURCE_HASH="{source_hash}"']
            sources = ["test_cpu_multi.c", "compute_kernel.c"]
        else:
            args = ["-O2", "-Wall", "-std=c11", "-pthread", "-Wno-unused-function", "-Wno-unused-result",
                    "-Wno-incompatible-pointer-types-discards-qualifiers" if clang else "-Wno-discarded-qualifiers"]
            sources = [name + ".c", "common.c", "platform.c", "report.c", "simd.c", "latency.c",
                       "util_windows.c" if windows else "util.c",
                       "detect_windows.c" if windows else "detect_macos.c" if darwin else "detect.c",
                       "pmu_macos.c" if windows or darwin else "pmu.c"]
        run(compiler + args + [str(ROOT / "src" / src) for src in sources] +
            ["-o", str(output), "-lm"] + (["-ladvapi32", "-static-libgcc"] if windows else []))
    if modules is None or "cpu_multi" in modules:
        run([directory / ("test_cpu_multi" + suffix), "--self-test"])


def collect_licenses(directory):
    directory.mkdir()
    collect_qt_notices(directory / "Qt-upstream", ROOT / "build/license-cache")
    if os.name == "nt":
        compiler = shutil.which(shlex.split(os.environ.get("CC", "gcc"))[0])
        if compiler:
            notices = Path(compiler).resolve().parent.parent / "share/licenses"
            if notices.is_dir():
                shutil.copytree(notices, directory / "Windows-toolchain", symlinks=False)
    shutil.copy2(ROOT / "LICENSE", directory / "MemoryTest-MIT.txt")
    for name in ["PySide6-Essentials", "shiboken6", "psutil", "pyinstaller"]:
        distribution = importlib.metadata.distribution(name)
        for item in distribution.files or []:
            if "license" in str(item).lower() or "copying" in str(item).lower():
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    destination = directory / name / str(item).replace("..", "_")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    # CPython's license is installed with the interpreter on supported builders.
    for source in [Path(sys.base_prefix) / "LICENSE.txt",
                   Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt"]:
        if source.is_file():
            shutil.copy2(source, directory / "Python-LICENSE.txt")
            break
    (directory / "NOTICE.txt").write_text(
        "MemoryTest uses dynamically linked Qt/PySide6 under LGPLv3 (or the applicable bundled licenses).\n"
        "No Qt modifications. Replace compatible shared libraries in the installed bundle to relink.\n"
        "Qt/PySide sources and build instructions: https://download.qt.io/official_releases/QtForPython/\n"
        "Qt sources: https://download.qt.io/archive/qt/6.10/6.10.2/\n"
        "Python: https://www.python.org/downloads/source/\n"
        "This package has no Chromium, Qt WebEngine, QML UI, telemetry or auto-update daemon.\n", encoding="utf-8")


def deb(bundle, staging, artifacts):
    root = staging / "deb"
    installed = root / "opt/memorytest"
    shutil.copytree(bundle, installed, symlinks=True)
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/memorytest").symlink_to("/opt/memorytest/MemoryTest")
    applications = root / "usr/share/applications"
    applications.mkdir(parents=True)
    (applications / "memorytest.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=MemoryTest\nComment=CPU and memory diagnostics\n"
        "Exec=/opt/memorytest/MemoryTest\nIcon=memorytest\nTerminal=false\nCategories=System;Utility;\n", encoding="utf-8")
    icons = root / "usr/share/icons/hicolor/scalable/apps"
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/memorytest.svg", icons / "memorytest.svg")
    arch = run(["dpkg", "--print-architecture"], capture_output=True, text=True).stdout.strip()
    control = root / "DEBIAN"
    control.mkdir()
    size = sum(path.stat().st_size for path in installed.rglob("*") if path.is_file() and not path.is_symlink()) // 1024
    (control / "control").write_text(
        f"Package: memorytest\nVersion: {VERSION}\nArchitecture: {arch}\nSection: utils\nPriority: optional\n"
        "Maintainer: MemoryTest contributors\n"
        f"Depends: libc6 (>= {platform.libc_ver()[1]}), libstdc++6, libgl1, libegl1, libfontconfig1, libxkbcommon0, "
        "libxkbcommon-x11-0, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-render-util0, libxcb-xinerama0\n"
        f"Installed-Size: {size}\nDescription: Native CPU and memory benchmark desktop\n"
        " Local diagnostic reports and fixed-workload ComputeMark v2 scoring.\n", encoding="utf-8")
    target = artifacts / f"MemoryTest_{VERSION}_{arch}.deb"
    run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", root, target])
    return target


def dmg(bundle, staging, artifacts):
    image = staging / "dmg"
    image.mkdir()
    shutil.copytree(bundle, image / "MemoryTest.app", symlinks=True)
    (image / "Applications").symlink_to("/Applications")
    target = artifacts / f"MemoryTest-{VERSION}-macOS-{platform.machine()}.dmg"
    run(["hdiutil", "create", "-volname", "MemoryTest", "-srcfolder", image, "-format", "UDZO", target])
    return target


def windows(bundle, staging, artifacts):
    target = artifacts / f"MemoryTest-{VERSION}-Windows-{architecture()}-Setup.exe"
    script = staging / "installer.nsi"
    script.write_text('Unicode true\n!include "MUI2.nsh"\nName "MemoryTest"\n'
        f'OutFile "{target}"\nInstallDir "$LOCALAPPDATA\\Programs\\MemoryTest"\n'
        'RequestExecutionLevel user\nSetCompressor /SOLID lzma\n'
        '!insertmacro MUI_PAGE_DIRECTORY\n!insertmacro MUI_PAGE_INSTFILES\n'
        '!insertmacro MUI_UNPAGE_CONFIRM\n!insertmacro MUI_UNPAGE_INSTFILES\n'
        '!insertmacro MUI_LANGUAGE "English"\nSection\nSetOutPath "$INSTDIR"\n'
        f'File /r "{bundle}\\*"\n'
        'WriteUninstaller "$INSTDIR\\Uninstall.exe"\n'
        'CreateDirectory "$SMPROGRAMS\\MemoryTest"\n'
        'CreateShortcut "$SMPROGRAMS\\MemoryTest\\MemoryTest.lnk" "$INSTDIR\\MemoryTest.exe"\n'
        'WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\MemoryTest" "DisplayName" "MemoryTest"\n'
        'WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\MemoryTest" "UninstallString" \'"$INSTDIR\\Uninstall.exe"\'\n'
        'SectionEnd\nSection "Uninstall"\n'
        'Delete "$SMPROGRAMS\\MemoryTest\\MemoryTest.lnk"\nRMDir "$SMPROGRAMS\\MemoryTest"\n'
        'Delete "$INSTDIR\\MemoryTest.exe"\nDelete "$INSTDIR\\Uninstall.exe"\n'
        'RMDir /r "$INSTDIR\\_internal"\nRMDir "$INSTDIR"\n'
        'DeleteRegKey HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\MemoryTest"\n'
        'SectionEnd\n', encoding="utf-8")
    run([os.environ.get("MAKENSIS", "makensis"), script])
    return target


def rpm(staging, artifacts):
    """RPM for modern Fedora/openSUSE; preserve bundled executables unmodified."""
    top = staging / "rpm"
    for name in ["BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"]:
        (top / name).mkdir(parents=True)
    arch = "aarch64" if architecture() == "arm64" else "x86_64"
    spec = top / "SPECS/memorytest.spec"
    payload = staging / "deb"
    spec.write_text(
        f"Name: memorytest\nVersion: {VERSION}\nRelease: 1\nSummary: Native CPU and memory diagnostic desktop\n"
        f"License: MIT AND LGPL-3.0-only\nBuildArch: {arch}\nAutoReqProv: no\n"
        f"Requires: glibc >= {platform.libc_ver()[1]}\nRequires: libGL.so.1()(64bit), libEGL.so.1()(64bit)\n"
        "%global __os_install_post %{nil}\n%global debug_package %{nil}\n%global _build_id_links none\n"
        "%description\nLocal diagnostic reports and fixed-workload ComputeMark v2 scoring.\n"
        "%install\nmkdir -p %{buildroot}\n"
        f"cp -a {shlex.quote(str(payload / 'opt'))} {shlex.quote(str(payload / 'usr'))} %{{buildroot}}/\n"
        "%files\n/opt/memorytest\n/usr/bin/memorytest\n/usr/share/applications/memorytest.desktop\n"
        "/usr/share/icons/hicolor/scalable/apps/memorytest.svg\n", encoding="utf-8")
    run(["rpmbuild", "-bb", "--define", f"_topdir {top}", "--target", arch, spec])
    output = next((top / "RPMS").rglob("*.rpm"))
    target = artifacts / f"MemoryTest-{VERSION}-Linux-{architecture()}.rpm"
    shutil.copy2(output, target)
    return target


def portable(bundle, artifacts):
    name = f"MemoryTest-{VERSION}-{platform.system()}-{architecture()}-Portable"
    if os.name == "nt":
        return Path(shutil.make_archive(str(artifacts / name), "zip", root_dir=bundle.parent, base_dir=bundle.name))
    target = artifacts / (name + ".tar.gz")
    with tarfile.open(target, "w:gz") as archive:
        archive.add(bundle, arcname="MemoryTest")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release/desktop")
    parser.add_argument("--skip-rpm", action="store_true", help="local development only; release CI always builds RPM")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = Path(tempfile.mkdtemp(prefix=platform.system() + "-" + platform.machine() + "-", dir=args.output_dir.resolve()))
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="desktop-", dir=build))
    engines(staging / "engines")
    collect_licenses(staging / "licenses")
    env = dict(os.environ, MEMORYTEST_ENGINE_DIR=str(staging / "engines"), MEMORYTEST_LICENSE_DIR=str(staging / "licenses"))
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", staging / "dist",
         "--workpath", staging / "pyinstaller", ROOT / "packaging/MemoryTest.spec"], cwd=ROOT, env=env)
    bundle = staging / "dist" / ("MemoryTest.app" if sys.platform == "darwin" else "MemoryTest")
    binary = bundle / ("Contents/MacOS/MemoryTest" if sys.platform == "darwin" else "MemoryTest.exe" if os.name == "nt" else "MemoryTest")
    run([binary, "--smoke-gui"], env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=30)
    package = (dmg if sys.platform == "darwin" else windows if os.name == "nt" else deb)(bundle, staging, artifacts)
    packages = [package]
    if sys.platform != "darwin":
        packages.append(portable(bundle, artifacts))
    if sys.platform.startswith("linux") and not args.skip_rpm:
        packages.append(rpm(staging, artifacts))
    manifest = {"version": VERSION, "os": platform.system(), "architecture": architecture(),
                "package": package.name, "package_bytes": package.stat().st_size,
                "bundle_bytes": sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file() and not p.is_symlink()),
                "sha256": hashlib.sha256(package.read_bytes()).hexdigest(), "bundle": str(bundle),
                "signed": bool(os.environ.get("MEMORYTEST_SIGN_IDENTITY")), "notarized": False,
                "libc": platform.libc_ver() if sys.platform.startswith("linux") else None,
                "assets": [{"file": p.name, "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in packages]}
    (artifacts / "build-info.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
