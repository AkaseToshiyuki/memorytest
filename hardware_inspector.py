"""Read-only, bounded, on-demand hardware inspection; no drivers or elevation.

OS inventory, firmware declarations and device SMART data retain their sources.
Missing values are None, never inferred from a model-name lookup. This module
does not import Qt and does not start benchmarks, self-tests or hardware writes.
"""
import argparse
import csv
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time

import psutil
from runtime_permissions import is_administrator

KINDS = ("cpu", "gpu", "dram", "disk")
LIMIT = 2_000_000


def text(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and not math.isfinite(value):
        return None
    result = str(value).strip().replace("\x00", "")[:8192]
    return None if result.lower() in {"", "unknown", "not specified", "n/a", "none", "[n/a]", "not available"} else result


def size(value):
    try:
        number = int(value)
        if number <= 0:
            return None
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if number < 1024 or unit == "TiB":
                return f"{number:.3f}".rstrip("0").rstrip(".") + " " + unit
            number /= 1024
    except (ValueError, TypeError, OverflowError):
        return None


def with_unit(value, unit):
    return f"{text(value)} {unit}" if text(value) is not None else None


def group(title, source, **values):
    return {"title": title, "fields": [
        {"name": key, "value": text(value), "source": source,
         "private": key in {"serial", "uuid"},
         "status": "available" if text(value) is not None else "not_reported"}
        for key, value in values.items()]}


def missing(section, names, reason):
    for field in section["fields"]:
        if field["name"] in names and field["value"] is None:
            field["status"] = reason
    return section


def profiler_value(value):
    """Normalize provider enum values, without guessing model specifications."""
    value = text(value)
    known = {"sppci_vendor_Apple": "Apple", "spdisplays_builtin": "integrated",
             "sppci_bus_builtin": "integrated", "spdisplays_bus_builtin": "integrated",
             "spdisplays_supported": "supported", "spdisplays_metal_supported": "supported"}
    if value in known:
        return known[value]
    match = re.fullmatch(r"spdisplays_metal(?:family)?_?(\d+)", value or "")
    return "Metal " + match[1] if match else value


def device(identity, name, groups, tables=None, notices=None):
    return {"id": str(identity), "name": text(name) or str(identity), "groups": groups,
            "tables": tables or [], "notices": notices or []}


def table(title, columns, rows, source):
    return {"title": title, "columns": columns, "rows": rows[:512], "source": source}


def read(path, limit=131072):
    try:
        with Path(path).open("r", errors="replace") as stream:
            return stream.read(limit).strip()
    except (OSError, ValueError):
        return ""


def find_tool(name):
    found = shutil.which(name)
    if found:
        return found
    # Finder does not inherit a shell's Homebrew PATH. Never download a tool.
    if sys.platform == "darwin":
        for prefix in ("/opt/homebrew/sbin", "/opt/homebrew/bin", "/usr/local/sbin", "/usr/local/bin"):
            path = Path(prefix) / name
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    if os.name == "nt" and name == "smartctl":
        path = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "smartmontools/bin/smartctl.exe"
        if path.is_file():
            return str(path)
    return None


class Reader:
    def __init__(self, budget=30, access=None):
        self.deadline = time.monotonic() + budget
        self.sources = []
        self.access = access
        self.privileged = access is not None or is_administrator()

    def run(self, args, timeout=7):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.sources.append({"source": Path(args[0]).name, "status": "timeout"})
            return b"", None
        try:
            if self.access is not None:
                answer = self.access(args, min(timeout, remaining))
                if answer is not None:
                    raw, code = answer
                    denied = code != 0 and any(word in raw.lower() for word in
                             (b"permission denied", b"operation not permitted", b"access is denied", b"requires root"))
                    status = "permission_denied" if denied else "unavailable" if code == -1 else "ok" if code == 0 else "partial"
                    self.sources.append({"source": Path(args[0]).name, "status": status,
                                         "exit_code": code, "access": "administrator"})
                    return raw, code
            result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=min(timeout, remaining), env={**os.environ, "LC_ALL": "C", "LANG": "C"},
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            denied = any(word in result.stderr.lower() for word in
                         (b"permission denied", b"operation not permitted", b"access is denied", b"requires root"))
            self.sources.append({"source": Path(args[0]).name,
                                 "status": "permission_denied" if denied else "ok" if result.returncode == 0 else "partial",
                                 "exit_code": result.returncode})
            return result.stdout[:LIMIT], result.returncode
        except subprocess.TimeoutExpired:
            self.sources.append({"source": Path(args[0]).name, "status": "timeout"})
        except PermissionError:
            self.sources.append({"source": Path(args[0]).name, "status": "permission_denied"})
        except OSError:
            self.sources.append({"source": Path(args[0]).name, "status": "unavailable"})
        return b"", None

    def json(self, args, timeout=7):
        raw, code = self.run(args, timeout)
        try:
            result = json.loads(raw.decode("utf-8-sig"))
            return result if isinstance(result, (dict, list)) else {}, code
        except (ValueError, UnicodeError):
            return {}, code

    def plist(self, args):
        raw, _ = self.run(args)
        try:
            result = plistlib.loads(raw)
            return result if isinstance(result, dict) else {}
        except (ValueError, TypeError, plistlib.InvalidFileException):
            return {}

    def powershell(self, script):
        prefix = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); $ErrorActionPreference='Stop'; "
        return self.json(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                          prefix + script + " | ConvertTo-Json -Depth 7 -Compress"], 12)[0]

    def profiler(self, kind):
        data, _ = self.json(["/usr/sbin/system_profiler", kind, "-json", "-detailLevel", "full"], 12)
        return data.get(kind, []) if isinstance(data, dict) else []


