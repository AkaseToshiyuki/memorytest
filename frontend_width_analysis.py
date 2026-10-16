"""Page-boundary inference and qualified sustained front-end measurements.

No clock-frequency conversion. A model fit is a candidate, not a count of
physical decoders. The NOP plateau on macOS/Arm is explicitly conditional on
the instruction-supply path; x86 requires measured legacy-decoder traffic.
"""
from math import ceil, sqrt
from statistics import median
from sample_statistics import summarize


def counter_sources(data, placement):
    source = data.get("counter_source")
    expected = "linux-perf-thread-user" if placement.startswith("linux") else (
        "macos-rusage-process-user-and-kernel" if placement.startswith("macos") else "unavailable")
    decode = data.get("decode_counter_source", "unavailable")
    if source not in {"unavailable", expected} or decode not in {"unavailable", "intel-skl-kbl-idq-mite-uops"}:
        raise ValueError("invalid front-end counter source")
    if decode != "unavailable" and source != "linux-perf-thread-user":
        raise ValueError("decoder event requires Linux hardware counters")
    return source, decode


def samples(row, source, decode, minimum_ns=1, require_mite=True):
    elapsed, cycles, instructions = (row.get(k) for k in ("elapsed_ns", "cycles", "instructions"))
    mite = row.get("mite_uops") if require_mite else [0]*10
    for values in (elapsed, cycles, instructions, mite):
        if not isinstance(values, list) or len(values) != 10 or any(type(n) is not int or not 0 <= n < 2**64 for n in values):
            raise ValueError("invalid front-end sample array")
    if min(elapsed) < minimum_ns or any(bool(c) != bool(i) for c, i in zip(cycles, instructions)):
        raise ValueError("invalid time or incomplete counter pair")
    if source == "unavailable" and any(cycles + instructions + mite):
        raise ValueError("hardware counts without a source")
    if decode == "unavailable" and any(mite): raise ValueError("decoder counts without a source")
    if any(m and not c for m, c in zip(mite, cycles)): raise ValueError("decoder count without cycles")
    return elapsed, cycles, instructions, mite


def stable(values):
    return bool(values) and min(values) > 0 and not summarize(values)["unstable"] and (max(values)-min(values))/median(values) <= .15


