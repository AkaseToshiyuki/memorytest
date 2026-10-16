"""Lossless tabular views of smartctl's structured device information.

Do not apply ATA normalized thresholds to NVMe/SCSI, infer vendor health, or
truncate 128-bit counters. JSON is bounded by the caller, not a silent row cap.
"""
import re


def exact_value(data, key, default=None):
    return data.get(key + "_s", data.get(key, default))


def nvme_gb(data, key):
    """NVMe data units are 1,000 * 512 bytes, NOT host commands or sectors.

    Keep the 128-bit counter exact through conversion, round half up to 0.01
    decimal GB using integer arithmetic, and leave the raw log untouched.
    Reject approximate JSON floats without --json=v's exact string companion.
    """
    value = exact_value(data, key)
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,39}", value):
        number = int(value)
    else:
        return None
    if not 0 <= number < 2**128:
        return None
    hundredths = (number * 512_000 * 100 + 500_000_000) // 1_000_000_000
    return f"{hundredths // 100:,}.{hundredths % 100:02d} GB"


def leaves(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            # smartctl --json=v provides exact decimal strings and little-endian
            # byte representations of the same counter. Show it once, exactly.
            if key.endswith(("_s", "_le")) and key.rsplit("_", 1)[0] in value:
                continue
            yield from leaves(exact_value(value, key), f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from leaves(child, f"{path}[{index}]")
    else:
        yield path, value


def unit(path):
    key = path.rsplit(".", 1)[-1]
    if key in {"temperature", "warning_composite_temperature_threshold", "critical_composite_temperature_threshold"} or key.startswith("temperature_sensors["):
        return "°C"
    if key in {"available_spare", "available_spare_threshold", "percentage_used"}:
        return "%"
    if key == "power_on_hours":
        return "h"
    if key in {"warning_temp_time", "critical_comp_time"}:
        return "min"
    if key in {"thermal_temp_1_time", "thermal_temp_2_time"}:
        return "s"
    if key in {"data_units_read", "data_units_written"}:
        return "512,000 B"
    if key == "controller_busy_time":
        return "min"
    return None


def details(title, data, source):
    rows, private = [], []
    for path, value in leaves(data):
        if re.search(r"serial|wwn|eui|nguid|uuid", path, re.I):
            private.append([len(rows), 1])
        rows.append([path, value, unit(path)])
    return {"title": title, "columns": ["parameter", "value", "unit"],
            "rows": rows, "private_cells": private, "source": source}


def smart_tables(data):
    tables = []
    attributes = data.get("ata_smart_attributes", {}).get("table", [])
    if attributes:
        rows = []
        for row in attributes:
            raw = row.get("raw", {})
            raw_value = exact_value(raw, "value")
            number = int(raw_value) if isinstance(raw_value, (str, int)) and str(raw_value).isdigit() else None
            rows.append([f'{row["id"]:02X}' if isinstance(row.get("id"), int) else row.get("id"),
                         row.get("name"), row.get("value"), row.get("worst"), row.get("thresh"),
                         raw.get("string", raw_value), row.get("when_failed"),
                         f"{number:012X}" if number is not None else None,
                         row.get("flags", {}).get("string")])
        tables.append({"title": "ata_attributes", "columns": ["id_hex", "attribute", "current", "worst",
            "threshold", "raw_value", "when_failed", "raw_hex", "flags"], "rows": rows,
            "source": "smartctl ATA SMART attributes; normalized values and vendor raw representation"})
    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        tables.append(details("nvme_health", nvme, "smartctl NVMe SMART/Health log"))
    # Preserve every other structured field, including flags, capability bits,
    # SCSI counters, ATA extended logs and future/vendor-specific fields. This
    # also retains the full ATA raw/flag representation beyond the main table.
    extra = {key: value for key, value in data.items() if key not in {
        "smartctl", "json_format_version", "local_time", "nvme_smart_health_information_log"}}
    if extra:
        tables.append(details("smart_details", extra, "smartctl extended device / SMART information"))
    # A command/log error is independent of the health result. Preserve its
    # evidence without exposing command-line arguments or known serials.
    control = data.get("smartctl", {})
    if isinstance(control.get("exit_status"), int) and control["exit_status"] & 7:
        messages = [str(m.get("string", "")) for m in control.get("messages", []) if isinstance(m, dict)]
        serial = str(data.get("serial_number") or "")
        if serial:
            messages = [message.replace(serial, "[redacted]") for message in messages]
        tables.append(details("smart_read_details", {"exit_status": control["exit_status"],
                      "messages": messages}, "smartctl collection diagnostics"))
    return tables