def items(value):
    return value if isinstance(value, list) else [value] if isinstance(value, dict) and value else []


def cpu_info(reader):
    base = dict(architecture=platform.machine(), physical=psutil.cpu_count(logical=False),
                logical=psutil.cpu_count(), os=platform.system() + " " + platform.release())
    groups = [group("topology", "OS / psutil", **base)]
    tables = []
    name = platform.processor() or "CPU"
    if sys.platform == "darwin":
        keys = ["machdep.cpu.brand_string", "machdep.cpu.vendor", "machdep.cpu.family",
                "machdep.cpu.model", "machdep.cpu.stepping", "machdep.cpu.features",
                "machdep.cpu.leaf7_features", "hw.model", "hw.cpufrequency", "hw.cpufrequency_max",
                "hw.l1icachesize", "hw.l1dcachesize", "hw.l2cachesize", "hw.l3cachesize",
                "hw.cachelinesize", "hw.perflevel0.name", "hw.perflevel0.physicalcpu",
                "hw.perflevel1.name", "hw.perflevel1.physicalcpu", "hw.cpufamily", "hw.cpusubtype",
                "hw.optional", "hw.nperflevels"]
        raw, _ = reader.run(["/usr/sbin/sysctl", *keys])
        values = dict(line.split(": ", 1) for line in raw.decode(errors="replace").splitlines() if ": " in line)
        try:
            levels = max(0, min(64, int(values.get("hw.nperflevels", "0"))))
        except ValueError:
            levels = 0
        if not levels:
            levels = max((i + 1 for i in range(2) if values.get(f"hw.perflevel{i}.name")), default=0)
        if levels:
            tier_keys = [f"hw.perflevel{i}." + field for i in range(levels) for field in
                         ("name", "physicalcpu", "logicalcpu", "cpusperl2", "cpusperl3", "l1icachesize",
                          "l1dcachesize", "l2cachesize", "l3cachesize")]
            tier_raw, _ = reader.run(["/usr/sbin/sysctl", *tier_keys])
            values.update(line.split(": ", 1) for line in tier_raw.decode(errors="replace").splitlines() if ": " in line)
        name = values.get(keys[0]) or name
        arm = platform.machine().lower() in {"arm64", "aarch64"} or values.get("hw.optional.arm64") == "1" or name.startswith("Apple ")
        features = [key.removeprefix("hw.optional.") for key, value in values.items()
                    if value == "1" and (key.startswith("hw.optional.arm.FEAT_") or key in {"hw.optional.neon", "hw.optional.AdvSIMD"})]
        groups += [group("processor", "sysctl", model=name, vendor=values.get(keys[1]) or ("Apple" if name.startswith("Apple ") else None),
                         family=values.get("hw.cpufamily") if arm else values.get(keys[2]),
                         model_id=values.get("hw.cpusubtype") if arm else values.get(keys[3]), stepping=values.get(keys[4]),
                         instructions=", ".join(features) if features else " ".join(filter(None, [values.get(keys[5]), values.get(keys[6])]))),
                   group("platform_info", "sysctl", board_model=values.get("hw.model"),
                         nominal_frequency=with_unit(values.get("hw.cpufrequency"), "Hz"),
                         maximum_frequency=with_unit(values.get("hw.cpufrequency_max"), "Hz")),
                   group("cache_lowest_performance" if arm else "cache", "sysctl hw.*", l1i=size(values.get("hw.l1icachesize")),
                         l1d=size(values.get("hw.l1dcachesize")), l2=size(values.get("hw.l2cachesize")),
                         l3=size(values.get("hw.l3cachesize")), cache_line=with_unit(values.get("hw.cachelinesize"), "B"))]
        if arm:
            missing(groups[1], {"stepping"}, "not_exposed")
            missing(groups[2], {"nominal_frequency", "maximum_frequency"}, "not_exposed")
            missing(groups[3], {"l3"}, "not_reported")
        rows = [[values.get(f"hw.perflevel{i}.name"), values.get(f"hw.perflevel{i}.physicalcpu"),
                 values.get(f"hw.perflevel{i}.logicalcpu"), size(values.get(f"hw.perflevel{i}.l1icachesize")),
                 size(values.get(f"hw.perflevel{i}.l1dcachesize")), size(values.get(f"hw.perflevel{i}.l2cachesize")),
                 values.get(f"hw.perflevel{i}.cpusperl2"), size(values.get(f"hw.perflevel{i}.l3cachesize")),
                 values.get(f"hw.perflevel{i}.cpusperl3")]
                for i in range(levels) if values.get(f"hw.perflevel{i}.name")]
        if rows:
            tables.append(table("performance_levels", ["performance_level", "physical", "logical", "l1i", "l1d", "l2", "cpus_per_l2", "l3", "cpus_per_l3"], rows, "sysctl hw.perflevel; cache size is per sharing group"))
        if arm and getattr(reader, "privileged", False) is True:
            tables.extend(power_snapshot(reader, "cpu"))
    elif sys.platform == "win32":
        data = reader.powershell("@{cpu=@(Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,SocketDesignation,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,CurrentClockSpeed,L2CacheSize,L3CacheSize); board=@(Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product); bios=@(Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion)}")
        result = []
        for index, cpu in enumerate(data.get("cpu", []) if isinstance(data, dict) else []):
            fields = group("processor", "Win32_Processor", model=cpu.get("Name"), vendor=cpu.get("Manufacturer"),
                           socket=cpu.get("SocketDesignation"), physical=cpu.get("NumberOfCores"),
                           logical=cpu.get("NumberOfLogicalProcessors"), nominal_frequency=with_unit(cpu.get("MaxClockSpeed"), "MHz"),
                           current_frequency=with_unit(cpu.get("CurrentClockSpeed"), "MHz"),
                           l2=with_unit(cpu.get("L2CacheSize"), "KiB"), l3=with_unit(cpu.get("L3CacheSize"), "KiB"))
            extra = [group("mainboard", "Win32_BaseBoard", vendor=b.get("Manufacturer"), board_model=b.get("Product")) for b in data.get("board", [])]
            extra += [group("firmware", "Win32_BIOS", vendor=b.get("Manufacturer"), firmware=b.get("SMBIOSBIOSVersion")) for b in data.get("bios", [])]
            result.append(device(f"cpu{index}", cpu.get("Name"), [fields, *groups, *extra], notices=["os_clock_note"]))
        if result:
            return result
    else:
        raw = read("/proc/cpuinfo")
        first = raw.split("\n\n")[0]
        values = dict(re.findall(r"^([^:\n]+)\s*:\s*(.+)$", first, re.M))
        values = {k.strip(): v for k, v in values.items()}
        name = values.get("model name") or values.get("Hardware") or name
        data, _ = reader.json(["lscpu", "--json"])
        lscpu = {r["field"].rstrip(":"): r.get("data") for r in data.get("lscpu", []) if "field" in r} if isinstance(data, dict) else {}
        groups += [group("processor", "/proc/cpuinfo; lscpu", model=name, vendor=values.get("vendor_id") or lscpu.get("Vendor ID"),
                         family=values.get("cpu family"), model_id=values.get("model"), stepping=values.get("stepping"),
                         sockets=lscpu.get("Socket(s)"), current_frequency=with_unit(values.get("cpu MHz"), "MHz"),
                         maximum_frequency=with_unit(lscpu.get("CPU max MHz"), "MHz"), instructions=values.get("flags") or values.get("Features")),
                   group("mainboard", "/sys/class/dmi/id (firmware)", vendor=read("/sys/class/dmi/id/board_vendor"),
                         board_model=read("/sys/class/dmi/id/board_name"), firmware=read("/sys/class/dmi/id/bios_version"),
                         firmware_date=read("/sys/class/dmi/id/bios_date"))]
        rows = [[read(p / "level"), read(p / "type"), read(p / "size"), read(p / "coherency_line_size"), read(p / "shared_cpu_list")]
                for p in sorted(Path("/sys/devices/system/cpu/cpu0/cache").glob("index*"))]
        if rows:
            tables.append(table("cache_domains", ["level", "type", "capacity", "cache_line", "shared_cpus"], rows, "sysfs CPU 0 cache domains"))
    return [device("cpu", name, groups, tables, ["os_clock_note"])]


