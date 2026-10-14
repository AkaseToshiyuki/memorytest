"""Read-only sensor providers. Unsupported values remain missing, never synthesized."""
from collections import deque
import json
import math
import os
from pathlib import Path
import subprocess
import signal
import sys
import threading
import time

import psutil
from benchmark_registry import ROOT
from i18n import ref
from process_control import Cancellation
from sensor_definitions import smc_label, named_sensor


class Provider:
    def __init__(self, enhanced=False, bin_dir=None, sysfs=Path("/sys"), privileged_samples=None):
        self.enhanced, self.sysfs = enhanced, Path(sysfs)
        self.catalog, self.energy, self.mac_samples = {}, {}, deque(maxlen=1)
        self.mac = None
        self.windows_process = None
        self.windows_attempted = False
        self.windows_samples = deque(maxlen=1)
        self.model = ""
        self.linux_definitions = None
        self.linux_discovered = 0
        self.mac_seen = 0
        self.denied = False
        self.privileged_samples = Path(privileged_samples) if privileged_samples else None
        psutil.cpu_percent(None)
        if sys.platform == "darwin":
            try:
                self.model = subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=2).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
            helper = Path(bin_dir or ROOT / "bin") / "telemetry_macos"
            if helper.is_file():
                self.mac = subprocess.Popen([str(helper), str(os.getpid()), *( ["--enhanced"] if enhanced else [])],
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                def read():
                    for line in self.mac.stdout:
                        try:
                            self.mac_samples.append((time.monotonic(), json.loads(line)))
                        except (ValueError, TypeError):
                            pass
                threading.Thread(target=read, daemon=True).start()

    def put(self, readings, identity, kind, unit, value, label=None):
        if identity not in self.catalog:
            if len(self.catalog) >= 512:
                return
            self.catalog[identity] = {"id": identity, "kind": kind, "unit": unit,
                                      "label": (ref(label) if isinstance(label, str) else label) if label else
                                      ref("unmapped_sensor", kind=ref(kind), identity=identity)}
        readings[identity] = value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None

    def read_number(self, path):
        try:
            return float(path.read_text().strip())
        except PermissionError:
            self.denied = True
        except (OSError, ValueError):
            pass
        return None

    def linux(self, readings):
        critical = False
        specs = [("temp", "temperature", "°C", 1000), ("fan", "fan", "RPM", 1),
                 ("power", "power", "W", 1_000_000), ("in", "voltage", "V", 1000)]
        now = time.monotonic()
        if self.linux_definitions is None or now - self.linux_discovered >= 30:
            self.linux_discovered = now
            self.linux_definitions = []
            def name(path):
                try:
                    return path.read_text().strip()[:120]
                except OSError:
                    return ""
            for device in sorted((self.sysfs / "class/hwmon").glob("hwmon*")):
                device_name = name(device / "name") or device.name
                for prefix, kind, unit, divisor in specs:
                    for path in sorted(device.glob(prefix + "[0-9]*_input")):
                        identity = device_name + "/" + path.name.removesuffix("_input")
                        label = named_sensor(name(path.with_name(path.name.replace("_input", "_label"))), identity, kind)
                        self.linux_definitions.append((path, kind, unit, divisor, label))
        for path, kind, unit, divisor, label in self.linux_definitions:
            value = self.read_number(path)
            identity = "hwmon:" + str(path.resolve())
            self.put(readings, identity, kind, unit, value / divisor if value is not None else None, label)
            if value is not None:
                if kind == "temperature":
                    limit = self.read_number(path.with_name(path.name.replace("_input", "_crit")))
                    critical |= limit is not None and limit > 0 and value >= limit
        for path in sorted((self.sysfs / "class/drm").glob("card*/device/gpu_busy_percent")):
            value = self.read_number(path)
            if value is not None:
                self.put(readings, "gpu:" + str(path.resolve()), "utilization", "%", value, "gpu_usage")
        for policy in sorted((self.sysfs / "devices/system/cpu/cpufreq").glob("policy*")):
            path = policy / "cpuinfo_cur_freq"
            value = self.read_number(path)
            source = "hardware_frequency"
            if value is None:
                value = self.read_number(policy / "scaling_cur_freq")
                source = "os_frequency"
            if value is not None and value > 0:
                try:
                    cpus = (policy / "affected_cpus").read_text().strip()
                except OSError:
                    cpus = policy.name.removeprefix("policy")
                identity = "cpufreq:" + policy.name
                label = ref("policy_frequency", cpus=cpus, source=ref(source))
                self.put(readings, identity, "frequency", "MHz", value / 1000, label)
                self.catalog[identity]["label"] = label
        if self.enhanced:
            now = time.monotonic()
            for path in sorted((self.sysfs / "class/powercap").glob("intel-rapl*/energy_uj")):
                value = self.read_number(path)
                if value is None:
                    continue
                previous = self.energy.get(str(path))
                self.energy[str(path)] = now, value
                if previous and now > previous[0]:
                    delta = value - previous[1]
                    if delta < 0:
                        wrap = self.read_number(path.with_name("max_energy_range_uj"))
                        delta = delta + wrap if wrap is not None else -1
                    if delta >= 0:
                        self.put(readings, "rapl:" + str(path.resolve()), "power", "W", delta / 1e6 / (now - previous[0]))
        return critical

    def start_windows(self):
        if self.windows_attempted:
            return
        self.windows_attempted = True
        # A single persistent reader replaces one PowerShell launch per sample.
        script = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
                  "while (Get-Process -Id " + str(os.getpid()) + " -ErrorAction SilentlyContinue) { "
                  "try { $s=@(Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction Stop | "
                  "Select-Object Identifier,Name,SensorType,Value); @{sensors=$s}|ConvertTo-Json -Depth 4 -Compress } "
                  "catch { @{sensors=@();permission_denied=($_.Exception.HResult -in @(-2147024891,-2147217405))}|ConvertTo-Json -Compress; Start-Sleep -Seconds 4 }; "
                  "Start-Sleep -Seconds 1 }")
        try:
            self.windows_process = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            def read():
                for line in self.windows_process.stdout:
                    try:
                        self.windows_samples.append((time.monotonic(), json.loads(line)))
                    except (ValueError, TypeError):
                        pass
            threading.Thread(target=read, daemon=True).start()
        except PermissionError:
            self.denied = True
        except OSError:
            pass

    def windows(self, readings):
        self.start_windows()
        if not self.windows_samples or time.monotonic() - self.windows_samples[-1][0] > 6:
            return
        try:
            sample = self.windows_samples[-1][1]
            self.denied |= sample.get("permission_denied", False)
            sensors = sample.get("sensors", [])
            from monitor_access import normalize_windows
            for sensor in normalize_windows(sensors):
                self.put(readings, sensor["id"], sensor["kind"], sensor["unit"], sensor["value"], sensor["label"])
        except PermissionError:
            self.denied = True
        except (OSError, ValueError, subprocess.TimeoutExpired, TypeError, KeyError, AttributeError):
            pass

    def sample(self):
        readings = {}
        self.denied = False
        self.put(readings, "cpu:usage", "utilization", "%", psutil.cpu_percent(None), "cpu_usage")
        self.put(readings, "memory:usage", "utilization", "%", psutil.virtual_memory().percent, "memory_usage")
        # psutil.cpu_freq() is nominal rather than live on some platforms.
        # Never publish that static value as a measured frequency.
        critical = self.linux(readings) if sys.platform.startswith("linux") else False
        if sys.platform == "win32":
            self.windows(readings)
        if self.mac_samples:
            timestamp, sample = self.mac_samples[-1]
            if time.monotonic() - timestamp < 3:
                self.denied |= sample.get("permission_denied", False)
                critical |= sample.get("thermal_critical", False)
                for sensor in sample.get("sensors", []):
                    self.put(readings, sensor["id"], sensor["kind"], sensor["unit"], sensor["value"],
                             smc_label(sensor["id"].removeprefix("smc:"), sensor["kind"], self.model))
        if self.privileged_samples:
            try:
                with self.privileged_samples.open(encoding="utf-8") as stream:
                    sample = json.loads(stream.read(131073))
                if 0 <= time.monotonic() - sample.get("sampled_at", 0) < 4:
                    critical |= sample.get("thermal_critical") is True
                    for sensor in sample.get("sensors", [])[:512]:
                        if sensor.get("kind") in {"temperature", "temperature_margin", "frequency", "utilization", "power", "voltage", "fan"}:
                            self.put(readings, sensor["id"], sensor["kind"], sensor["unit"], sensor["value"],
                                     None if sensor["id"] in self.catalog else sensor.get("label"))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
        # Missing/disconnected sensors retain identity, but not their previous reading.
        return {"catalog": self.catalog, "values": {key: readings.get(key) for key in self.catalog},
                "thermal_critical": critical, "permission_denied": self.denied,
                "needs_admin": self.denied or (sys.platform == "darwin" and Path("/usr/bin/powermetrics").is_file()),
                "has_temperature": any(self.catalog[key]["kind"] == "temperature" and value is not None for key, value in readings.items())}

    def close(self):
        if self.windows_process and self.windows_process.poll() is None:
            self.windows_process.terminate()
            self.windows_process.wait(timeout=3)
        if self.mac and self.mac.poll() is None:
            self.mac.terminate()
            try:
                self.mac.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.mac.kill()
                self.mac.wait(timeout=2)


def main(argv=None):
    import argparse
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--enhanced", action="store_true")
    parser.add_argument("--bin-dir", type=Path)
    parser.add_argument("--privileged-samples", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    cancel = Cancellation(parent_pid=args.parent_pid)
    provider = Provider(args.enhanced, args.bin_dir, privileged_samples=args.privileged_samples)
    try:
        if args.once:
            if sys.platform == "win32":
                provider.start_windows()
            # Give asynchronous platform discovery a bounded chance to return.
            deadline = time.monotonic() + 4
            while (provider.mac or provider.windows_process) and not (provider.mac_samples or provider.windows_samples) and time.monotonic() < deadline and not cancel():
                time.sleep(.05)
            print(json.dumps(provider.sample(), ensure_ascii=True, allow_nan=False), flush=True)
            return 0
        while not cancel():
            started = time.monotonic()
            print(json.dumps(provider.sample(), ensure_ascii=True, allow_nan=False), flush=True)
            while time.monotonic() - started < 1 and not cancel():
                time.sleep(0.1)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        provider.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
