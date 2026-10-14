"""Strict ComputeMark v2 reader. Never extrapolate rates or accept v1 inputs."""
from dataclasses import dataclass
import math
import statistics

VERSION = "v2"
WORKLOAD = "u64-xorshift4-fp64-logistic4-r1"
PROFILE = "portable-scalar-v2"
FLAGS = "-O2 -std=c11 -Wall -Wextra -pthread -fno-fast-math -ffp-contract=off -fno-tree-vectorize -fno-tree-slp-vectorize -fno-lto"
CLANG_FLAGS = "-O2 -std=c11 -Wall -Wextra -pthread -fno-fast-math -ffp-contract=off -fno-vectorize -fno-slp-vectorize -fno-lto"
KERNEL_HASH = "a05a8fc6700545d9a6dd5dfa6221caf7550ff89b65ad3749b586af5dde51e5d5"
REPEATS = 5
MIN_NS = 100_000_000


@dataclass
class ComputeMarkScore:
    version: str
    points: int
    single_points: int
    integer_gops: float
    fp64_gflops: float
    single_integer_gops: float
    single_fp64_gflops: float
    threads: int
    available_threads: int
    cpu_ids: list[int]
    profile: str
    workload: str
    compiler: str
    architecture: str
    source_hash: str
    scope: str
    max_spread_pct: float
    stable: bool
    placement_policy: str
    affinity_verified: bool
    os: str


def _positive_int(value, name):
    if type(value) is not int or not 0 < value < 2**64:
        raise ValueError(f"invalid {name}")
    return value