def power_snapshot(reader, kind):
    from monitor_access import parse_powermetrics
    raw, code = reader.run(["/usr/bin/powermetrics", "--samplers", "cpu_power,gpu_power",
                            "-n", "1", "-i", "1000"], 5)
    if code != 0:
        return []
    sensors = parse_powermetrics(raw.decode(errors="replace"))
    rows = [[s["id"].removeprefix("powermetrics:"), with_unit(s["value"], s["unit"])] for s in sensors
            if ("gpu" in s["id"].lower()) == (kind == "gpu")]
    return [table("active_snapshot", ["parameter", "value"], rows, "powermetrics; one-second active interval, not nominal clock")] if rows else []


def mac_metal(reader):
    helper = Path(__file__).resolve().parent / "bin/hardware_macos"
    if not helper.is_file():
        return []
    data, _ = reader.json([str(helper)])
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def metal_group(row):
    return group("metal_device", "Metal MTLDevice", registry_id=row.get("registry_id"),
                 unified_memory="yes" if row.get("unified") else "no",
                 recommended_working_set=size(row.get("recommended_working_set")),
                 max_buffer_length=size(row.get("max_buffer_length")),
                 metal="supported", metal_families=", ".join(row.get("families", [])))


def gpu_info(reader):
    result = []
    if sys.platform == "darwin":
        metal_devices = mac_metal(reader)
        profiler_devices = reader.profiler("SPDisplaysDataType")
        for index, gpu in enumerate(profiler_devices):
            name = gpu.get("sppci_model") or gpu.get("_name")
            candidates = [m for m in metal_devices if m.get("name") == name]
            # Only merge an unambiguous model match. Identical-name GPUs remain
            # separate Metal identities instead of acquiring each other's RAM.
            native = candidates[0] if len(candidates) == 1 and sum(g.get("sppci_model") == name for g in profiler_devices) == 1 else {}
            unified = native.get("unified") is True
            section = group("graphics_adapter", "system_profiler SPDisplaysDataType", model=name,
                      vendor=profiler_value(gpu.get("spdisplays_vendor")), device_id=gpu.get("spdisplays_device-id"),
                      gpu_cores=gpu.get("sppci_cores"), bus=profiler_value(gpu.get("spdisplays_bus") or gpu.get("sppci_bus")),
                      vram=gpu.get("spdisplays_vram"), shared_memory=gpu.get("spdisplays_vram_shared"),
                      metal=profiler_value(gpu.get("spdisplays_metal") or gpu.get("spdisplays_metalfamily") or gpu.get("spdisplays_metal_family")),
                      revision=gpu.get("spdisplays_revision-id"))
            if unified:
                missing(section, {"vram", "device_id", "revision"}, "not_applicable")
                missing(section, {"shared_memory"}, "dynamic_shared")
            groups = [section]
            if native:
                groups.append(metal_group(native))
            result.append(device("metal:" + native["registry_id"] if native else f"gpu{index}", name, groups,
                                 notices=["gpu_shared_note"]))
        matched = {d["id"] for d in result}
        for native in metal_devices:
            if "metal:" + native["registry_id"] not in matched:
                result.append(device("metal:" + native["registry_id"], native["name"], [metal_group(native)]))
        if len(result) == 1 and getattr(reader, "privileged", False) is True:
            result[0]["tables"].extend(power_snapshot(reader, "gpu"))
    elif sys.platform == "win32":
        data = reader.powershell("@(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,PNPDeviceID,DriverVersion,VideoProcessor,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate)")
        for index, gpu in enumerate(items(data)):
            result.append(device(gpu.get("PNPDeviceID", f"gpu{index}"), gpu.get("Name"), [
                group("graphics_adapter", "Win32_VideoController", model=gpu.get("Name"), vendor=gpu.get("AdapterCompatibility"),
                      gpu_processor=gpu.get("VideoProcessor"), device_id=gpu.get("PNPDeviceID"),
                      driver=gpu.get("DriverVersion"), vram=None, memory_type=None,
                      display_width=gpu.get("CurrentHorizontalResolution"), display_height=gpu.get("CurrentVerticalResolution"),
                      refresh_rate=with_unit(gpu.get("CurrentRefreshRate"), "Hz"))], notices=["wmi_vram_note"]))
    else:
        for path in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
            if not re.fullmatch(r"card\d+", path.name):
                continue
            hw = path / "device"
            pci_id = hw.resolve().name
            raw, _ = reader.run(["lspci", "-s", pci_id, "-mm"])
            quoted = re.findall(r'"([^"]*)"', raw.decode(errors="replace"))
            name = " ".join(quoted[1:3]) if len(quoted) >= 3 else path.name
            result.append(device(pci_id, name, [group("graphics_adapter", "DRM sysfs / lspci", model=name,
                vendor=read(hw / "vendor"), device_id=read(hw / "device"), pci_address=pci_id,
                driver=(hw / "driver").resolve().name if (hw / "driver").exists() else None,
                vram=size(read(hw / "mem_info_vram_total")), vram_used=size(read(hw / "mem_info_vram_used")),
                pcie_speed=read(hw / "current_link_speed"), pcie_width=read(hw / "current_link_width"),
                gpu_usage=with_unit(read(hw / "gpu_busy_percent"), "%"))]))
    smi = find_tool("nvidia-smi")
    if smi:
        raw, code = reader.run([smi, "--query-gpu=name,pci.bus_id,driver_version,memory.total,memory.used,temperature.gpu,utilization.gpu,clocks.gr,clocks.mem,power.draw", "--format=csv,noheader,nounits"])
        if code == 0:
            for row in csv.reader(io.StringIO(raw.decode(errors="replace"))):
                if len(row) != 10:
                    continue
                name, bus, driver, total, used, temp, busy, core, mem, power = [x.strip() for x in row]
                # NVIDIA reports 8 domain digits; sysfs normally reports four.
                normalized = bus.lower().split(":", 1)[-1]
                match = next((d for d in result if d["id"].lower().split(":", 1)[-1] == normalized), None)
                if match is None:
                    named = [d for d in result if d["name"] == name]
                    match = named[0] if len(named) == 1 else None
                if match is None:
                    match = device(bus, name + " · " + bus, [], notices=["gpu_driver_identity"])
                    result.append(match)
                match["groups"].append(group("device_snapshot", "nvidia-smi (snapshot)", driver=driver,
                    vram=with_unit(total, "MiB"), vram_used=with_unit(used, "MiB"), temperature=with_unit(temp, "°C"),
                    gpu_usage=with_unit(busy, "%"), current_frequency=with_unit(core, "MHz"),
                    memory_clock=with_unit(mem, "MHz"), power=with_unit(power, "W")))
    return result


