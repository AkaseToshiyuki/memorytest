"""Collect version-matched upstream license/attribution files, not source code.

Source archives are cached under build/ and never extracted or executed. The
bundled notice includes upstream source URLs and archive digests for inspection.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import urllib.request

QT_VERSION = "6.10.2"
ARCHIVES = {
    name: f"https://download.qt.io/archive/qt/6.10/{QT_VERSION}/submodules/{name}-everywhere-src-{QT_VERSION}.tar.xz"
    for name in ["qtbase", "qtsvg", "qtimageformats", "qtwayland"]
}
ARCHIVES["pyside"] = f"https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{QT_VERSION}-src/pyside-setup-everywhere-src-{QT_VERSION}.tar.xz"


def collect_qt_notices(directory, cache):
    cache.mkdir(parents=True, exist_ok=True)
    directory.mkdir(parents=True, exist_ok=True)
    def collect(item):
        name, url = item
        target = cache / url.rsplit("/", 1)[1]
        if not target.exists():
            temporary = target.with_suffix(target.suffix + ".download")
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            temporary.replace(target)
        count = 0
        with tarfile.open(target) as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not member.isfile() or member.size > 2_000_000:
                    continue
                filename = path.name.lower()
                if not (filename.startswith(("license", "copying", "copyright", "notice")) or
                        "LICENSES" in path.parts or filename == "qt_attribution.json"):
                    continue
                output = directory / name / Path(*path.parts[1:])
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, output.open("wb") as stream:
                    shutil.copyfileobj(source, stream)
                count += 1
        return {"component": name, "version": QT_VERSION, "source_url": url,
                "archive_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "notice_files": count}
    with ThreadPoolExecutor(max_workers=3) as executor:
        records = list(executor.map(collect, ARCHIVES.items()))
    # The Linux Qt wheel uses ICU 73; license text accompanies its data/library.
    icu_url = "https://raw.githubusercontent.com/unicode-org/icu/release-73-2/icu4c/LICENSE"
    icu = cache / "ICU-73.2-LICENSE.txt"
    if not icu.exists():
        with urllib.request.urlopen(icu_url, timeout=60) as response:
            icu.write_bytes(response.read())
    shutil.copy2(icu, directory / icu.name)
    records.append({"component": "ICU", "source_url": "https://github.com/unicode-org/icu/tree/release-73-2",
                    "notice_url": icu_url, "notice_sha256": hashlib.sha256(icu.read_bytes()).hexdigest()})
    (directory / "sources.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
