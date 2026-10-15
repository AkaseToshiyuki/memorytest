"""OS-authorized, session-only access to a fixed native read-only sensor helper.

The GUI, Python workers and benchmarks always retain the user's identity. No
password is read by this program. IPC carries sensor output and three fixed
start/pause/quit messages, never commands, paths or code to the elevated side.
"""
from collections import deque
import ctypes
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time

from benchmark_registry import ROOT
from i18n import ref
from sensor_definitions import named_sensor


def parse_powermetrics(text):
    """Keep the tool's actual tier/cluster labels; never assume a core count.

    CPU HW-active frequency is an interval average excluding idle time, not a
    nominal clock nor an instantaneous per-core frequency. Unknown output is
    missing, not filled with a model-specific constant or inferred multiplier.
    """
    sensors = []
    for match in re.finditer(r"^\s*([A-Za-z][A-Za-z0-9_-]*-Cluster) HW active frequency:\s*([0-9.]+)\s*(MHz|GHz)\s*$", text, re.M):
        cluster, value, unit = match.groups()
        kind = "e_frequency" if cluster.startswith("E") else "p_frequency" if cluster.startswith("P") else "cluster_frequency"
        sensors.append({"id": "powermetrics:" + cluster, "kind": "frequency", "unit": "MHz",
            "value": float(value) * (1000 if unit == "GHz" else 1), "label": ref(kind, cluster=cluster)})
    for match in re.finditer(r"^\s*GPU (?:HW )?active frequency:\s*([0-9.]+)\s*(MHz|GHz)\s*$", text, re.M):
        sensors.append({"id": "powermetrics:gpu_frequency", "kind": "frequency", "unit": "MHz",
            "value": float(match[1]) * (1000 if match[2] == "GHz" else 1), "label": ref("gpu_frequency")})
    for match in re.finditer(r"^\s*CPU Average frequency as fraction of nominal:.*\(([0-9.]+)\s*(MHz|GHz)\)\s*$", text, re.M | re.I):
        sensors.append({"id": "powermetrics:cpu_active", "kind": "frequency", "unit": "MHz",
            "value": float(match[1]) * (1000 if match[2].lower() == "ghz" else 1), "label": ref("cpu_active_frequency")})
    for match in re.finditer(r"^\s*(CPU|GPU|ANE) Power:\s*([0-9.]+)\s*(mW|W)\s*$", text, re.M):
        sensors.append({"id": "powermetrics:" + match[1].lower() + "_power", "kind": "power", "unit": "W",
            "value": float(match[2]) / (1000 if match[3] == "mW" else 1),
            "label": ref("identified_sensor", name=ref("power"), identity=match[1])})
    # Intel output varies by OS release. Only accept explicitly reported MHz/GHz.
    for match in re.finditer(r"^\s*(?:CPU |Core )?(\d+) (?:HW active |average )?frequency:\s*([0-9.]+)\s*(MHz|GHz)\s*$", text, re.M | re.I):
        sensors.append({"id": "powermetrics:cpu" + match[1], "kind": "frequency", "unit": "MHz",
            "value": float(match[2]) * (1000 if match[3].lower() == "ghz" else 1),
            "label": ref("active_core_frequency", number=int(match[1]))})
    return sensors


def normalize_windows(sensors):
    kinds = {"Temperature": ("temperature", "°C"), "Power": ("power", "W"), "Fan": ("fan", "RPM"),
             "Voltage": ("voltage", "V"), "Clock": ("frequency", "MHz"), "Load": ("utilization", "%")}
    result = []
    for sensor in sensors[:512] if isinstance(sensors, list) else []:
        if isinstance(sensor, dict) and sensor.get("SensorType") in kinds and isinstance(sensor.get("Identifier"), str):
            kind, unit = kinds[sensor["SensorType"]]
            if kind == "temperature" and "Distance to TjMax" in str(sensor.get("Name", "")):
                kind, unit = "temperature_margin", "Δ°C"
            identity = sensor["Identifier"][:256]
            result.append({"id": "lhm:" + identity, "kind": kind, "unit": unit, "value": sensor.get("Value"),
                           "label": named_sensor(sensor.get("Name"), identity, kind)})
    return result


def authorization_command(helper, port, token, parent_pid, platform=None, inspection=False):
    platform = platform or sys.platform
    arguments = [str(Path(helper).resolve()), str(port), token, str(parent_pid)]
    if inspection:
        arguments.append("--inspect")
    if platform == "darwin":
        return ["/usr/bin/osascript", "-e", "on run argv", "-e",
                "do shell script (item 1 of argv) with administrator privileges", "-e", "end run", shlex.join(arguments)]
    if platform.startswith("linux"):
        return ["/usr/bin/pkexec", *arguments]
    return arguments