MEMORY_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 27: "LPDDR", 28: "LPDDR2",
                29: "LPDDR3", 30: "LPDDR4", 34: "DDR5", 35: "LPDDR5"}


def memory_from_dmi(raw):
    result = []
    for block in re.split(r"\n(?=Handle )", raw):
        if "Memory Device\n" not in block:
            continue
        values = dict(re.findall(r"^\s+([^:\n]+):\s*(.*)$", block, re.M))
        if "No Module Installed" in values.get("Size", ""):
            continue
        result.append(device(values.get("Locator", f"dimm{len(result)}"), values.get("Locator"), [
            group("memory_module", "SMBIOS / dmidecode (firmware)", slot=values.get("Locator"),
                  bank=values.get("Bank Locator"), capacity=values.get("Size"), memory_type=values.get("Type"),
                  form_factor=values.get("Form Factor"), vendor=values.get("Manufacturer"),
                  part_number=values.get("Part Number"), serial=values.get("Serial Number"), ranks=values.get("Rank"),
                  data_width=values.get("Data Width"), total_width=values.get("Total Width"),
                  rated_speed=values.get("Speed"), configured_speed=values.get("Configured Memory Speed") or values.get("Configured Clock Speed"),
                  configured_voltage=values.get("Configured Voltage"))], notices=["firmware_not_spd"]))
    return result


