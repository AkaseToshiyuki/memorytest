"""Evidence reader: raw timing/counts, qualified candidates and lower bounds."""
from sample_statistics import summarize
from statistics import median
from frontend_width_analysis import counter_sources, samples, read_width_scan, sustained_widths, stable as stable_samples

PROBES = {"nop_stream": 64, "iadd_dep": 32, "iadd_ind8": 32, "imul_dep": 32, "imul_ind8": 32,
          "fadd_dep": 32, "fadd_ind8": 32, "fmul_dep": 32, "fmul_ind8": 32,
          "load_chain_32k": 256, "write_ordered_32k": 256, "copy_ordered_64k": 256}


def read_architecture(data):
    if not isinstance(data, dict) or (data.get("schema_version"), data.get("protocol")) not in {
            (1, "architecture-probes-v1"), (2, "architecture-probes-v2"), (3, "architecture-probes-v3")}:
        raise ValueError("unsupported architecture probe protocol")
    if data.get("isa") not in {"x86_64-sse2", "aarch64-fp64"}:
        raise ValueError("unsupported instruction probe ISA")
    if data.get("timer") != "monotonic-wall-time" or data.get("cache_source") != "OS-reported-not-probed":
        raise ValueError("unsupported timing or metadata source")
    if type(data.get("sample_count")) is not int or data["sample_count"] != 10:
        raise ValueError("ten ordered samples required")
    target = data.get("sample_target_ns")
    if type(target) is not int or not 1 <= target <= 10_000_000_000:
        raise ValueError("invalid sample target")
    policy, cpu = data.get("placement"), data.get("cpu_id")
    if type(cpu) is not int or policy not in {"linux-pinned-one-logical-cpu", "macos-qos-user-initiated", "os-scheduled-single-thread"}:
        raise ValueError("invalid placement")
    if not (0 <= cpu < 1024 if policy.startswith("linux") else cpu == -1):
        raise ValueError("incorrect CPU pinning claim")
    caches = data.get("reported_cache_bytes")
    if not isinstance(caches, dict) or set(caches) != {"l1i", "l1d", "l2", "l3"} or any(
            type(value) is not int or not 0 <= value <= 2**50 for value in caches.values()):
        raise ValueError("invalid OS cache metadata")
    records = data.get("records")
    if not isinstance(records, list) or len(records) != len(PROBES):
        raise ValueError("missing architecture measurements")
    metrics = {}
    for record in records:
        if not isinstance(record, dict): raise ValueError("invalid record")
        name = record.get("probe")
        if name not in PROBES or name in metrics or record.get("validated") is not True:
            raise ValueError("unexpected or unvalidated probe")
        blocks, operations = record.get("blocks"), record.get("operations_per_block")
        if type(blocks) is not int or not 1 <= blocks <= 2**30 or type(operations) is not int or operations != PROBES[name]:
            raise ValueError("incorrect explicit work count")
        elapsed = record.get("elapsed_ns")
        if not isinstance(elapsed, list) or len(elapsed) != 10 or any(
                type(ns) is not int or ns <= 0 or ns < target // 2 or ns > 2**64 - 1 for ns in elapsed):
            raise ValueError("invalid timing samples")
        stats = summarize(elapsed)
        ns = stats["median"] / (blocks * operations)
        spread = (max(elapsed) - min(elapsed)) * 100 / stats["raw_median"]
        metrics[name] = {"ns": ns, "gops": 1 / ns, "statistics": stats,
                         "spread_pct": spread, "stable": not stats["unstable"] and spread <= 15}
    frontend = read_frontend(data.get("frontend"), data["isa"], policy) if data["schema_version"] >= 2 else None
    width_scan = None
    if data["schema_version"] == 3:
        if data.get("frontend", {}).get("protocol") != "frontend-static-v2": raise ValueError("missing decoder-path evidence")
        width_scan = read_width_scan(data.get("width_scan"), data["isa"], policy)
        frontend["fetch_width"] = width_scan["fetch_width"]
        frontend["decode_width"], frontend["retirement_width"] = sustained_widths(frontend, data["isa"], policy, caches["l1i"])
    return {"metrics": metrics, "reported_cache_bytes": caches, "placement": policy, "cpu_id": cpu,
            "protocol": data["protocol"], "frontend": frontend, "width_scan": width_scan,
            "isa": data["isa"], "sample_target_ns": target,
            "stable": all(m["stable"] for m in metrics.values()) and (frontend is None or frontend["stable"]) and
                      (width_scan is None or width_scan["stable"])}


BTB_COUNTS = (128, 256, 512, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192, 12288, 16384, 24576, 32768)
CODE_BYTES = tuple(128 * 2**i for i in range(12))


def btb_transitions(records):
    """Conservative, empirical transition candidates, not physical BTB entries.

    Require a persistent >=30% step above the preceding three-point plateau,
    low sample spread, and an identical count interval at >=2 spacings.
    An instruction-footprint knee moves with spacing and is rejected.
    """
    votes = {}
    for stride in (8, 16, 32):
        points = sorted((r for r in records if r["kind"] == "btb" and r["stride"] == stride), key=lambda r: r["count"])
        for i in range(3, len(points)-1):
            before, after = points[i-3:i], points[i:i+2]
            if not all(p["stable"] for p in before + after): continue
            baseline = median(p["ns"] for p in before)
            if max(p["ns"] for p in before) > 1.15 * min(p["ns"] for p in before): continue
            if min(p["ns"] for p in after) < baseline * 1.30: continue
            interval = (points[i-1]["count"], points[i]["count"])
            votes.setdefault(interval, []).append(stride)
    return [{"lower": lo, "upper": hi, "spacings": spacings}
            for (lo, hi), spacings in sorted(votes.items()) if len(spacings) >= 2]


def read_frontend(data, isa, placement):
    if not isinstance(data, dict) or data.get("protocol") not in {"frontend-static-v1", "frontend-static-v2"}:
        raise ValueError("missing front-end evidence")
    source, decode_source = counter_sources(data, placement)
    if decode_source != "unavailable" and not isa.startswith("x86"): raise ValueError("incorrect decoder event ISA")
    target = data.get("sample_target_ns")
    if type(target) is not int or not 1 <= target <= 10_000_000: raise ValueError("invalid front-end target")
    widths = (1, 4, 8) if isa.startswith("x86") else (4,)
    expected = {("btb", n, s) for n in BTB_COUNTS for s in (8, 16, 32)} | {
        ("nop", size // width, width) for size in CODE_BYTES for width in widths}
    raw = data.get("records")
    if not isinstance(raw, list) or len(raw) != len(expected): raise ValueError("incomplete front-end scan")
    records, seen = [], set()
    for row in raw:
        if not isinstance(row, dict): raise ValueError("invalid front-end record")
        kind, count, stride, blocks = (row.get(k) for k in ("kind", "count", "stride", "blocks"))
        if any(type(n) is not int for n in (count, stride, blocks)) or not 1 <= blocks <= 2**30:
            raise ValueError("invalid front-end work count")
        key = (kind, count, stride)
        if key not in expected or key in seen or row.get("code_bytes") != count * stride:
            raise ValueError("invalid front-end code footprint")
        seen.add(key)
        elapsed, cycles, instructions, mite = samples(row, source, decode_source, max(1, target//2), data["protocol"] == "frontend-static-v2")
        stats = summarize(elapsed)
        stable = not stats["unstable"] and (max(elapsed)-min(elapsed))/stats["raw_median"] <= .15
        cycle_ok = all(cycles) and all(i >= count * blocks for i in instructions)
        ipc = median(count * blocks / c for c in cycles) if cycle_ok else None
        if cycle_ok:
            ratios = [count * blocks / c for c in cycles]
            cycle_stats = summarize(ratios)
            cycle_ok = not cycle_stats["unstable"] and (max(ratios)-min(ratios))/median(ratios) <= .15
        records.append({"kind": kind, "count": count, "stride": stride, "code_bytes": count*stride,
            "ns": stats["median"] / (count*blocks), "stable": stable,
            "instructions_per_cycle": ipc, "counter_stable": bool(cycle_ok),
            "supply_floor": min(count*blocks/c for c in cycles) if cycle_ok else None,
            "decoder_qualified": bool(cycle_ok and min(mite) >= count*blocks*.9 and stable_samples([m/c for m, c in zip(mite, cycles)])),
            "mite_per_cycle": median(m/c for m, c in zip(mite, cycles)) if cycle_ok and all(mite) else None,
            "mite_floor": min(m/c for m, c in zip(mite, cycles)) if cycle_ok and all(mite) else None,
            "bytes_per_cycle": ipc * stride if ipc is not None else None})
    supply = [r for r in records if r["kind"] == "nop" and r["stable"] and r["counter_stable"]]
    return {"records": records, "counter_source": source, "decode_counter_source": decode_source, "sample_target_ns": target,
            "btb_transitions": btb_transitions(records), "stable": all(r["stable"] for r in records),
            "peak_supply_ipc": max((r["instructions_per_cycle"] for r in supply), default=None),
            "fetch_width": None, "decode_width": None, "retirement_width": None}