def compute_mark_score(data):
    """Recompute rates/points from counted operations and raw wall times.

    Points = round-half-up(1000 * sqrt(integer Gop/s * scalar FP64 GFLOP/s)).
    1000 is a fixed unit conversion, not a reference CPU or a score ceiling.
    Calculate separately for 1 thread and the explicitly recorded thread count.
    """
    if not isinstance(data, dict):
        raise ValueError("ComputeMark requires a v2 result object")
    contract = {"schema_version": 2, "version": VERSION, "workload": WORKLOAD,
                "profile": PROFILE, "source_hash": KERNEL_HASH}
    for key, expected in contract.items():
        if data.get(key) != expected:
            raise ValueError(f"unsupported or mismatched ComputeMark {key}")
    compiler = data.get("compiler")
    if not isinstance(compiler, str) or not compiler.strip():
        raise ValueError("missing compiler")
    expected_flags = CLANG_FLAGS if "clang" in compiler.lower() else FLAGS
    if data.get("build_flags") != expected_flags:
        raise ValueError("unsupported or mismatched ComputeMark build_flags")
    if data.get("selftest_passed") is not True:
        raise ValueError("kernel self-test is required")
    if data.get("translated", False) is not False:
        raise ValueError("translated execution cannot produce a native score")
    threads = _positive_int(data.get("threads"), "thread count")
    cpus = data.get("available_cpus")
    # Preserve old Linux v2 archives while making the new unpinned policy
    # explicit and impossible to mistake for verified CPU IDs.
    policy = data.get("placement_policy", "linux-pinned")
    expected_timer = "QueryPerformanceCounter" if policy == "windows-pinned-process-group" else "CLOCK_MONOTONIC"
    if data.get("timer") != expected_timer:
        raise ValueError("unsupported or mismatched ComputeMark timer")
    if policy in {"linux-pinned", "windows-pinned-process-group"}:
        expected_os = "Windows" if policy.startswith("windows") else "Linux"
        if data.get("affinity_verified") is not True or data.get("os", "Linux") != expected_os:
            raise ValueError("Pinned policy requires verified affinity and matching OS")
        if (not isinstance(cpus, list) or not cpus or len(cpus) > 262144 or
                any(type(cpu) is not int or cpu < 0 for cpu in cpus) or
                sorted(set(cpus)) != cpus or threads > len(cpus)):
            raise ValueError("invalid available CPU IDs")
        available = len(cpus)
        if data.get("available_threads", available) != available:
            raise ValueError("inconsistent available thread count")
        scope = "all-allowed" if threads == available else f"limited-{threads}"
        if policy.startswith("windows"):
            if available > 64 or len({cpu // 64 for cpu in cpus}) != 1 or data.get("placement_verified") is not True:
                raise ValueError("Windows requires one verified processor group")
            scope = "process-group" if threads == available else f"process-group-limited-{threads}"
        cpu_ids = cpus[:threads]
    elif policy == "macos-qos-user-initiated":
        if (data.get("os") != "Darwin" or data.get("affinity_verified") is not False or
                data.get("placement_verified") is not True or data.get("translated") is not False or
                "available_cpus" not in data or cpus is not None):
            raise ValueError("macOS requires verified QoS, native execution and no claimed CPU pinning")
        available = _positive_int(data.get("available_threads"), "available threads")
        if available > 262144 or threads > available:
            raise ValueError("invalid macOS thread count")
        cpu_ids = []
        scope = "all-online" if threads == available else f"limited-{threads}"
    else:
        raise ValueError("unsupported placement policy")
    for key in ["compiler", "architecture"]:
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"missing {key}")
    rates = {}
    spreads = []
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError("missing measured records")
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("invalid measured record")
        kind = record.get("workload")
        if kind not in {"integer", "fp64"}:
            raise ValueError("unknown measured workload")
        count = _positive_int(record.get("threads"), "record threads")
        key = kind, count
        if key in rates or count not in {1, threads}:
            raise ValueError("duplicate/unexpected thread record")
        per_iteration = 24 if kind == "integer" else 12
        if record.get("operations_per_iteration") != per_iteration:
            raise ValueError("incorrect operation accounting")
        iterations = _positive_int(record.get("iterations_per_thread"), "iteration count")
        expected_ops = iterations * count * per_iteration
        samples = record.get("samples")
        if not isinstance(samples, list) or len(samples) != REPEATS:
            raise ValueError(f"exactly {REPEATS} repeats required")
        observed = []
        for sample in samples:
            if not isinstance(sample, dict):
                raise ValueError("invalid sample")
            elapsed = _positive_int(sample.get("elapsed_ns"), "elapsed time")
            ops = _positive_int(sample.get("operations"), "operation count")
            if elapsed < MIN_NS or ops != expected_ops:
                raise ValueError("short sample or inconsistent operation count")
            checksum = sample.get("checksum")
            if not isinstance(checksum, str) or len(checksum) != 16:
                raise ValueError("missing checksum")
            try:
                int(checksum, 16)
            except ValueError as exc:
                raise ValueError("invalid checksum") from exc
            observed.append(ops / elapsed)  # operations/ns == billions of ops/s
        if len({s["checksum"] for s in samples}) != 1:
            raise ValueError("nondeterministic workload results")
        median = statistics.median(observed)
        rates[key] = median
        spreads.append((max(observed) - min(observed)) / median * 100)
    required = {(kind, n) for kind in ["integer", "fp64"] for n in {1, threads}}
    if set(rates) != required:
        raise ValueError("missing single/multi integer or FP64 measurements")
    def points(n):
        return math.floor(1000 * math.sqrt(rates["integer", n] * rates["fp64", n]) + 0.5)
    return ComputeMarkScore(
        VERSION, points(threads), points(1), rates["integer", threads], rates["fp64", threads],
        rates["integer", 1], rates["fp64", 1], threads, available, cpu_ids,
        PROFILE, WORKLOAD, data["compiler"], data["architecture"], KERNEL_HASH,
        scope, max(spreads), max(spreads) <= 15.0, policy, data["affinity_verified"], data.get("os", "Linux"),
    )


def main():
    import argparse
    from dataclasses import asdict
    import json
    from pathlib import Path
    from benchmark_registry import ROOT
    parser = argparse.ArgumentParser(description="Read measured ComputeMark v2 points; never reuse legacy /100 grades")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", type=Path, help="explicit compute_mark_v2.json")
    source.add_argument("--run-dir", type=Path, help="archived run directory")
    source.add_argument("--latest-run", action="store_true", help="use most recently finished run (default)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.input:
            path = args.input
        else:
            run = args.run_dir
            if run is None:
                candidates = []
                for item in (ROOT / "reports/runs").glob("*/manifest.json"):
                    try:
                        if json.loads(item.read_text(encoding="utf-8")).get("finished_at"):
                            candidates.append(item)
                    except (OSError, ValueError):
                        continue
                if not candidates:
                    raise ValueError("no completed run; use make test MODULE=cpu_multi")
                run = max(candidates, key=lambda item: item.stat().st_mtime_ns).parent
            path = run / "reports/compute_mark_v2.json"
        score = compute_mark_score(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"ComputeMark unavailable: {exc}\n")
    if args.json:
        print(json.dumps(asdict(score), indent=2))
    else:
        print(f"Source: {path}\nComputeMark v2 ({score.scope})")
        print(f"Single-thread: {score.single_points:,} points")
        print(f"Multi-thread: {score.points:,} points at {score.threads} threads")
        print(f"Measured: {score.integer_gops:.3f} Gop/s integer; {score.fp64_gflops:.3f} GFLOP/s scalar FP64")
        print(f"Profile: {score.profile}; compiler: {score.compiler}; max repeat spread: {score.max_spread_pct:.2f}%")
        print(f"Placement: {score.placement_policy}; exact CPU affinity: {score.affinity_verified}")
        if not score.affinity_verified:
            print("NOTE: OS-scheduled score; P/E core placement is not guaranteed. Do not mix with pinned-policy rankings.")
        if not score.stable:
            print("WARNING: unstable samples; this run must not be used for ranking")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
