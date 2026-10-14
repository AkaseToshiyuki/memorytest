#!/usr/bin/env python3
"""Archive one immutable Git commit, retaining the CLI and build/test sources."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app_version import VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release/source")
    args = parser.parse_args()
    sha = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options", args.ref + "^{commit}"], cwd=ROOT, text=True).strip()
    # The archive version comes from the selected commit, not a dirty working tree.
    version_text = subprocess.check_output(["git", "show", f"{sha}:app_version.py"], cwd=ROOT, text=True)
    if f'VERSION = "{VERSION}"' not in version_text:
        raise RuntimeError("Selected commit does not match the release version")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    assets = []
    for kind in ["tar.gz", "zip"]:
        path = args.output_dir / f"MemoryTest-{VERSION}-Source.{kind}"
        subprocess.run(["git", "archive", f"--format={kind}", f"--prefix=memorytest-{VERSION}/", f"--output={path.resolve()}", sha], cwd=ROOT, check=True)
        assets.append({"file": path.name, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (args.output_dir / "source-info.json").write_text(json.dumps({"version": VERSION, "commit": sha, "assets": assets}, indent=2) + "\n")
    print(json.dumps(assets, indent=2))


if __name__ == "__main__":
    main()
