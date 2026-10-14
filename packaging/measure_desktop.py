#!/usr/bin/env python3
"""Measure the packaged idle GUI, then exercise only a bounded two-thread run.
Runs offscreen in CI; real desktop/driver overhead is deliberately not claimed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import psutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve()
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    results = {"display_mode": "offscreen", "binary": str(binary)}
    with subprocess.Popen([str(binary), "--idle-seconds", "15"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as process:
        time.sleep(2)
        if process.poll() is not None:
            raise RuntimeError(process.stderr.read().decode(errors="replace"))
        sample = psutil.Process(process.pid)
        results["idle_rss_bytes"] = sample.memory_info().rss
        results["idle_children"] = len(sample.children(recursive=True))
        before = sample.cpu_times()
        start = time.monotonic()
        time.sleep(5)
        after = sample.cpu_times()
        elapsed = time.monotonic() - start
        results["idle_cpu_percent_one_core"] = max(0, 100 * ((after.user - before.user) + (after.system - before.system)) / elapsed)
        results["idle_sample_seconds"] = elapsed
        if process.wait(timeout=15) != 0:
            raise RuntimeError("GUI failed to exit")
    with tempfile.TemporaryDirectory(prefix="memorytest-packaged-run-") as directory:
        root = Path(directory)
        command = [str(binary), "--worker", "--skip-build", "--skip-charts", "--module", "cpu_multi",
                   "--cpu-threads", "2", "--output-dir", str(root)]
        run = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if run.returncode:
            raise RuntimeError(run.stdout + run.stderr)
        manifest = json.loads(next(root.glob("runs/*/manifest.json")).read_text())
        results["packaged_two_thread_status"] = manifest["modules"][0]["status"]
        report = next(root.glob("runs/*/*report.html"))
        results["packaged_report_bytes"] = report.stat().st_size
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