def dram_info(reader):
    result = []
    if sys.platform == "darwin":
        def visit(rows):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if any(k in row for k in ("dimm_size", "dimm_type", "dimm_manufacturer", "SPMemoryDataType")):
                    section = group("memory_module", "system_profiler SPMemoryDataType", capacity=row.get("dimm_size") or row.get("SPMemoryDataType"),
                              memory_type=row.get("dimm_type"), vendor=row.get("dimm_manufacturer"),
                              configured_speed=row.get("dimm_speed"), part_number=row.get("dimm_part_number"),
                              serial=row.get("dimm_serial_number"), status=row.get("dimm_status"))
                    if "SPMemoryDataType" in row:
                        missing(section, {"configured_speed", "part_number", "serial", "status"}, "not_exposed")
                    result.append(device(f"memory{len(result)}", row.get("_name", "DRAM"), [section], notices=["firmware_not_spd"]))
                visit(row.get("_items", []))
        visit(reader.profiler("SPMemoryDataType"))
    elif sys.platform == "win32":
        data = reader.powershell("@(Get-CimInstance Win32_PhysicalMemory | Select-Object DeviceLocator,BankLabel,Capacity,Manufacturer,PartNumber,SerialNumber,ConfiguredClockSpeed,SMBIOSMemoryType,DataWidth,TotalWidth,ConfiguredVoltage)")
        for index, row in enumerate(items(data)):
            result.append(device(row.get("DeviceLocator", f"dimm{index}"), row.get("DeviceLocator"), [
                group("memory_module", "Win32_PhysicalMemory (SMBIOS)", slot=row.get("DeviceLocator"), bank=row.get("BankLabel"),
                      capacity=size(row.get("Capacity")), vendor=row.get("Manufacturer"), part_number=row.get("PartNumber"),
                      serial=row.get("SerialNumber"), memory_type=MEMORY_TYPES.get(row.get("SMBIOSMemoryType")),
                      rated_speed=None,
                      configured_speed=with_unit(row.get("ConfiguredClockSpeed") or None, "MHz (CIM)"),
                      data_width=with_unit(row.get("DataWidth"), "bit"), total_width=with_unit(row.get("TotalWidth"), "bit"),
                      configured_voltage=with_unit(row.get("ConfiguredVoltage") or None, "mV"))], notices=["firmware_not_spd"]))
    else:
        dmi = find_tool("dmidecode")
        if dmi:
            raw, _ = reader.run([dmi, "--type", "17"])
            result = memory_from_dmi(raw.decode(errors="replace"))
        decoder = find_tool("decode-dimms")
        if decoder:
            # decode-dimms uses existing read-only kernel EEPROM interfaces;
            # no modprobe, i2c scan, driver binding, or EEPROM writes here.
            raw, code = reader.run([decoder], 10)
            if code == 0 and b"Decoding EEPROM" in raw:
                for block in re.split(r"(?=Decoding EEPROM:)", raw.decode(errors="replace"))[1:33]:
                    lines = block.splitlines()
                    pairs = [re.split(r"\s{2,}", line.strip(), maxsplit=1) for line in lines[1:] if line.strip() and not line.lstrip().startswith("---")]
                    rows = [row for row in pairs if len(row) == 2 and "serial" not in row[0].lower()]
                    result.append(device(lines[0], lines[0], [], [table("spd_report", ["parameter", "value"], rows, "decode-dimms / kernel EEPROM")], ["spd_not_live_timings"]))
    summary = device("memory-summary", "memory_overview", [group("memory_overview", "OS / psutil",
                     capacity=size(psutil.virtual_memory().total), available_memory=size(psutil.virtual_memory().available),
                     active_timings=None, xmp_expo=None)], notices=["dram_access_note"])
    return [summary, *result]


