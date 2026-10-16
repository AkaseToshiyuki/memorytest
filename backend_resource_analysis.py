"""Validate complete raw scans; infer effective windows, never physical entries."""
from statistics import median
from sample_statistics import summarize
from frontend_width_analysis import counter_sources, samples, stable as stable_samples

SMALL = (0, 8, 16, 24, 32, 48, 64, 80, 96, 128, 160, 192, 224, 256, 320, 384, 448, 512)
LARGE = SMALL + (640, 768, 1024, 1280, 1536, 2048)
FAMILIES = {"rob_nop": LARGE + (2560, 3072, 3584, 4096), "prf_i32": LARGE,
            "prf_i64": LARGE, "prf_vec128": LARGE, **dict.fromkeys(
                ("sched_int", "sched_vector", "load_ready", "load_wait", "store_ready", "store_wait"), SMALL)}
ARM_SCALAR = {"prf_fp32": LARGE, "prf_fp64": LARGE, "sched_fp32": SMALL, "sched_fp64": SMALL}


def resource_families(isa):
    return {**FAMILIES, **(ARM_SCALAR if isa == "aarch64-fp64" else {})}


BTB_COUNTS = (16, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096)


def timing(row):
    blocks, elapsed = row.get("blocks"), row.get("elapsed_ns")
    if type(blocks) is not int or not 1 <= blocks <= 2**30 or not isinstance(elapsed, list) or len(elapsed) != 10 or any(
            type(n) is not int or not 1 <= n < 2**64 for n in elapsed):
        raise ValueError("invalid resource timing samples")
    stats = summarize(elapsed)
    return stats["median"] / blocks, not stats["unstable"] and (max(elapsed)-min(elapsed))/median(elapsed) <= .15


def window_candidate(points):
    """An abrupt, sustained loss of overlap at the same bracket for both anchors.

    Smooth throughput saturation, failed baseline overlap and depth-sensitive
    knees do not identify a resource window. This tests identifiability, not a
    known chip's advertised capacity. Returned counts exclude loop/anchor work.
    """
    intervals = []
    for depth in (1, 2):
        curve = sorted((p for p in points if p["depth"] == depth), key=lambda p: p["count"])
        if curve and not curve[0]["stable"]: return {"status": "unstable"}
        if not curve or curve[0]["count"] != 0 or curve[0]["ratio"] >= .78:
            return {"status": "overlap_unverified"}
        found = []
        for i in range(3, len(curve)-1):
            before, after = curve[i-3:i], curve[i:i+2]
            if not all(p["stable"] for p in before+after): continue
            baseline = median(p["ratio"] for p in before)
            if max(p["ratio"] for p in before)-min(p["ratio"] for p in before) > .12: continue
            if min(p["ratio"] for p in after) < max(.85, baseline+.15): continue
            found.append((curve[i-1]["count"], curve[i]["count"]))
        if len(found) != 1: return {"status": ("unstable" if any(not p["stable"] for p in curve) else "no_knee") if not found else "ambiguous"}
        intervals.append(found[0])
    if intervals[0] != intervals[1]: return {"status": "depth_sensitive"}
    return {"status": "candidate", "lower": intervals[0][0], "upper": intervals[0][1],
            "unit": "filler-instructions", "basis": "dual-ring-serial-control"}


