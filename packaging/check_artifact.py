"""Verify the newest locally built installer bundle without installing as root."""
import json
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import zipfile
import plistlib
import shutil
import secrets
import socket
import time

ROOT = Path(__file__).resolve().parent.parent


def smoke(binary):
    subprocess.run([str(binary), "--smoke-gui"], check=True, timeout=30,
                   env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    subprocess.run([str(binary), "--cli", "--list"], check=True, timeout=15)
    check_native_readers(binary)
    check_smart_sources(binary)


def check_smart_sources(binary):
    roots = [binary.parent / "_internal", binary.parent.parent / "Resources"]
    root = next((path for path in roots if (path / "licenses/smartmontools/build.json").is_file()), None)
    if root is None:
        raise RuntimeError("Packaged SMART reader source/license metadata is missing")
    record = json.loads((root / "licenses/smartmontools/build.json").read_text(encoding="utf-8"))
    archive = root / "third_party_sources/smartmontools-7.5.tar.gz"
    expected = "690b83ca331378da9ea0d9d61008c4b22dde391387b9bbad7f29387f2595f76e"
    if record.get("source_sha256") != expected or not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Packaged SMART reader corresponding source is missing or altered")
    for name in ("COPYING.txt", "AUTHORS.txt", "INSTALL.txt", "NOTICE.txt", "MemoryTest-build.py"):
        if not (root / "licenses/smartmontools" / name).is_file():
            raise RuntimeError("Packaged SMART reader license/build recipe is missing")
    return {"smartmontools_source_sha256": expected, "smartmontools_license": "GPL-2.0-or-later"}


def check_native_readers(binary):
    """Exercise packaged helpers without requesting OS authorization or load."""
    locations = [binary.parent / "_internal/bin", binary.parent.parent / "Frameworks/bin",
                 binary.parent.parent / "Resources/bin"]
    name = "telemetry_privileged.exe" if os.name == "nt" else "telemetry_privileged"
    helper = next((directory / name for directory in locations if (directory / name).is_file()), None)
    if helper is None:
        raise RuntimeError("Packaged administrator reader is missing")
    smartctl = helper.parent / ("smartctl.exe" if os.name == "nt" else "smartctl")
    if not smartctl.is_file():
        raise RuntimeError("Packaged SMART reader is missing")
    version = subprocess.run([str(smartctl), "--version"], capture_output=True, check=True, timeout=10).stdout
    if not version.startswith(b"smartctl 7.5 "):
        raise RuntimeError("Packaged SMART reader has an unexpected version")
    # Parse the exact read flags using the real packaged executable, without
    # opening a device. In particular -jv is NOT equivalent to --json=v.
    command_check = subprocess.run([str(smartctl), "-x", "--json=v", "-n", "standby", "--help"],
                                   capture_output=True, check=True, timeout=10)
    if json.loads(command_check.stdout).get("smartctl", {}).get("exit_status") != 0:
        raise RuntimeError("Packaged SMART reader rejected the read-only argument contract")
    token = secrets.token_hex(32)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); listener.listen(1); listener.settimeout(5)
        process = subprocess.Popen([str(helper), str(listener.getsockname()[1]), token, str(os.getpid()), "--inspect"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            peer, _ = listener.accept()
            peer.settimeout(5)
            with peer, peer.makefile("rb") as stream:
                if stream.readline(100) != ("MTINSPECT/1 " + token + "\n").encode():
                    raise RuntimeError("Packaged administrator reader handshake failed")
                peer.sendall(b"invalid-operation\n")
                if stream.readline(64) != b"-1 0\n":
                    raise RuntimeError("Packaged administrator reader accepted an invalid operation")
                peer.sendall(b"smart-version\n")
                response = stream.readline(64).split()
                if len(response) != 2 or response[0] != b"0" or not 0 < int(response[1]) < 100000:
                    raise RuntimeError("Packaged administrator reader could not launch its SMART reader")
                native_version = stream.read(int(response[1]))
                if native_version.replace(b"\r\n", b"\n") != version.replace(b"\r\n", b"\n"):
                    raise RuntimeError("Administrator reader did not use the bundled SMART reader")
            if process.wait(timeout=5) != 0:
                raise RuntimeError("Packaged administrator reader did not cleanly disconnect")
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
    result = {"inspection_protocol": "passed", "os_elevation_requested": False, "bundled_smartctl": "7.5",
              "inherited_smartctl_launch": "passed", "smartctl_read_arguments": "passed"}
    if sys.platform == "darwin":
        metal = helper.parent / "hardware_macos"
        output = subprocess.run([str(metal)], capture_output=True, text=True, check=True, timeout=10)
        rows = json.loads(output.stdout)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or
                not {"name", "registry_id", "unified", "recommended_working_set"}.issubset(row) for row in rows):
            raise RuntimeError("Packaged Metal reader returned invalid inventory")
        result["metal_inventory"] = "passed"
        result["metal_device_count"] = len(rows)
    return result


def detach_disk_image(mount):
    """Allow transient Finder/indexer handles to close; never force unmount."""
    for attempt in range(5):
        try:
            subprocess.run(["hdiutil", "detach", mount], check=True,
                           capture_output=True, text=True, timeout=15)
            return
        except subprocess.CalledProcessError as error:
            if error.returncode != 16 or attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))


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
                    mounted_app = Path(mounts[0]) / "MemoryTest.app"
                    smoke(mounted_app / "Contents/MacOS/MemoryTest")
                    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(mounted_app)], check=True, timeout=30)
                    # Also validate the copied app and native Cocoa startup.
                    # This does not simulate a browser's quarantine metadata.
                    copied_app = root / "MemoryTest.app"
                    shutil.copytree(mounted_app, copied_app, symlinks=True)
                    cocoa_env = dict(os.environ, QT_QPA_PLATFORM="cocoa")
                    subprocess.run([str(copied_app / "Contents/MacOS/MemoryTest"), "--idle-seconds", "2"],
                                   env=cocoa_env, check=True, timeout=30)
                    subprocess.run(["/usr/bin/open", "-n", "-W", str(copied_app), "--args", "--idle-seconds", "2"],
                                   env=cocoa_env, check=True, timeout=30)
                finally:
                    for mount in mounts:
                        detach_disk_image(mount)
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
    (info.parent / "native-readers-check.json").write_text(
        json.dumps({**check_native_readers(binary), **check_smart_sources(binary)}, indent=2), encoding="utf-8")
    # Bounded, read-only native inventory. No elevation or disk self-tests.
    # Persist capabilities, not disk/module serial numbers, in CI artifacts.
    inspections = []
    for kind in ("cpu", "gpu", "dram", "disk"):
        inspected = subprocess.run([str(binary), "--hardware-tool", "--kind", kind],
                                   capture_output=True, text=True, timeout=35, check=True)
        snapshot = json.loads(inspected.stdout)
        if snapshot.get("schema") != 1 or snapshot.get("kind") != kind or not isinstance(snapshot.get("devices"), list):
            raise RuntimeError(f"Packaged {kind} inspector returned no valid inventory")
        if kind in {"cpu", "dram"} and not snapshot["devices"]:
            raise RuntimeError(f"Packaged {kind} inspector returned no system summary")
        if kind == "disk" and any("smartctl_missing" in dev.get("notices", []) for dev in snapshot["devices"]):
            raise RuntimeError("Packaged disk inspector did not find its bundled SMART reader")
        inspections.append({"schema": snapshot["schema"], "kind": snapshot["kind"],
            "startup_access": snapshot.get("access"),
            "device_count": len(snapshot["devices"]), "sources": snapshot.get("sources", []),
            "availability": [{"fields": sorted({field["name"] for group in dev["groups"]
                              for field in group["fields"] if field.get("value") is not None}),
                              "tables": [table["title"] for table in dev["tables"]]} for dev in snapshot["devices"]],
            "notices": snapshot.get("notices", [])})
    (info.parent / "inspector-check.json").write_text(json.dumps(inspections, indent=2), encoding="utf-8")
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
