"""Evidence reader for explicit-instruction architecture characterization.

No conversion from elapsed time to cycles, decoded width, execution ports,
physical registers, scheduler sizes or reorder-buffer capacity.
"""
from sample_statistics import summarize

PROBES = {"nop_stream": 64, "iadd_dep": 32, "iadd_ind8": 32, "imul_dep": 32, "imul_ind8": 32,
          "fadd_dep": 32, "fadd_ind8": 32, "fmul_dep": 32, "fmul_ind8": 32,
          "load_chain_32k": 256, "write_ordered_32k": 256, "copy_ordered_64k": 256}


def read_architecture(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("protocol") != "architecture-probes-v1":
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
    return {"metrics": metrics, "reported_cache_bytes": caches, "placement": policy, "cpu_id": cpu,
            "isa": data["isa"], "sample_target_ns": target,
            "stable": all(m["stable"] for m in metrics.values())}