class WindowsLaunch:
    def __init__(self, arguments):
        from ctypes import wintypes
        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
        shell.ShellExecuteExW.restype = wintypes.BOOL
        request = SHELLEXECUTEINFOW()
        request.cbSize = ctypes.sizeof(request)
        request.fMask = 0x40 | 0x100  # NOCLOSEPROCESS | NOASYNC
        request.lpVerb = "runas"
        request.lpFile = arguments[0]
        request.lpParameters = subprocess.list2cmdline(arguments[1:])
        request.nShow = 0
        if not shell.ShellExecuteExW(ctypes.byref(request)):
            raise PermissionError("OS authorization was cancelled or unavailable")
        self.handle = request.hProcess

    def poll(self):
        value = ctypes.c_ulong()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(value)):
            return 1
        return None if value.value == 259 else value.value

    def release(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class MonitorAccess:
    def __init__(self, helper=None):
        self.helper = Path(helper or ROOT / "bin" / ("telemetry_privileged.exe" if os.name == "nt" else "telemetry_privileged"))
        self.directory = tempfile.TemporaryDirectory(prefix="memorytest-monitor-")
        self.samples_path = Path(self.directory.name) / "sample.json"
        self.status = "not_requested"
        self.authorized = False
        self.active = False
        self.process = self.connection = self.server = self.thread = None
        self.stop_event = threading.Event()
        self.energy = {}
        self.last_error = ""
        self.generation = 0

    def available(self):
        return self.helper.is_file() and (not sys.platform.startswith("linux") or Path("/usr/bin/pkexec").is_file())

    def start(self, launcher=None):
        if self.status in ("authorizing", "ready"):
            return
        if not self.helper.is_file():
            self.status = "unavailable"
            return
        self.stop_event.clear()
        self.status = "authorizing"
        self.thread = threading.Thread(target=self._run, args=(launcher,), daemon=True)
        self.thread.start()

    def set_active(self, active):
        if self.active == bool(active):
            return
        self.active = bool(active)
        self.generation += 1
        if self.connection is not None and self.status == "ready":
            try:
                self.connection.sendall(b"1" if self.active else b"0")
            except OSError:
                self.status = "error"
        if not active:
            self.energy.clear()
            self.publish([])

    def publish(self, sensors, thermal_critical=False):
        payload = {"sampled_at": time.monotonic(), "sensors": sensors[:512], "thermal_critical": thermal_critical is True}
        try:
            pending = self.samples_path.with_suffix(".tmp")
            # Called from the reader except pause. Lock serializes atomic replacement.
            with _publish_lock:
                pending.write_text(json.dumps(payload, ensure_ascii=True, allow_nan=False), encoding="utf-8")
                pending.replace(self.samples_path)
        except (OSError, ValueError):
            pass

    def _run(self, launcher):
        token = secrets.token_hex(32)
        listener = None
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server = listener
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            listener.settimeout(.25)
            port = listener.getsockname()[1]
            args = authorization_command(self.helper, port, token, os.getpid())
            if launcher:
                self.process = launcher(args)
            elif os.name == "nt":
                self.process = WindowsLaunch(args)
            else:
                self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 120
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                if self.process.poll() is not None:
                    self.status = "denied"
                    return
                try:
                    peer, _ = listener.accept()
                except socket.timeout:
                    continue
                peer.settimeout(2)
                greeting = bytearray()
                try:
                    while not greeting.endswith(b"\n") and len(greeting) < 128:
                        part = peer.recv(1)
                        if not part:
                            break
                        greeting.extend(part)
                except OSError:
                    peer.close()
                    continue
                if not hmac.compare_digest(bytes(greeting), ("MTMON/1 " + token + "\n").encode()):
                    peer.close()
                    continue
                self.connection = peer
                self.authorized = True
                self.status = "ready"
                peer.sendall(b"1" if self.active else b"0")
                self._read(peer)
                return
            if not self.stop_event.is_set():
                self.status = "denied"
        except PermissionError:
            self.status = "denied"
        except (OSError, ValueError, TypeError):
            if not self.stop_event.is_set():
                self.status = "error"
        finally:
            if self.connection:
                self.connection.close()
                self.connection = None
            if listener is not None:
                listener.close()
            self.server = None
            if isinstance(self.process, WindowsLaunch):
                self.process.release()
            if self.status == "ready":
                self.status = "stopped" if self.stop_event.is_set() else "error"

    def _read(self, peer):
        pending, block = b"", []
        generation = self.generation
        peer.settimeout(.25)
        while not self.stop_event.is_set():
            try:
                part = peer.recv(8192)
            except socket.timeout:
                continue
            if not part:
                return
            if generation != self.generation:
                generation = self.generation
                pending, block = b"", []
            pending += part
            if len(pending) > 131072:
                self.status = "error"
                return
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip()
                if line in ("ready", ""):
                    continue
                if line == "backend_error":
                    self.status = "error"
                    return
                if line.startswith("*** Sampled"):
                    if block and self.active:
                        self.publish(parse_powermetrics("\n".join(block)))
                    block = []
                elif line.startswith("{"):
                    try:
                        sample = json.loads(line)
                        sensors = normalize_windows(sample.get("sensors", [])) if os.name == "nt" else self._linux(sample.get("sensors", []))
                        if self.active:
                            self.publish(sensors, sample.get("thermal_critical"))
                    except (ValueError, TypeError, KeyError, AttributeError):
                        pass
                elif len(block) < 2048:
                    block.append(line[:1024])

    def _linux(self, sensors):
        result, now = [], time.monotonic()
        for sensor in sensors[:512] if isinstance(sensors, list) else []:
            if not isinstance(sensor, dict) or not isinstance(sensor.get("id"), str):
                continue
            if sensor.get("kind") == "energy":
                previous = self.energy.get(sensor["id"])
                self.energy[sensor["id"]] = now, sensor["value"]
                if not previous or now <= previous[0] or sensor["value"] < previous[1]:
                    continue
                sensor = dict(sensor, kind="power", unit="W", value=(sensor["value"] - previous[1]) / 1e6 / (now - previous[0]))
            if sensor.get("kind") not in {"temperature", "fan", "power", "voltage", "frequency", "utilization"}:
                continue
            sensor["label"] = named_sensor(None, sensor["id"], sensor["kind"])
            result.append(sensor)
        return result

    def close(self):
        self.stop_event.set()
        if self.connection:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.thread:
            self.thread.join(timeout=1)
        if self.process and not isinstance(self.process, WindowsLaunch) and self.process.poll() is None:
            # This is the non-elevated authorization client, not an arbitrary PID.
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.directory.cleanup()


_publish_lock = threading.Lock()