def read_width_scan(data, isa, placement):
    if not isinstance(data, dict) or data.get("protocol") != "page-boundary-width-v1":
        raise ValueError("missing width scan")
    source, decode = counter_sources(data, placement)
    if decode != "unavailable" and not isa.startswith("x86"): raise ValueError("incorrect decoder event ISA")
    page, target = data.get("page_bytes"), data.get("sample_target_ns")
    if type(page) is not int or page not in (4096, 8192, 16384, 32768, 65536): raise ValueError("invalid page size")
    if type(target) is not int or not 1 <= target <= 10_000_000: raise ValueError("invalid width target")
    raw = data.get("records")
    if not isinstance(raw, list) or len(raw) != 122: raise ValueError("incomplete paired width scan")
    points, seen, repetitions, times = [], set(), {}, {}
    for row in raw:
        if not isinstance(row, dict): raise ValueError("invalid width row")
        count, crossed, blocks = (row.get(k) for k in ("count", "crossed", "blocks"))
        if any(type(n) is not int for n in (count, crossed, blocks)) or not 4 <= count <= 64 or crossed not in (0, 1) or not 1 <= blocks <= 2**30:
            raise ValueError("invalid width work count")
        if (count, crossed) in seen: raise ValueError("duplicate width point")
        seen.add((count, crossed))
        if row.get("page_offset") != (page-4 if crossed else 0) or row.get("code_bytes") != count*4 + int(isa.startswith("x86")):
            raise ValueError("incorrect loop layout")
        if count in repetitions and blocks != repetitions[count]: raise ValueError("unpaired work counts")
        repetitions[count] = blocks
        elapsed, cycles, instructions, mite = samples(row, source, decode)
        times[count, crossed] = elapsed
        if (count, 1-crossed) in times and any(a+b < max(1, target//4) for a, b in zip(elapsed, times[count, 1-crossed])):
            raise ValueError("short paired width sample")
        if all(cycles) and any(i < blocks*count for i in instructions): raise ValueError("retired count below explicit work")
        ns = [v / blocks for v in elapsed]
        cpi = [v / blocks for v in cycles] if all(cycles) else []
        points.append({"count": count, "crossed": crossed, "code_bytes": row["code_bytes"],
            "ns_per_iteration": median(ns), "cycles_per_iteration": median(cpi) if cpi else None,
            "stable": stable(ns), "counter_stable": stable(cpi),
            "decoder_qualified": bool(all(mite) and min(mite) >= blocks*(count-1)*.9)})
    # An available but noisy/invalid PMU must not silently fall back to a
    # wall-time model that could contradict the measured cycle cost.
    use_cycles = source != "unavailable"
    fit = fetch_candidate(points, use_cycles)
    # The x86 DSB/LSD can produce a staircase without accessing the fetch path.
    qualified = isa == "aarch64-fp64" or all(p["decoder_qualified"] for p in points)
    if not qualified: fit = None
    return {"records": points, "fetch_width": fit, "counter_source": source,
        "decode_counter_source": decode, "page_bytes": page, "sample_target_ns": target,
        "status": "candidate" if fit else "path_unverified" if not qualified else "no_fit",
        "stable": all(p["stable"] for p in points)}


def fetch_candidate(points, use_cycles=False):
    """Fit both curves to max(fetch packets, sustained instruction supply).

    Fetch packets are ceil(bytes/W), or 1+ceil((bytes-4)/W) across a page.
    The shared supply constraint is n/D. A shared nonnegative setup cost and
    time scale are fitted; no GHz assumption is needed for wall-time curves.
    Reject flat/linear curves, weak page penalties and ambiguous widths.
    """
    good = [p for p in points if p["stable"] and (not use_cycles or p["counter_stable"])]
    if len(good) < 110: return None
    field = "cycles_per_iteration" if use_cycles else "ns_per_iteration"
    paired = {(p["count"], p["crossed"]): p[field] for p in good}
    penalties = [paired[n, 1]-paired[n, 0] for n in range(4, 65) if (n, 1) in paired and (n, 0) in paired]
    values = [p[field] for p in good]
    short = [p[field] for p in good if p["count"] <= 16]
    if max(values) < 1.5*min(values) or max(penalties, default=0) < .12*median(short): return None
    average = sum(values)/len(values)
    candidates = []
    for width in (16, 32, 64, 128):
        best = None
        for quarter in range(8, 81):
            supply = quarter/4
            model = [max((1+ceil((p["code_bytes"]-4)/width)) if p["crossed"] else ceil(p["code_bytes"]/width),
                         p["count"]/supply) for p in good]
            center = sum(model)/len(model)
            variance = sum((x-center)**2 for x in model)
            if not variance: continue
            scale = sum((x-center)*(y-average) for x, y in zip(model, values))/variance
            offset = average-scale*center
            if scale <= 0 or offset < -.10*scale or offset > scale: continue
            if use_cycles and not .8 <= scale <= 1.2: continue
            error = sqrt(sum((y-(offset+scale*x))**2 for x, y in zip(model, values))/len(values))/average
            if best is None or error < best[0]: best = error, scale, offset
        if best: candidates.append((best[0], width))
    candidates.sort()
    if len(candidates) < 2 or candidates[0][0] > .06 or candidates[1][0] < max(.025, candidates[0][0]*1.5): return None
    error, width = candidates[0]
    # Require an observed page-end step at a model-independent packet boundary.
    boundary = (width+4-(points[0]["code_bytes"]-4*points[0]["count"]))//4 + 1
    if not all((n, 1) in paired for n in (boundary-1, boundary)): return None
    if paired[boundary, 1] < 1.12*paired[boundary-1, 1]: return None
    return {"value": width, "unit": "B/cycle", "status": "candidate", "fit_error": error,
            "basis": "hardware-cycles" if use_cycles else "paired-wall-time-shape"}


def sustained_widths(frontend, isa, placement, l1i):
    rows = [r for r in frontend["records"] if r["kind"] == "nop" and r["stable"] and r["counter_stable"]]
    retirement = max((r["supply_floor"] for r in rows), default=None)
    decode = None
    # >=3 adjacent footprints: short-loop buffers and a single lucky sample
    # cannot establish a sustained plateau. Keep a 2x L1I capacity margin.
    for stride in sorted({r["stride"] for r in rows}):
        group = sorted((r for r in rows if r["stride"] == stride and 4096 <= r["code_bytes"] <= l1i/2), key=lambda r: r["code_bytes"])
        for i in range(len(group)-2):
            three = group[i:i+3]
            if [r["code_bytes"] for r in three] != [three[0]["code_bytes"]*2**j for j in range(3)]: continue
            pmu = all(r.get("decoder_qualified") for r in three)
            arm_candidate = isa == "aarch64-fp64" and placement.startswith("macos")
            if not pmu and not arm_candidate: continue
            values = [r["mite_per_cycle"] if pmu else r["instructions_per_cycle"] for r in three]
            if max(values) > min(values)*1.12: continue
            value = min(r["mite_floor"] if pmu else r["supply_floor"] for r in three)
            if decode is None or value > decode["value"]:
                decode = {"value": value, "unit": "µop/cycle" if pmu else "instr/cycle",
                    "status": "lower_bound" if pmu else "candidate", "code_bytes": [r["code_bytes"] for r in three],
                    "basis": "intel-mite-counter" if pmu else "nop-plateau-path-assumption"}
    return decode, ({"value": retirement, "unit": "instr/cycle", "status": "lower_bound"} if retirement else None)
