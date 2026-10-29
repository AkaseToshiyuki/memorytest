"""Strict reader for experimental unpinned macOS CPU-pair observations.

Raw attempts, including rejections, are retained. Endpoint checks are NOT proof
of uninterrupted placement; this data must never enter pinned topology ranks.
"""
from collections import defaultdict
from sample_statistics import summarize

PROTOCOL = "macos-observed-cpu-pairs-v2"
PROTOCOLS = {"macos-observed-cpu-pairs-v1": (10, 20000), PROTOCOL: (32, 64000)}


def read_observed(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1 or type(data.get("protocol")) is not str or data["protocol"] not in PROTOCOLS:
        raise ValueError("unsupported observed-pair protocol")
    samples, max_attempts = PROTOCOLS[data["protocol"]]
    expected = {"os": "Darwin", "affinity_verified": False, "migration_verification": "endpoints-only",
                "cpu_id_source": "TPIDR_EL0-low12-private-ABI", "timer": "CLOCK_MONOTONIC",
                "qos": "user-initiated", "qos_verified": True, "background_load": "other-workers-runnable"}
    for key, value in expected.items():
        if type(data.get(key)) is not type(value) or data[key] != value:
            raise ValueError(f"invalid observation condition: {key}")
    for key, low, high in (("logical_cpus", 2, 64), ("workers", 2, 64), ("budget_ms", 1, 30000),
                           ("sample_count", samples, samples), ("round_trips", 2000, 2000), ("warmup_round_trips", 128, 128)):
        if type(data.get(key)) is not int or not low <= data[key] <= high:
            raise ValueError(f"invalid {key}")
    if data["workers"] > data["logical_cpus"]:
        raise ValueError("worker count exceeds logical CPUs")
    attempts = data.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= max_attempts:
        raise ValueError("missing or excessive attempts")
    groups, counts, observed = defaultdict(list), defaultdict(int), set()
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("invalid attempt")
        start, end = attempt.get("from"), attempt.get("to")
        if any(not isinstance(pair, list) or len(pair) != 2 or any(type(cpu) is not int or not 0 <= cpu < 4096 for cpu in pair)
               for pair in (start, end)):
            raise ValueError("invalid observed CPU IDs")
        elapsed = attempt.get("elapsed_ns")
        reason = attempt.get("status")
        if type(elapsed) is not int or not 0 <= elapsed <= 2**64 - 1:
            raise ValueError("invalid elapsed time")
        if reason not in {"kept", "migration", "same_cpu", "timeout", "quota", "cpu_id_unavailable"}:
            raise ValueError("invalid attempt status")
        counts[reason] += 1
        if reason in {"timeout", "cpu_id_unavailable"}:
            continue
        if elapsed <= 0:
            raise ValueError("missing timing")
        migrated = start[0] != start[1] or end[0] != end[1]
        if reason == "migration":
            if not migrated:
                raise ValueError("false migration rejection")
            continue
        if migrated:
            raise ValueError("migrated sample was not rejected")
        if reason == "same_cpu":
            if start[0] != end[0]:
                raise ValueError("false same-CPU rejection")
            continue
        if start[0] == end[0]:
            raise ValueError("same CPU is not an inter-CPU observation")
        observed.update((start[0], end[0]))
        values = groups[start[0], end[0]]
        if reason == "quota":
            if len(values) != samples:
                raise ValueError("quota skipped before the required samples")
        else:
            if len(values) == samples:
                raise ValueError("too many retained samples")
            values.append(elapsed / (2 * data["round_trips"]))
    if len(observed) > data["logical_cpus"]:
        raise ValueError("CPU observation count exceeds reported CPU count")
    pairs = {key: summarize(values) for key, values in groups.items() if len(values) == samples}
    if type(data.get("covered_pairs")) is not int or data["covered_pairs"] != len(pairs):
        raise ValueError("incorrect coverage")
    if not pairs:
        raise ValueError("no pair has the required usable samples")
    return {"ids": sorted(observed), "pairs": pairs, "attempt_counts": dict(counts),
            "covered_pairs": len(pairs), "possible_pairs": data["logical_cpus"] * (data["logical_cpus"] - 1),
            "workers": data["workers"], "logical_cpus": data["logical_cpus"],
            "protocol": data["protocol"], "sample_count": samples}
