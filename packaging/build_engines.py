#!/usr/bin/env python3
"""Build native command-line engines; only a C compiler and Python required."""
import argparse
from pathlib import Path
from build_desktop import ROOT, TESTS, engines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "bin")
    parser.add_argument("--module", action="append", choices=TESTS)
    args = parser.parse_args()
    engines(args.output_dir.resolve(), args.module)


if __name__ == "__main__":
    main()
