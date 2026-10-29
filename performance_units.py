"""Decimal operation-throughput units; stored timing and score inputs stay unchanged."""
import math


def throughput_scale(gops_values):
    """Use TOPS only when every positive value on a shared scale reaches 0.1."""
    positive = [value for value in gops_values if isinstance(value, (int, float))
                and not isinstance(value, bool) and math.isfinite(value) and value > 0]
    return (0.001, "TOPS") if positive and min(positive) >= 100 else (1.0, "GOPS")


def throughput_value(gops):
    factor, unit = throughput_scale([gops])
    return gops * factor if gops is not None else None, unit


def format_throughput(gops):
    value, unit = throughput_value(gops)
    return f"{value:.6g} {unit}"


def row_gops(headers, row):
    """Read current rate columns and old op/s or ns/op archives into GOPS."""
    columns = {header.lower().replace(" ", ""): index for index, header in enumerate(headers)}
    for names, factor, reciprocal in (
        (("rate(tops)", "tops"), 1000.0, False),
        (("rate(gops)", "gops", "rate(gop/s)", "gop/s", "gflop/s"), 1.0, False),
        (("ops/sec", "opspersec", "op/s"), 1e-9, False),
        (("ns/op", "ns_per_op", "nsec/op"), 1.0, True),
    ):
        for name in names:
            if name not in columns or columns[name] >= len(row):
                continue
            raw = row[columns[name]]
            if isinstance(raw, bool):
                return None
            try:
                value = float(str(raw).strip().replace(",", ""))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value) or value < 0 or (reciprocal and value == 0):
                return None
            result = factor / value if reciprocal else value * factor
            return result if math.isfinite(result) else None
    return None
