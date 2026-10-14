"""Bounded subprocess management shared by CLI and desktop worker."""
import os
import signal
import subprocess
import sys
from pathlib import Path


def app_command(mode):
    if getattr(sys, "frozen", False):
        return [sys.executable, "--" + mode]
    return [sys.executable, str(Path(__file__).resolve().parent / "desktop_app.py"), "--" + mode]


def stop_process_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=15)


class Cancellation:
    def __init__(self, path=None, parent_pid=None):
        self.path = path
        self.parent = None
        self.missing_parent = False
        if parent_pid:
            import psutil
            try:
                self.parent = psutil.Process(parent_pid)
                self.parent.create_time()  # caches identity to avoid PID reuse
            except psutil.Error:
                self.missing_parent = True

    def __call__(self):
        if self.path and self.path.exists():
            return True
        return self.missing_parent or (self.parent is not None and not self.parent.is_running())
