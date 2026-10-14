"""Stress protocol, resource limits and streaming statistics; no hardware access."""
import math
import os
from pathlib import Path

MODES = ("cpu", "fpu", "cache", "ram", "gpu")
CPU_MASK = {"cpu": 1, "fpu": 2, "cache": 4, "ram": 8}
MIB = 1024 * 1024
GPU_BYTES = 64 * MIB
CACHE_BYTES_PER_THREAD = 64 * 1024


def allowed_threads():
    try:
        count = len(os.sched_getaffinity(0))
    except AttributeError:
        count = os.cpu_count() or 1
    return min(256, max(1, count))


def available_memory(system_available=None, cgroup_root=Path("/sys/fs/cgroup"), membership=Path("/proc/self/cgroup")):
    """Constrain OS availability by readable Linux cgroup v1/v2 ancestors."""
    if system_available is None:
        import psutil
        system_available = psutil.virtual_memory().available
    available = max(0, int(system_available))
    root = Path(cgroup_root).resolve()
    candidates = {root, root / "memory"}
    try:
        for line in Path(membership).read_text().splitlines():
            _, controllers, relative = line.split(":", 2)
            if controllers not in {"", "memory"} and "memory" not in controllers.split(","):
                continue
            base = root / "memory" if controllers else root
            path = (base / relative.lstrip("/")).resolve()
            if not path.is_relative_to(base):
                continue
            for _ in range(64):
                candidates.add(path)
                if path == base:
                    break
                path = path.parent
    except (OSError, ValueError):
        pass
    for directory in candidates:
        for maximum, usage in [("memory.max", "memory.current"), ("memory.limit_in_bytes", "memory.usage_in_bytes")]:
            try:
                limit = int((directory / maximum).read_text().strip())
                used = int((directory / usage).read_text().strip())
                if limit > 0 and used >= 0:
                    available = min(available, max(0, limit - used))
            except (OSError, ValueError):
                pass
    return available


def memory_limits(available, threads, gpu=False):
    """RAM plus cache and GPU allocations must all fit half of available memory."""
    overhead = threads * CACHE_BYTES_PER_THREAD + (GPU_BYTES if gpu else 0)
    maximum = max(0, (int(available) // 2 - overhead) // MIB)
    default = min(maximum, 2048, int(available) // 4 // MIB)
    return default, maximum


class Statistics:
    def __init__(self):
        self.rows = {}

    def add(self, sensor, value):
        row = self.rows.setdefault(sensor, {"current": None, "minimum": None, "maximum": None, "average": None, "count": 0})
        row["current"] = None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return
        row["current"] = value
        row["count"] += 1
        row["minimum"] = value if row["minimum"] is None else min(row["minimum"], value)
        row["maximum"] = value if row["maximum"] is None else max(row["maximum"], value)
        row["average"] = value if row["average"] is None else row["average"] + (value - row["average"]) / row["count"]

    def sample(self, values):
        for sensor in self.rows.keys() | values.keys():
            self.add(sensor, values.get(sensor))


def validate_modes(values, capabilities):
    if not isinstance(values, list) or any(value not in MODES for value in values):
        raise ValueError("invalid modes")
    if any(not capabilities.get(value, False) for value in values):
        raise ValueError("unsupported modes")
    return [mode for mode in MODES if mode in values]
