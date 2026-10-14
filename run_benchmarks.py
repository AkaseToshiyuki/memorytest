#!/usr/bin/env python3
"""Run selected benchmark modules (all by default), then write a scoped HTML report.

Each invocation gets a new archive under reports/runs/. Only successful reports
from this invocation are published to its raw report directory. The runner never
reads legacy reports/*.md or automatically invokes sudo / drops system caches.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from benchmark_registry import ROOT, TESTS, CATEGORIES, select_modules, report_filename
from report_html import _parse_all_tables
from process_control import Cancellation, app_command, stop_process_tree


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and greater than zero")
    return seconds


def default_timeout(name):
    cores = os.cpu_count() or 1
    if name == "inter_core":
        return min(2400, 120 + cores * cores / 3)
    return positive_seconds(os.environ.get("CPU_TIMEOUT", str(min(2400, max(300, 300 + (cores - 24) * 4)))))


def run_command(command, cwd, log, timeout, cancelled=None):
    """Capture output without buffering it in RAM; kill descendants on timeout."""
    with log.open("ab") as stream:
        stream.write(("Command: " + " ".join(map(str, command)) + "\n").encode())
        stream.flush()
        with subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                              stdout=stream, stderr=subprocess.STDOUT,
                              start_new_session=os.name != "nt",
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as process:
            try:
                deadline = time.monotonic() + timeout
                while True:
                    if cancelled and cancelled():
                        raise KeyboardInterrupt
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    try:
                        return process.wait(timeout=min(0.2, remaining))
                    except subprocess.TimeoutExpired:
                        pass
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                stop_process_tree(process)
                raise


def validate_report(path):
    """Structural completion check, NOT scientific validation of the values."""
    if not path.is_file():
        return "expected report was not produced"
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("# "):
        return "report has no title"
    # Every module emits a result table; the hardware Item/Value table alone
    # is not a result. This also rejects empty and header-only reports.
    tables = _parse_all_tables(text)
    if not any(rows and headers[0].strip("* ").lower() != "item"
               for headers, rows in tables):
        return "report has no result table"
    return None


def save_manifest(path, manifest):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pending.replace(path)


def environment_snapshot():
    """Read-only provenance. Unknown/unavailable restrictions stay unknown."""
    result = {"os": platform.system(), "architecture": platform.machine(), "kernel": platform.release(),
              "allowed_cpus": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
              "cgroup_v2_limits": {}, "cpu_governor": "unknown"}
    if platform.system() == "Darwin":
        # Public, read-only metadata; no shell, credentials or machine serials.
        result["allowed_cpus"] = None
        result["macos_sysctl"] = {}
        def read_sysctl(key):
            try:
                read = subprocess.run(["/usr/sbin/sysctl", "-n", key], capture_output=True, text=True, timeout=2)
                if read.returncode == 0:
                    result["macos_sysctl"][key] = read.stdout.strip()
                    return read.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
            return None
        for key in ["machdep.cpu.brand_string", "hw.model", "hw.logicalcpu", "hw.physicalcpu",
                    "hw.memsize", "hw.pagesize", "hw.cachelinesize", "hw.nperflevels",
                    "kern.osproductversion", "kern.osversion", "sysctl.proc_translated", "hw.optional.neon"]:
            read_sysctl(key)
        try:
            count = int(result["macos_sysctl"].get("hw.nperflevels", "0"))
        except ValueError:
            count = 0
        result["macos_perflevels"] = []
        for level in range(min(16, max(0, count))):
            fields = {key: read_sysctl(f"hw.perflevel{level}.{key}") for key in
                      ["name", "physicalcpu", "logicalcpu", "l1dcachesize", "l1icachesize", "l2cachesize", "cpusperl2"]}
            result["macos_perflevels"].append({"level": level, **fields})
        result["cpu_placement"] = "OS scheduled; exact CPU affinity unavailable"
        return result
    try:
        membership = Path("/proc/self/cgroup").read_text()
        result["cgroup_membership"] = membership.strip()
        root = Path("/sys/fs/cgroup")
        for line in membership.splitlines():
            if not line.startswith("0::"):
                continue
            current = root / line.split(":", 2)[2].lstrip("/")
            for directory in [current, *current.parents]:
                if directory != root and root not in directory.parents:
                    break
                for filename in ["cpu.max", "cpuset.cpus.effective"]:
                    path = directory / filename
                    try:
                        result["cgroup_v2_limits"][str(path)] = path.read_text().strip()
                    except OSError:
                        pass
    except OSError:
        pass
    if result["allowed_cpus"]:
        cpu = result["allowed_cpus"][0]
        try:
            result["cpu_governor"] = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor").read_text().strip()
        except OSError:
            pass
    return result


def run_suite(modules, output_dir, bin_dir, skip_build=False, timeout=None, skip_charts=False, cpu_threads=None,
              cancelled=None, event=None):
    event = event or (lambda **data: None)
    output_dir = output_dir.resolve()
    bin_dir = bin_dir.resolve()
    runs = output_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=datetime.datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=runs))
    raw_dir = run_dir / "reports"
    raw_dir.mkdir()
    manifest_path = run_dir / "manifest.json"
    manifest = {"schema_version": 1, "run_id": run_dir.name,
                "started_at": datetime.datetime.now().astimezone().isoformat(),
                "environment": environment_snapshot(),
                "selected_modules": modules, "modules": []}
    interrupted = False
    event(type="run", path=str(run_dir), total=len(modules))
    print(f"Run: {run_dir}\nSelected: {', '.join(modules)}", flush=True)
    for name in modules:
        info = TESTS[name]
        work = run_dir / name
        (work / "reports").mkdir(parents=True)
        log = work / "output.log"
        result = {"module": name, "status": "running", "exit_code": None,
                  "log": str(log.relative_to(run_dir)), "message": ""}
        manifest["modules"].append(result)
        save_manifest(manifest_path, manifest)
        started = time.monotonic()
        print(f"[{name}] starting; log: {log}", flush=True)
        event(type="module", module=name, status="running")
        try:
            if interrupted:
                result.update(status="skipped", message="not run after interruption")
                continue
            if cancelled and cancelled():
                raise KeyboardInterrupt
            if not skip_build:
                build = ([sys.executable, str(ROOT / "packaging/build_engines.py"), "--output-dir", str(bin_dir), "--module", name]
                         if os.name == "nt" else ["make", f"BUILD_DIR={bin_dir}", str(bin_dir / info["bin"])])
                code = run_command(build,
                                   ROOT, log, 300, cancelled)
                if code:
                    result.update(status="build_failed", exit_code=code, message="build failed; see log")
                    continue
            binary = bin_dir / info["bin"]
            if os.name == "nt":
                binary = binary.with_suffix(".exe")
            result["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
            command = [str(binary)]
            if name == "cpu_multi" and cpu_threads is not None:
                command.extend(["--threads", str(cpu_threads)])
            result["command"] = command
            code = run_command(command, work, log,
                               timeout if timeout is not None else default_timeout(name), cancelled)
            result["exit_code"] = code
            if code == 77:
                result.update(status="unsupported", message="module unsupported on this platform; no measurements; see log")
                continue
            if code:
                result.update(status="failed", message=f"process exited with code {code}; partial data excluded")
                continue
            source = work / "reports" / info["report"]
            error = validate_report(source)
            if error:
                result.update(status="invalid_report", message=error)
                continue
            shutil.copy2(source, raw_dir / info["report"])
            sidecars = {"inter_core": "inter_core_heatmap_data.json", "cpu_multi": "compute_mark_v2.json"}
            if name in sidecars:
                sidecar = work / "reports" / sidecars[name]
                if sidecar.is_file():
                    shutil.copy2(sidecar, raw_dir / sidecar.name)
            result.update(status="passed", message="completed; result table present")
        except subprocess.TimeoutExpired:
            result.update(status="timeout", message="time limit exceeded; process group stopped")
        except KeyboardInterrupt:
            interrupted = True
            result.update(status="interrupted", message="interrupted by user; partial data excluded")
        except (OSError, ValueError) as exc:
            result.update(status="failed", message=str(exc))
        finally:
            result["duration_seconds"] = round(time.monotonic() - started, 3)
            print(f"[{name}] {result['status']}: {result['message']}", flush=True)
            save_manifest(manifest_path, manifest)
            event(type="module", **result)
    manifest["finished_at"] = datetime.datetime.now().astimezone().isoformat()
    manifest["all_selected_passed"] = all(r["status"] == "passed" for r in manifest["modules"])
    manifest["success"] = (any(r["status"] == "passed" for r in manifest["modules"]) and
                           all(r["status"] in {"passed", "unsupported"} for r in manifest["modules"]))
    archive = run_dir / report_filename(modules)
    manifest["html_report"] = archive.name
    save_manifest(manifest_path, manifest)
    command = (app_command("report") if getattr(sys, "frozen", False) else
               [sys.executable, str(ROOT / "report_html.py")]) + ["--reports-dir", str(raw_dir),
               "--manifest", str(manifest_path), "--output", str(archive)]
    for name in modules:
        command.extend(["--module", name])
    if skip_charts:
        command.append("--skip-charts")
    # A cancellation still produces a report explaining missing/partial data.
    report_code = run_command(command, run_dir, run_dir / "report.log", 120)
    if report_code:
        print(f"HTML generation failed; raw results and status: {run_dir}", file=sys.stderr)
        return 1
    # Latest copy is a convenience only; the unique archive is never overwritten.
    latest = output_dir / archive.name
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".html", delete=False) as stream:
        pending = Path(stream.name)
    try:
        shutil.copyfile(archive, pending)
        pending.replace(latest)
    finally:
        pending.unlink(missing_ok=True)
    print(f"HTML: {latest}\nArchive: {archive}", flush=True)
    event(type="finished", report=str(archive), success=manifest["success"], interrupted=interrupted)
    if interrupted:
        return 130
    if all(r["status"] == "unsupported" for r in manifest["modules"]):
        return 77
    return 0 if manifest["success"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", "-m", action="append", choices=TESTS, help="repeat to select several modules")
    parser.add_argument("--category", choices=CATEGORIES, help="select a group (union with --module)")
    parser.add_argument("--list", action="store_true", help="list modules without building or running")
    parser.add_argument("--system-info", action="store_true", help="print read-only system/topology metadata as JSON; no benchmarks")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--bin-dir", type=Path, default=ROOT / "bin")
    parser.add_argument("--skip-build", action="store_true", help="use existing binaries (e.g. release packages)")
    parser.add_argument("--timeout", type=positive_seconds, help="per-module time limit in seconds")
    parser.add_argument("--skip-charts", action="store_true", help="omit optional matplotlib charts; keep inline SVG/tables")
    parser.add_argument("--cpu-threads", type=int, help="limit ComputeMark threads; default is all allowed logical CPUs")
    parser.add_argument("--cancel-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--events-jsonl", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.cpu_threads is not None and not 1 <= args.cpu_threads <= 262144:
        parser.error("--cpu-threads must be between 1 and 262144")
    if args.system_info:
        print(json.dumps(environment_snapshot(), indent=2, ensure_ascii=False))
        return 0
    if args.list:
        for name, info in TESTS.items():
            print(f"{name:20s} [{info['category']}] {info['description_zh']}")
        return 0
    if "CPU_TIMEOUT" in os.environ:
        try:
            positive_seconds(os.environ["CPU_TIMEOUT"])
        except (ValueError, argparse.ArgumentTypeError) as exc:
            parser.error(f"CPU_TIMEOUT: {exc}")
    def publish(**data):
        print("@memorytest " + json.dumps(data, ensure_ascii=False), flush=True)
    return run_suite(select_modules(args.module, args.category), args.output_dir, args.bin_dir,
                     args.skip_build, args.timeout, args.skip_charts, args.cpu_threads,
                     Cancellation(args.cancel_file, args.parent_pid), publish if args.events_jsonl else None)


if __name__ == "__main__":
    sys.exit(main())
