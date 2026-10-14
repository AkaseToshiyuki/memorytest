"""Supervised stress sessions. Default manual stop; GUI must explicitly start a session."""
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time

from benchmark_registry import ROOT
from process_control import Cancellation, app_command, stop_process_tree
from stress_common import MODES, CPU_MASK, Statistics, allowed_threads, available_memory, memory_limits, validate_modes


def emit(**data):
    print("@memorytest " + json.dumps(data, ensure_ascii=True, allow_nan=False), flush=True)


def write_manifest(path, data):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(data, indent=2, ensure_ascii=True, allow_nan=False), encoding="utf-8")
    pending.replace(path)


def gpu_command(bin_dir, probe=False):
    if sys.platform == "darwin":
        return [str(Path(bin_dir) / "stress_metal"), "--probe" if probe else str(os.getpid())]
    return app_command("gpu-worker") + (["--probe"] if probe else ["--parent-pid", str(os.getpid())])


def capabilities(bin_dir):
    binary = Path(bin_dir) / ("stress_cpu.exe" if os.name == "nt" else "stress_cpu")
    available = available_memory()
    threads = allowed_threads()
    default, maximum = memory_limits(available, threads, True)
    modes = {mode: binary.is_file() for mode in MODES if mode != "gpu"}
    modes["ram"] &= maximum > 0
    gpu = {"available": False, "reason": "device_unavailable"}
    probe = None
    try:
        probe = subprocess.Popen(gpu_command(bin_dir, True), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=os.name != "nt",
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        stdout, _ = probe.communicate(timeout=8)
        if not probe.returncode:
            result = json.loads(stdout)
            if isinstance(result, dict):
                gpu = result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    finally:
        if probe and probe.poll() is None:
            stop_process_tree(probe)
            probe.communicate()
    modes["gpu"] = gpu.get("available") is True
    return {"modes": modes, "gpu": gpu, "threads": threads, "default_memory_mib": default,
            "max_memory_mib": maximum, "available_bytes": available}


class Child:
    def __init__(self, name, command, directory, events):
        self.name = name
        self.log = (directory / (name + ".log")).open("w", encoding="utf-8")
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, bufsize=1,
                                           start_new_session=os.name != "nt",
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError:
            self.log.close()
            raise
        def read():
            try:
                for line in self.process.stdout:
                    self.log.write(line)
                    self.log.flush()
                    try:
                        events.put((name, line[:131072], time.monotonic()), timeout=0.2)
                    except queue.Full:
                        pass
            finally:
                self.log.close()
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def send(self, text):
        self.process.stdin.write(text + "\n")
        self.process.stdin.flush()

    def stop(self, graceful=False):
        if self.process.poll() is None:
            if graceful:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                stop_process_tree(self.process)
        self.reader.join(timeout=1)
        self.process.stdout.close()
        if not self.process.stdin.closed:
            self.process.stdin.close()


def run_session(output, bin_dir, modes, threads, memory_mib, duration=0, enhanced=False,
                control=None, parent_pid=None, language=None, theme="dark", event=emit,
                capability_data=None, child_factory=Child, provider_command=None):
    caps = capability_data or capabilities(bin_dir)
    modes = validate_modes(modes, caps["modes"])
    if not 1 <= threads <= caps["threads"] or memory_mib < 1:
        raise ValueError("invalid resource settings")
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("invalid duration")
    runs = Path(output).resolve() / "stress-runs"
    runs.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=datetime.datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=runs))
    manifest = {"schema_version": 1, "kind": "stress", "run_id": directory.name, "status": "running",
                "started_at": datetime.datetime.now().astimezone().isoformat(), "threads": threads,
                "memory_mib": memory_mib, "duration_limit_seconds": duration, "enhanced_monitoring": enhanced,
                "modes": modes, "catalog": {}, "statistics": {}, "events": []}
    write_manifest(directory / "manifest.json", manifest)
    event(type="stress_run", path=str(directory))
    events = queue.Queue(maxsize=128)
    children = {}
    stats = Statistics()
    cancel = Cancellation(parent_pid=parent_pid)
    started = time.monotonic()
    next_sample, last_control, gpu_heartbeat = started, None, started
    latest, latest_at = None, 0
    catalog = {}
    reason, failed = "user_stop", False
    applied_modes = []

    def record_event(key, selected=None):
        entry = {"elapsed": time.monotonic() - started, "event": key}
        if selected is not None:
            entry["modes"] = selected
        manifest["events"].append(entry)
        event(type="stress_event", **entry)

    def change(selected):
        nonlocal modes, gpu_heartbeat, applied_modes
        selected = validate_modes(selected, caps["modes"])
        # A hot switch must not count our already allocated RAM against itself.
        owned_ram = memory_mib * 1024 * 1024 if "ram" in applied_modes else 0
        _, maximum = memory_limits(available_memory() + owned_ram, threads, "gpu" in selected)
        if "ram" in selected and memory_mib > maximum:
            raise RuntimeError("memory_error")
        children["cpu"].send(str(sum(CPU_MASK.get(mode, 0) for mode in selected)))
        if "gpu" in selected and "gpu" not in children:
            children["gpu"] = child_factory("gpu", gpu_command(bin_dir), directory, events)
            gpu_heartbeat = time.monotonic()
        elif "gpu" not in selected and "gpu" in children:
            children.pop("gpu").stop()
        modes = selected
        applied_modes = list(selected)
        manifest["modes"] = modes
        record_event("mode_change", modes)

    try:
        binary = Path(bin_dir) / ("stress_cpu.exe" if os.name == "nt" else "stress_cpu")
        children["cpu"] = child_factory("cpu", [str(binary), str(threads), str(memory_mib)], directory, events)
        command = provider_command or (app_command("sensor-worker") + ["--parent-pid", str(os.getpid()), "--bin-dir", str(bin_dir)] + (["--enhanced"] if enhanced else []))
        children["sensors"] = child_factory("sensors", command, directory, events)
        change(modes)
        with (directory / "samples.jsonl").open("w", encoding="utf-8") as samples:
            while True:
                now = time.monotonic()
                if cancel():
                    reason = "parent_exit"
                    break
                if duration and now - started >= duration:
                    reason = "duration_stop"
                    break
                if control and Path(control).is_file():
                    # Consecutive writes can share a timestamp on some filesystems.
                    # Compare the small atomic request itself, not its mtime.
                    with Path(control).open(encoding="utf-8") as request:
                        command_text = request.read(65537)
                    if len(command_text) > 65536:
                        raise ValueError("oversized control request")
                    if command_text != last_control:
                        # UI writes atomically; a malformed request fails the session closed.
                        command = json.loads(command_text)
                        if not isinstance(command, dict):
                            raise ValueError("invalid control request")
                        last_control = command_text
                        if command.get("stop"):
                            reason = "user_stop"
                            break
                        if "modes" in command and command["modes"] != modes:
                            change(command["modes"])
                while True:
                    try:
                        name, line, timestamp = events.get_nowait()
                    except queue.Empty:
                        break
                    if name == "sensors":
                        try:
                            sample = json.loads(line)
                            if not isinstance(sample.get("catalog"), dict) or not isinstance(sample.get("values"), dict):
                                continue
                            latest, latest_at = sample, timestamp
                            catalog.update(sample["catalog"])
                            if sample.get("thermal_critical"):
                                raise RuntimeError("thermal_stop")
                        except json.JSONDecodeError:
                            continue
                    elif line.startswith("error "):
                        key = line[6:].strip()
                        raise RuntimeError(key if key in {"calculation_error", "memory_error"} else "backend_error")
                    elif name == "gpu" and line.strip() == "ready":
                        gpu_heartbeat = timestamp
                if any(child.process.poll() is not None for child in children.values()):
                    raise RuntimeError("backend_error")
                if "gpu" in children and now - gpu_heartbeat > 15:
                    raise RuntimeError("backend_error")
                if now >= next_sample:
                    fresh = latest is not None and now - latest_at < 3
                    values = {key: latest["values"].get(key) if fresh else None for key in catalog}
                    stats.sample(values)
                    sample = {"elapsed": now - started, "values": values}
                    samples.write(json.dumps(sample, allow_nan=False) + "\n")
                    samples.flush()
                    manifest["catalog"], manifest["statistics"] = catalog, stats.rows
                    manifest["elapsed"] = sample["elapsed"]
                    write_manifest(directory / "manifest.json", manifest)
                    event(type="stress_sample", **sample, catalog=catalog, statistics=stats.rows,
                          has_temperature=bool(fresh and latest.get("has_temperature")),
                          permission_denied=bool(fresh and latest.get("permission_denied")), modes=modes)
                    next_sample = now + 1
                time.sleep(0.05)
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            reason = "user_stop"
        else:
            reason = str(exc) if str(exc) in {"memory_error", "thermal_stop", "calculation_error"} else "backend_error"
            failed = True
    finally:
        for name, child in list(children.items()):
            try:
                child.stop(graceful=name == "cpu")
            except (OSError, subprocess.TimeoutExpired):
                failed, reason = True, "backend_error"
                try:
                    stop_process_tree(child.process)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        manifest.update(status="failed" if failed else "stopped", reason=reason,
                        elapsed=time.monotonic() - started, catalog=catalog, statistics=stats.rows,
                        finished_at=datetime.datetime.now().astimezone().isoformat())
        record_event(reason)
        write_manifest(directory / "manifest.json", manifest)
        from stress_report import build_stress_report
        report = directory / "stress_report.html"
        try:
            build_stress_report(directory, report, language, theme)
        except (OSError, ValueError):
            report = None
        event(type="stress_finished", path=str(directory), report=str(report) if report else None,
              status=manifest["status"], reason=reason)
    return 1 if failed else 0


def main(argv=None):
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--mode", action="append", choices=MODES)
    parser.add_argument("--no-load", action="store_true", help="start a monitoring-only session")
    parser.add_argument("--threads", type=int)
    parser.add_argument("--memory-mib", type=int)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--enhanced", action="store_true")
    parser.add_argument("--control", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--bin-dir", type=Path, default=ROOT / "bin")
    parser.add_argument("--language", choices=("en", "zh"))
    parser.add_argument("--theme", choices=("light", "dark"), default="dark")
    args = parser.parse_args(argv)
    caps = capabilities(args.bin_dir)
    if args.probe:
        emit(type="stress_capabilities", **caps)
        return 0
    try:
        return run_session(args.output_dir, args.bin_dir, [] if args.no_load else args.mode or ["cpu"], args.threads or caps["threads"],
                           args.memory_mib if args.memory_mib is not None else max(1, caps["default_memory_mib"]),
                           args.duration, args.enhanced, args.control, args.parent_pid, args.language, args.theme,
                           capability_data=caps)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
