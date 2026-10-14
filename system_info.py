"""One-shot, read-only hardware summary. No benchmarks, serial numbers or inference."""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

import psutil


def command(args, timeout=6):
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        return result.stdout[:2_000_000] if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return ""


def collect():
    result = {"cpu_model": None, "physical": psutil.cpu_count(logical=False),
        "logical": psutil.cpu_count(), "memory_total": round(psutil.virtual_memory().total / 2**30, 1),
        "nominal_frequency": None, "memory_rate": None, "gpu": None,
        "os": platform.system() + " " + platform.release(), "architecture": platform.machine()}
    if sys.platform == "darwin":
        keys = ["machdep.cpu.brand_string", "hw.cpufrequency_max", "hw.perflevel0.name", "hw.perflevel0.physicalcpu",
                "hw.perflevel1.name", "hw.perflevel1.physicalcpu"]
        values = dict(line.split(": ", 1) for line in command(["/usr/sbin/sysctl", *keys]).splitlines() if ": " in line)
        # sysctl may return nonzero for just one missing key; query brand separately.
        result["cpu_model"] = values.get(keys[0]) or command(["/usr/sbin/sysctl", "-n", keys[0]]).strip() or None
        frequency = command(["/usr/sbin/sysctl", "-n", "hw.cpufrequency_max"]).strip()
        if frequency.isdigit() and int(frequency) > 0:
            result["nominal_frequency"] = f"{int(frequency) / 1e6:g} MHz"
        for index in range(2):
            name = command(["/usr/sbin/sysctl", "-n", f"hw.perflevel{index}.name"]).strip().lower()
            count = command(["/usr/sbin/sysctl", "-n", f"hw.perflevel{index}.physicalcpu"]).strip()
            if name in ("performance", "efficiency") and count.isdigit():
                result["performance" if name == "performance" else "efficiency_cores"] = int(count)
        try:
            info = json.loads(command(["/usr/sbin/system_profiler", "SPDisplaysDataType", "SPMemoryDataType", "-json", "-detailLevel", "mini"], 10))
            result["gpu"] = ", ".join(str(item.get("sppci_model") or item.get("_name")) for item in info.get("SPDisplaysDataType", []) if item.get("sppci_model") or item.get("_name")) or None
            speeds = set()
            def memory_items(items):
                for item in items:
                    if item.get("dimm_speed"):
                        speeds.add(str(item["dimm_speed"]))
                    memory_items(item.get("_items", []))
            memory_items(info.get("SPMemoryDataType", []))
            result["memory_rate"] = ", ".join(sorted(speeds)) or None
        except (ValueError, TypeError):
            pass
    elif sys.platform == "win32":
        script = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
                  "$c=Get-CimInstance Win32_Processor; $m=Get-CimInstance Win32_PhysicalMemory; "
                  "$g=Get-CimInstance Win32_VideoController; "
                  "@{cpu=@($c|Select-Object Name,MaxClockSpeed); memory=@($m|Select-Object ConfiguredClockSpeed); "
                  "gpu=@($g|Select-Object Name)}|ConvertTo-Json -Depth 4 -Compress")
        try:
            info = json.loads(command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], 10))
            result["cpu_model"] = ", ".join(dict.fromkeys(item["Name"].strip() for item in info["cpu"]))
            result["nominal_frequency"] = ", ".join(sorted({f'{item["MaxClockSpeed"]} MHz' for item in info["cpu"] if item.get("MaxClockSpeed", 0) > 0})) or None
            result["memory_rate"] = ", ".join(sorted({f'{item["ConfiguredClockSpeed"]} MHz' for item in info["memory"] if item.get("ConfiguredClockSpeed", 0) > 0})) or None
            result["gpu"] = ", ".join(item["Name"] for item in info["gpu"])
        except (ValueError, KeyError, TypeError):
            pass
    else:
        match = re.search(r"^(?:model name|Hardware)\s*:\s*(.+)", read("/proc/cpuinfo"), re.M)
        result["cpu_model"] = match[1] if match else platform.processor() or None
        frequencies = [read(path) for path in Path("/sys/devices/system/cpu/cpufreq").glob("policy*/cpuinfo_max_freq")]
        result["nominal_frequency"] = ", ".join(f"{value / 1000:g} MHz" for value in sorted({int(item) for item in frequencies if item.isdigit()})) or None
        if shutil.which("lspci"):
            lines = command(["lspci", "-mm"]).splitlines()
            result["gpu"] = "; ".join(" ".join(re.findall(r'"([^\"]*)"', line)[1:3]) for line in lines if re.search(r'"(?:VGA|3D|Display)', line)) or None
        if not result["gpu"] and shutil.which("nvidia-smi"):
            result["gpu"] = command(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]).strip() or None
        if shutil.which("dmidecode"):
            dmi = command(["dmidecode", "--type", "17"])
            speeds = re.findall(r"Configured (?:Memory |Clock )?Speed:\s*(\d+\s*(?:MT/s|MHz))", dmi)
            result["memory_rate"] = ", ".join(sorted(set(speeds))) or None
    return result


def main():
    print(json.dumps(collect(), ensure_ascii=True), flush=True)
    return 0