def read_resources(data, isa="x86_64-sse2"):
    if not isinstance(data, dict) or data.get("protocol") != "dual-ring-resource-v1":
        raise ValueError("missing backend resource scan")
    size, target = data.get("ring_bytes"), data.get("sample_target_ns")
    if type(size) is not int or not 128 <= size <= 512*1024**2 or size % 64 or data.get("line_bytes") != 64 or data.get("scratch_bytes") != 32832:
        raise ValueError("invalid resource memory layout")
    if type(target) is not int or not 1 <= target <= 2_000_000: raise ValueError("invalid resource target")
    if data.get("status") == "allocation_failed" and data.get("records") == []:
        return {"status": "allocation_failed", "records": [], "candidates": {}, "stable": False, "ring_bytes": size}
    if data.get("status") != "measured": raise ValueError("invalid resource scan status")
    families = resource_families(isa)
    expected = {(f, d, n, s) for f, counts in families.items() for n in counts for d in (1, 2) for s in (0, 1)}
    raw = data.get("records")
    if not isinstance(raw, list) or len(raw) != len(expected): raise ValueError("incomplete resource scan")
    seen = {}
    for row in raw:
        if not isinstance(row, dict): raise ValueError("invalid resource record")
        key = tuple(row.get(k) for k in ("family", "depth", "count", "serial"))
        if not isinstance(key[0], str) or any(type(n) is not int for n in key[1:]) or key not in expected or key in seen:
            raise ValueError("invalid resource work count")
        ns, stable = timing(row)
        retries = row.get("duration_retries", 0)
        if type(retries) is not int or not 0 <= retries <= 2: raise ValueError("invalid resource duration retries")
        seen[key] = dict(row, ns=ns, stable=stable)
    records = []
    for family, counts in families.items():
        for depth in (1, 2):
            for count in counts:
                a, b = (seen[family, depth, count, s] for s in (0, 1))
                if a["blocks"] != b["blocks"] or a.get("duration_retries", 0) != b.get("duration_retries", 0) or min(x+y for x, y in zip(a["elapsed_ns"], b["elapsed_ns"])) < target//2:
                    raise ValueError("unmatched resource pair")
                ratios = [x/y for x, y in zip(a["elapsed_ns"], b["elapsed_ns"])]
                stats = summarize(ratios)
                stable = a["stable"] and b["stable"] and not stats["unstable"] and (max(ratios)-min(ratios))/median(ratios) <= .15
                records.append({"family": family, "depth": depth, "count": count, "ratio": median(ratios),
                                "parallel_ns": a["ns"], "serial_ns": b["ns"], "stable": stable})
    candidates = {family: window_candidate([r for r in records if r["family"] == family]) for family in families}
    # A result-writing or dependent stream must lose overlap before its ready
    # control. Otherwise the shared ROB/frontend/dispatch limit is unresolved.
    controls = {"prf_i32": "rob_nop", "prf_i64": "rob_nop", "prf_vec128": "rob_nop",
                "sched_int": "prf_i64", "sched_vector": "prf_vec128", "load_wait": "load_ready",
                "store_wait": "store_ready", "load_ready": "rob_nop", "store_ready": "rob_nop"}
    if isa == "aarch64-fp64": controls.update(prf_fp32="rob_nop", prf_fp64="rob_nop", sched_fp32="prf_fp32", sched_fp64="prf_fp64")
    for family, control in controls.items():
        candidate = candidates[family]
        if candidate["status"] != "candidate": continue
        for depth in (1, 2):
            reference = [p for p in records if p["family"] == control and p["depth"] == depth and p["count"] >= candidate["upper"]][:2]
            if len(reference) < 2 or not all(p["stable"] and p["ratio"] < .8 for p in reference):
                candidates[family] = {"status": "shared_limit", "observed_interval": [candidate["lower"], candidate["upper"]]}
                break
    return {"status": "measured", "records": records, "candidates": candidates,
            "stable": all(r["stable"] for r in records), "ring_bytes": size}


def read_btb_layout(data):
    if not isinstance(data, dict) or data.get("protocol") != "btb-layout-v1" or data.get("loop_control_branches") != 1:
        raise ValueError("missing BTB layout controls")
    target = data.get("sample_target_ns")
    if type(target) is not int or not 1 <= target <= 2_000_000: raise ValueError("invalid BTB layout target")
    expected = {(n, s, o) for n in BTB_COUNTS for s in (4, 8, 16, 32, 64) for o in (0, 32)}
    rows = data.get("records")
    if not isinstance(rows, list) or len(rows) != len(expected): raise ValueError("incomplete BTB layout scan")
    records, seen = [], set()
    for row in rows:
        if not isinstance(row, dict): raise ValueError("invalid BTB layout record")
        key = tuple(row.get(k) for k in ("count", "stride", "offset"))
        if any(type(n) is not int for n in key) or key not in expected or key in seen:
            raise ValueError("invalid BTB layout count")
        count, stride, offset = key
        if row.get("actual_offset") != offset or row.get("code_bytes") != count*stride:
            raise ValueError("invalid BTB layout geometry")
        ns, stable = timing(row)
        if min(row["elapsed_ns"]) < target//2: raise ValueError("short BTB sample")
        seen.add(key)
        records.append({"count": count, "stride": stride, "offset": offset, "ns": ns/count, "stable": stable})
    votes = {}
    for stride in (4, 8, 16, 32, 64):
        for offset in (0, 32):
            curve = sorted((r for r in records if r["stride"] == stride and r["offset"] == offset), key=lambda r: r["count"])
            for i in range(2, len(curve)-1):
                before, after = curve[i-2:i], curve[i:i+2]
                if not all(p["stable"] for p in before+after): continue
                if max(p["ns"] for p in before) > 1.15*min(p["ns"] for p in before): continue
                if min(p["ns"] for p in after) < 1.30*median(p["ns"] for p in before): continue
                votes.setdefault((stride, curve[i-1]["count"], curve[i]["count"]), set()).add(offset)
    candidates = [{"stride": s, "lower": lo, "upper": hi, "offsets": sorted(offsets), "status": "layout_transition"}
                  for (s, lo, hi), offsets in sorted(votes.items()) if len(offsets) == 2]
    # Equal byte-footprint transitions at different strides are cache/index
    # ambiguous. Keep them inspectable, without assigning a BTB entry count.
    for candidate in candidates:
        if any(other["stride"] != candidate["stride"] and other["upper"]*other["stride"] == candidate["upper"]*candidate["stride"] for other in candidates):
            candidate["status"] = "footprint_or_index"
    return {"records": records, "transitions": candidates, "stable": all(r["stable"] for r in records)}


