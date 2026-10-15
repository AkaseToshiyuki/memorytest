"""Fixed-operation native reads inheriting an already-elevated startup token.

Never requests authorization. The helper ends on disconnect or timeout.
"""
import hmac
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time

from benchmark_registry import ROOT
from hardware_inspector import KINDS, LIMIT, Reader, collect
from monitor_access import reader_command
from runtime_permissions import is_administrator

CONNECT_TIMEOUT = 5


class InspectionFailure(RuntimeError):
    """A broken/cancelled authorized session must not become a partial snapshot."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def operation(args):
    name = Path(args[0]).name.lower()
    if name in {"smartctl", "smartctl.exe"}:
        if args[1:] == ["--scan", "-j"]:
            return "scan"
        if len(args) == 6 and args[1:5] == ["-a", "-j", "-n", "standby"]:
            import re
            if re.fullmatch(r"/dev/(?:sd[a-z]+|nvme\d+(?:n\d+)?|disk\d+|pd\d+)", args[5]):
                return "smart " + args[5]
    if name == "dmidecode" and args[1:] == ["--type", "17"]:
        return "memory"
    if name == "decode-dimms" and len(args) == 1:
        return "spd"
    if name == "powermetrics" and args[1:] == ["--samplers", "cpu_power,gpu_power", "-n", "1", "-i", "1000"]:
        return "power"
    if name == "powershell.exe" and len(args) == 5 and args[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]:
        for cls, key in (("Win32_Processor", "cpu"), ("Win32_PhysicalMemory", "memory"),
                         ("Win32_VideoController", "gpu"), ("Win32_DiskDrive", "disks")):
            if "Get-CimInstance " + cls in args[4]:
                return key  # Native side supplies its own fixed script.
    return None


class InspectorAccess:
    def __init__(self, helper=None):
        self.helper = Path(helper or ROOT / "bin" / ("telemetry_privileged.exe" if os.name == "nt" else "telemetry_privileged"))
        self.status = "idle"
        self.snapshot = None
        self.peer = self.process = self.thread = None
        self.stop = threading.Event()

    def start(self, kind, launcher=None):
        if kind not in KINDS:
            raise ValueError("unknown hardware tool")
        if self.thread and self.thread.is_alive():
            return
        if not is_administrator():
            self.snapshot = None
            self.status = "denied"
            return
        self.stop.clear()
        self.snapshot = None
        self.status = "starting"
        self.thread = threading.Thread(target=self._run, args=(kind, launcher), daemon=True)
        self.thread.start()

    def _run(self, kind, launcher):
        token = secrets.token_hex(32)
        try:
            if not self.helper.is_file():
                self.status = "unavailable"
                return
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.bind(("127.0.0.1", 0)); server.listen(2); server.settimeout(.2)
                args = reader_command(self.helper, server.getsockname()[1], token, os.getpid(), inspection=True)
                if self.stop.is_set():
                    return
                self.process = launcher(args) if launcher else subprocess.Popen(
                    args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                deadline = time.monotonic() + CONNECT_TIMEOUT
                while not self.stop.is_set() and time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        self.status = "error"
                        return
                    try:
                        peer, _ = server.accept()
                    except socket.timeout:
                        continue
                    peer.settimeout(.5)
                    greeting = bytearray()
                    try:
                        while not greeting.endswith(b"\n") and len(greeting) < 100:
                            part = peer.recv(1)
                            if not part:
                                break
                            greeting.extend(part)
                    except OSError:
                        peer.close(); continue
                    if not hmac.compare_digest(greeting, ("MTINSPECT/1 " + token + "\n").encode()):
                        peer.close(); continue
                    self.peer = peer
                    self.status = "reading"
                    snapshot = collect(kind, Reader(budget=45, access=self.request))
                    if not self.stop.is_set():
                        self.snapshot = snapshot
                        self.status = "ready"
                    return
                if not self.stop.is_set():
                    self.status = "timeout"
        except InspectionFailure as exc:
            if not self.stop.is_set():
                self.status = exc.status
        except PermissionError:
            if not self.stop.is_set():
                self.status = "denied"
        except (OSError, ValueError, TypeError):
            if not self.stop.is_set():
                self.status = "error"
        finally:
            if self.peer:
                self.peer.close(); self.peer = None
            if self.process:
                try:
                    # Reap the direct reader child on every terminal path.
                    # Let its disconnect handler stop its child group first.
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=1)
                except OSError:
                    pass

    def request(self, args, timeout):
        if self.stop.is_set():
            raise InspectionFailure("cancelled")
        query = operation(args)
        if query is None:
            return None
        peer = self.peer
        if peer is None:
            raise InspectionFailure("error")
        # Native Windows CIM reads have a 30-second cap, plus two seconds
        # for response framing. Other fixed operations retain their old cap.
        limit = 32 if Path(args[0]).name.lower() == "powershell.exe" else 14
        deadline = time.monotonic() + min(limit, timeout)

        def remaining():
            if self.stop.is_set():
                raise InspectionFailure("cancelled")
            value = deadline - time.monotonic()
            if value <= 0:
                raise InspectionFailure("timeout")
            peer.settimeout(value)

        def receive(length):
            remaining()
            part = peer.recv(length)
            if not part:
                raise InspectionFailure("error")
            return part

        try:
            remaining()
            peer.sendall((query + "\n").encode("ascii"))
            header = bytearray()
            while not header.endswith(b"\n") and len(header) < 64:
                header.extend(receive(1))
            if not header.endswith(b"\n"):
                raise InspectionFailure("error")
            code, length = map(int, header.split())
            if not 0 <= length <= LIMIT or not -3 <= code <= 255:
                raise InspectionFailure("error")
            data = bytearray()
            while len(data) < length:
                data.extend(receive(min(65536, length - len(data))))
            if code in (-2, -3):
                raise InspectionFailure("timeout" if code == -2 else "error")
            return bytes(data), code
        except (OSError, ValueError, InspectionFailure) as exc:
            # A timeout or partial frame cannot be followed by another query:
            # the old reply could otherwise be mistaken for a new device.
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if isinstance(exc, InspectionFailure):
                raise
            raise InspectionFailure("timeout" if isinstance(exc, TimeoutError) else "error") from exc

    def close(self):
        self.stop.set()
        peer = self.peer
        if peer:
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # The worker closes the listener/socket and reaps the direct child.
        # Do not kill it before its disconnect handler reaps query descendants.
        self.status = "cancelled"