def smart_device(data, identity, exit_code=0):
    """Interpret native smartctl JSON even when a health warning makes exit != 0."""
    status = data.get("smart_status", {}).get("passed")
    nvme = data.get("nvme_smart_health_information_log", {})
    bits = data.get("smartctl", {}).get("exit_status", exit_code) or 0
    bits = bits if isinstance(bits, int) and bits >= 0 else 0
    critical = nvme.get("critical_warning", 0)
    critical = critical if isinstance(critical, int) else 0
    if status is False or bits & 24 or critical:
        health = "smart_failed"
    elif bits & 224:
        health = "smart_warning"
    elif bits & 7:
        health = "smart_unknown"
    elif status is True:
        health = "smart_passed"
    else:
        health = "smart_unknown"
    raw_attributes = data.get("ata_smart_attributes", {}).get("table", [])
    # Surface common bad-sector counters, but never turn other vendor-specific
    # raw counters into a universal life percentage.
    if health == "smart_passed" and any(row.get("id") in {5, 197, 198} and
            isinstance(row.get("raw", {}).get("value"), int) and row["raw"]["value"] > 0 for row in raw_attributes):
        health = "smart_warning"
    if health == "smart_passed" and ((isinstance(nvme.get("percentage_used"), (int, float)) and nvme["percentage_used"] >= 100)
                                    or (isinstance(nvme.get("media_errors"), int) and nvme["media_errors"] > 0)):
        health = "smart_warning"
    capacity = data.get("user_capacity", {}).get("bytes") or data.get("nvme_total_capacity")
    temp = data.get("temperature", {}).get("current", nvme.get("temperature"))
    groups = [group("drive_identity", "smartctl / device", model=data.get("model_name") or data.get("product"),
                    serial=data.get("serial_number"), firmware=data.get("firmware_version"), capacity=size(capacity),
                    protocol=data.get("device", {}).get("protocol"), device_path=identity,
                    rotation_rate=with_unit(data.get("rotation_rate"), "RPM")),
              group("smart_summary", "smartctl / device", health=health, temperature=with_unit(temp, "°C"),
                    power_on_hours=data.get("power_on_time", {}).get("hours", nvme.get("power_on_hours")),
                    power_cycles=data.get("power_cycle_count", nvme.get("power_cycles")),
                    percentage_used=with_unit(nvme.get("percentage_used"), "%"),
                    available_spare=with_unit(nvme.get("available_spare"), "%"),
                    media_errors=nvme.get("media_errors"), unsafe_shutdowns=nvme.get("unsafe_shutdowns"))]
    tables = []
    if raw_attributes:
        rows = [[f'{row["id"]:02X}' if isinstance(row.get("id"), int) else row.get("id"), row.get("name"),
                 row.get("value"), row.get("worst"), row.get("thresh"),
                 row.get("raw", {}).get("string", row.get("raw", {}).get("value")), row.get("when_failed")]
                for row in raw_attributes]
        tables.append(table("ata_attributes", ["id_hex", "attribute", "current", "worst", "threshold", "raw_value", "when_failed"], rows, "smartctl ATA; raw value uses provider formatting"))
    if nvme:
        rows = [[key, value] for key, value in nvme.items() if isinstance(value, (str, int, float)) and not key.endswith("_s")]
        tables.append(table("nvme_health", ["parameter", "value"], rows, "smartctl NVMe SMART/Health log"))
    notes = ["smart_not_guarantee"]
    if bits & 7:
        notes.append("smart_read_error")
    dev = device(identity, data.get("model_name") or data.get("product") or identity, groups, tables, notes)
    dev["health"] = health
    return dev


