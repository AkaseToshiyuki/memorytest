"""Build the pinned standalone SMART reader; never install a service or driver.

The unmodified corresponding source archive and this build recipe accompany
the binary. Downloads happen only during packaging, never in the application.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import urllib.request

VERSION = "7.5"
ARCHIVE = f"smartmontools-{VERSION}.tar.gz"
SOURCE_URL = f"https://github.com/smartmontools/smartmontools/releases/download/RELEASE_7_5/{ARCHIVE}"
SOURCE_SHA256 = "690b83ca331378da9ea0d9d61008c4b22dde391387b9bbad7f29387f2595f76e"
ROOT = Path(__file__).resolve().parent.parent


def source_archive(cache):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / ARCHIVE
    if not target.exists():
        download = target.with_suffix(".download")
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as response, download.open("wb") as output:
            shutil.copyfileobj(response, output)
        if hashlib.sha256(download.read_bytes()).hexdigest() != SOURCE_SHA256:
            raise RuntimeError("SMART reader source checksum mismatch")
        download.replace(target)
    if hashlib.sha256(target.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("Cached SMART reader source checksum mismatch")
    return target


def build_options(windows, machine, compiler):
    options = ["--with-drivedbdir=no", "--with-update-smart-drivedb=no",
               "--without-libcap-ng", "--without-libsystemd"]
    # The version-matched drive database remains compiled into smartctl. No
    # external database, updater, daemon, installer or device write is used.
    environment = {"CXX": "clang++" if "clang" in compiler else "g++", "CXXFLAGS": "-O2"}
    if windows:
        architecture = "aarch64" if machine.lower() in {"arm64", "aarch64"} else "x86_64"
        options += [f"--host={architecture}-w64-mingw32"]
        resource_compiler = shutil.which("windres") or shutil.which("llvm-windres")
        if resource_compiler:
            environment["WINDRES"] = resource_compiler.replace("\\", "/")
    return options, environment


def build_smartctl(staging, binaries, notices, sources, cache=None):
    archive = source_archive(cache or ROOT / "build/smartctl-cache")
    staging.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as source:
        source.extractall(staging, filter="data")
    folder = staging / f"smartmontools-{VERSION}"
    windows = os.name == "nt"
    options, overrides = build_options(windows, platform.machine(), os.environ.get("CC", "cc"))
    environment = {**os.environ, **overrides}
    configure = ["sh", "./configure", *options]
    target = "smartctl.exe" if windows else "smartctl"
    build = ["make", "-j2", target]
    for command in (configure, build):
        print("+", shlex.join(command), flush=True)
        subprocess.run(command, cwd=folder, env=environment, check=True, timeout=300)
    binary = folder / target
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True, timeout=10)
    if not version.stdout.startswith(f"smartctl {VERSION} "):
        raise RuntimeError("Unexpected SMART reader version")
    binaries.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, binaries / target)
    notices.mkdir(parents=True, exist_ok=True)
    for name in ("COPYING", "AUTHORS", "INSTALL"):
        shutil.copy2(folder / name, notices / (name + ".txt"))
    shutil.copy2(Path(__file__), notices / "MemoryTest-build.py")
    sources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, sources / ARCHIVE)
    record = {"component": "smartmontools / standalone smartctl", "version": VERSION,
              "source_url": SOURCE_URL, "source_sha256": SOURCE_SHA256,
              "source_archive": "third_party_sources/" + ARCHIVE, "source_modified": False,
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "configure": configure, "build": build, "environment": overrides,
              "version_output": version.stdout}
    (notices / "build.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    (notices / "NOTICE.txt").write_text(
        "smartctl is a standalone executable from smartmontools 7.5, GPL-2.0-or-later.\n"
        "It is not linked into MemoryTest. The complete unmodified corresponding source\n"
        "archive is shipped in third_party_sources/smartmontools-7.5.tar.gz.\n"
        "See COPYING.txt, INSTALL.txt, build.json and MemoryTest-build.py for the license,\n"
        "exact build commands and recipe. Extract the archive, run configure and make\n"
        "as recorded in build.json using a native C++ compiler for the target OS.\n"
        "Only smartctl is bundled; no smartd service, database updater or driver.\n",
        encoding="utf-8")
    return record


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_smartctl(args.output / "build", args.output / "bin",
                     args.output / "licenses/smartmontools", args.output / "third_party_sources"), indent=2))
