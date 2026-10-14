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


class Provider:
    def __init__(self, enhanced=False, bin_dir=None, sysfs=Path("/sys")):
        self.enhanced, self.sysfs = enhanced, Path(sysfs)
        self.catalog, self.energy, self.mac_samples = {}, {}, deque(maxlen=1)
        self.mac = None
        self.mac_seen = 0
        self.denied = False
        psutil.cpu_percent(None)
        if sys.platform == "darwin":
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
            count = sum(item["kind"] == kind for item in self.catalog.values()) + 1
            self.catalog[identity] = {"id": identity, "kind": kind, "unit": unit,
                                      "label": ref(label) if label else ref("sensor_n", kind=ref(kind), number=count)}
        readings[identity] = value if isinstance(value, (float, int)) and math.isfinite(value) else None

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
        for device in sorted((self.sysfs / "class/hwmon").glob("hwmon*")):
            for prefix, kind, unit, divisor in specs:
                for path in sorted(device.glob(prefix + "[0-9]*_input")):
                    value = self.read_number(path)
                    if value is not None:
                        self.put(readings, "hwmon:" + str(path.resolve()), kind, unit, value / divisor)
                        if kind == "temperature":
                            limit = self.read_number(path.with_name(path.name.replace("_input", "_crit")))
                            critical |= limit is not None and limit > 0 and value >= limit
        for path in sorted((self.sysfs / "class/drm").glob("card*/device/gpu_busy_percent")):
            value = self.read_number(path)
            if value is not None:
                self.put(readings, "gpu:" + str(path.resolve()), "utilization", "%", value, "gpu_usage")
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

    def windows(self, readings):
        if not self.enhanced:
            return
        # Only an already-running local provider. No downloads, services or drivers installed.
        script = ("try { Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction Stop | "
                  "Select-Object Identifier,SensorType,Value | ConvertTo-Json -Compress } "
                  "catch { if ($_.Exception.HResult -eq -2147024891) { exit 5 }; exit 2 }")
        try:
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    capture_output=True, text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.denied = result.returncode == 5
                return
            sensors = json.loads(result.stdout)
            sensors = sensors if isinstance(sensors, list) else [sensors]
            kinds = {"Temperature": ("temperature", "°C"), "Power": ("power", "W"), "Fan": ("fan", "RPM"),
                     "Voltage": ("voltage", "V"), "Clock": ("frequency", "MHz"), "Load": ("utilization", "%")}
            for sensor in sensors:
                if sensor.get("SensorType") in kinds:
                    self.put(readings, "lhm:" + str(sensor["Identifier"]), *kinds[sensor["SensorType"]], sensor.get("Value"))
        except PermissionError:
            self.denied = True
        except (OSError, ValueError, subprocess.TimeoutExpired, TypeError, KeyError):
            pass

    def sample(self):
        readings = {}
        self.denied = False
        self.put(readings, "cpu:usage", "utilization", "%", psutil.cpu_percent(None), "cpu_usage")
        self.put(readings, "memory:usage", "utilization", "%", psutil.virtual_memory().percent, "memory_usage")
        try:
            frequency = psutil.cpu_freq()
            if frequency and frequency.current > 0:
                self.put(readings, "cpu:frequency", "frequency", "MHz", frequency.current, "cpu_frequency")
        except (AttributeError, OSError, NotImplementedError):
            pass
        critical = self.linux(readings) if sys.platform.startswith("linux") else False
        if sys.platform == "win32":
            self.windows(readings)
        if self.mac_samples:
            timestamp, sample = self.mac_samples[-1]
            if time.monotonic() - timestamp < 3:
                self.denied |= sample.get("permission_denied", False)
                critical |= sample.get("thermal_critical", False)
                for sensor in sample.get("sensors", []):
                    self.put(readings, sensor["id"], sensor["kind"], sensor["unit"], sensor["value"])
        # Missing/disconnected sensors retain identity, but not their previous reading.
        return {"catalog": self.catalog, "values": {key: readings.get(key) for key in self.catalog},
                "thermal_critical": critical, "permission_denied": self.denied,
                "has_temperature": any(self.catalog[key]["kind"] == "temperature" and value is not None for key, value in readings.items())}

    def close(self):
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
    args = parser.parse_args(argv)
    cancel = Cancellation(parent_pid=args.parent_pid)
    provider = Provider(args.enhanced, args.bin_dir)
    try:
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