def disk_info(reader):
    result = []
    if sys.platform == "darwin":
        listing = reader.plist(["/usr/sbin/diskutil", "list", "-plist", "physical"])
        for identifier in listing.get("WholeDisks", [])[:32]:
            if not re.fullmatch(r"disk\d+", identifier):
                continue
            data = reader.plist(["/usr/sbin/diskutil", "info", "-plist", identifier])
            status = data.get("SMARTStatus")
            dev = device("/dev/" + identifier, data.get("MediaName") or identifier, [group("drive_identity", "diskutil (OS)",
                model=data.get("MediaName"), capacity=size(data.get("TotalSize")), device_path="/dev/" + identifier,
                protocol=data.get("BusProtocol"), solid_state=data.get("SolidState"), smart_os_status=status)], notices=["smart_os_only"])
            dev["health"] = "smart_failed" if status == "Failing" else "smart_unknown"
            result.append(dev)
    elif sys.platform == "win32":
        data = reader.powershell("@(Get-CimInstance Win32_DiskDrive | Select-Object Index,Model,SerialNumber,FirmwareRevision,Size,InterfaceType,Status)")
        for row in items(data):
            index = row.get("Index")
            if not isinstance(index, int) or not 0 <= index < 1024:
                continue
            result.append(device(f"/dev/pd{index}", row.get("Model"), [group("drive_identity", "Win32_DiskDrive (OS)",
                model=row.get("Model"), serial=row.get("SerialNumber"), firmware=row.get("FirmwareRevision"),
                capacity=size(row.get("Size")), protocol=row.get("InterfaceType"), os_status=row.get("Status"))], notices=["smart_os_only"]))
    else:
        for path in sorted(Path("/sys/class/block").iterdir()):
            if (path / "partition").exists() or not re.fullmatch(r"(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+)", path.name):
                continue
            sectors = read(path / "size")
            result.append(device("/dev/" + path.name, read(path / "device/model") or path.name, [group("drive_identity", "block sysfs (OS)",
                model=read(path / "device/model"), serial=read(path / "device/serial"),
                firmware=read(path / "device/firmware_rev") or read(path / "device/rev"),
                capacity=size(int(sectors) * 512) if sectors.isdigit() else None,
                device_path="/dev/" + path.name, logical_sector=with_unit(read(path / "queue/logical_block_size"), "B"))], notices=["smart_os_only"]))
    smartctl = find_tool("smartctl")
    if smartctl:
        scan, _ = reader.json([smartctl, "--scan", "-j"])
        scan_items = scan.get("devices", []) if isinstance(scan, dict) else []
        known = {d["id"] for d in result}
        for row in scan_items[:32]:
            path = row.get("name", "")
            if re.fullmatch(r"/dev/(?:sd[a-z]+|nvme\d+(?:n\d+)?|disk\d+|pd\d+)", path) and path not in known:
                # A namespace and its NVMe controller refer to the same drive.
                if any(re.fullmatch(re.escape(path) + r"n\d+", existing) for existing in known):
                    continue
                result.append(device(path, path, []))
                known.add(path)
        for index, dev in enumerate(result[:32]):
            data, code = reader.json([smartctl, "-a", "-j", "-n", "standby", dev["id"]])
            if isinstance(data, dict) and any(key in data for key in ("smart_status", "ata_smart_attributes", "nvme_smart_health_information_log")):
                measured = smart_device(data, dev["id"], code)
                measured["groups"].extend(dev["groups"])
                result[index] = measured
            else:
                messages = " ".join(str(m.get("string", "")) for m in data.get("smartctl", {}).get("messages", []) if isinstance(m, dict)) if isinstance(data, dict) else ""
                denied = any(s in messages.lower() for s in ("permission denied", "operation not permitted", "access is denied", "requires root", "administrator"))
                dev["notices"].append("hardware_permission_denied" if denied else "smart_read_error")
    else:
        for dev in result:
            dev["notices"].append("smartctl_missing")
    return result


def collect(kind, reader=None):
    if kind not in KINDS:
        raise ValueError("unknown hardware tool")
    reader = reader or Reader()
    result = {"schema": 1, "kind": kind, "collected_at": datetime.now(timezone.utc).isoformat(),
              "devices": [], "sources": reader.sources, "notices": ["hardware_readonly"]}
    try:
        result["devices"] = {"cpu": cpu_info, "gpu": gpu_info, "dram": dram_info, "disk": disk_info}[kind](reader)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, psutil.Error):
        result["notices"].append("hardware_read_failed")
    if not result["devices"]:
        result["notices"].append("hardware_no_devices")
    if any(s.get("status") == "permission_denied" for s in reader.sources):
        result["notices"].append("hardware_permission_denied")
    result["access"] = "administrator" if reader.privileged else "standard"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=KINDS, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(collect(args.kind), ensure_ascii=True, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