COMPETITION_SINGLE = ("iadd", "imul", "fadd", "fmul", "load", "store")
COMPETITION_MIX = ("iadd_imul", "fadd_fmul", "iadd_fadd", "load_store", "iadd_load", "fadd_load")


def read_competition(data, placement):
    if not isinstance(data, dict) or data.get("protocol") != "mixed-throughput-v1" or data.get("chains_per_family") != 4:
        raise ValueError("missing execution competition scan")
    source, decode = counter_sources(data, placement)
    if decode != "unavailable": raise ValueError("unexpected competition decoder event")
    target = data.get("sample_target_ns")
    if type(target) is not int or not 1 <= target <= 5_000_000: raise ValueError("invalid competition target")
    expected = {(f, n) for f in COMPETITION_SINGLE+COMPETITION_MIX for n in (32, 64)}
    rows = data.get("records")
    if not isinstance(rows, list) or len(rows) != len(expected): raise ValueError("incomplete competition scan")
    records, seen = [], set()
    for row in rows:
        if not isinstance(row, dict): raise ValueError("invalid competition record")
        key = row.get("family"), row.get("unroll")
        if not isinstance(key[0], str) or type(key[1]) is not int or key not in expected or key in seen:
            raise ValueError("invalid competition payload")
        ns, time_stable = timing(row)
        elapsed, cycles, instructions, _ = samples(row, source, decode, max(1, target//2), False)
        ops = row["blocks"]*key[1]
        if any(c and i < ops for c, i in zip(cycles, instructions)): raise ValueError("retired count below competition work")
        ipc = [ops/c for c in cycles] if all(cycles) else []
        counter_stable = stable_samples(ipc)
        records.append({"family": key[0], "unroll": key[1], "ns": ns/key[1], "stable": time_stable,
                        "instructions_per_cycle": median(ipc) if ipc else None, "counter_stable": counter_stable,
                        "allocation_floor": min(ipc) if counter_stable and time_stable else None})
        seen.add(key)
    mixtures = []
    for family in COMPETITION_MIX:
        first, second = family.split("_")
        gains, valid = [], True
        for unroll in (32, 64):
            a, b, mixed = (next(r for r in records if r["family"] == name and r["unroll"] == unroll) for name in (first, second, family))
            gains.append((a["ns"]+b["ns"])/(2*mixed["ns"]))
            valid = valid and all(r["stable"] for r in (a, b, mixed))
        consistent = max(gains)/min(gains) <= 1.10
        mixtures.append({"family": family, "gain": median(gains), "unroll_gains": gains,
                         "status": "measured" if valid and consistent else "unstable" if not valid else "unroll_sensitive"})
    # Non-eliminable result writes provide an allocation-throughput floor only.
    # Four-chain latency, execution and retire bottlenecks can all lower it.
    floors = []
    for family in ("iadd", "imul", "fadd", "fmul", "iadd_imul", "fadd_fmul", "iadd_fadd"):
        values = [r["allocation_floor"] for r in records if r["family"] == family]
        if len(values) == 2 and all(values) and max(values)/min(values) <= 1.10: floors.append(min(values))
    rename = {"status": "lower_bound", "value": max(floors), "unit": "writes/cycle", "basis": "non-eliminable-result-throughput"} if floors else None
    return {"records": records, "mixtures": mixtures, "rename_width": rename, "counter_source": source,
            "stable": all(r["stable"] for r in records)}
