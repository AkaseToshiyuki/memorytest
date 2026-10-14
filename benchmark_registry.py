"""Shared module selection for the benchmark runner and report generator."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "tests.json").read_text(encoding="utf-8"))
TESTS = CONFIG["tests"]
CATEGORIES = CONFIG["categories"]

# Audit status, not a certification of every OS/ISA or hardware environment.
COMPARABILITY = {
    "cache_hierarchy": "Diagnostic only: pointer-chain footprint, eviction and topology-dependent scan sizes differ. Compare matching working sets/page sizes/cache policy; earlier frequency-derived timings are incompatible.",
    "memory_bandwidth": "Not yet ranking-safe: per-thread bandwidths are summed over different time windows; useful bytes vs cache-line traffic and MiB/s vs MB/s are mixed. Buffer sizing, NUMA placement and resource limits need protocol fixes.",
    "inter_core": "Topology diagnostic, not a CPU ranking: atomic primitive/lock-freedom, actual allowed CPU IDs, pair completion and cache-line placement need validation per platform. Matrix averages across unlike topologies are not equivalent.",
    "cpu_alu": "Legacy diagnostic: volatile dependency chains and mixed operations are not peak instruction throughput. Frequency-derived IPC is not retired-instruction IPC and is not comparable across ISAs.",
    "cpu_float": "Not ranking-safe: SIMD implementations perform different recurrences/counts; fallback may be a no-op, division/sqrt can be optimized away, and underflow/overflow/libm affect workloads. Excluded from ComputeMark v2.",
    "cpu_branch": "Not ranking-safe: constant branches can disappear or become conditional moves; libc rand sequences vary. PMU misses/instructions is not branch-miss rate. Excluded from ComputeMark v2.",
    "cpu_multi": "ComputeMark v2: fixed scalar uint64/binary64 kernels, direct common wall-time and counted work. Linux verifies CPU pinning; Windows verifies pinning within the current process group (not necessarily the whole machine); macOS verifies USER_INITIATED QoS, NOT pinning or P-core selection. Compare matching version/profile/compiler and placement policy; never mix placement policies in rankings. Scalar workload rating, not peak SIMD or universal application speed. Native Windows/Apple Silicon validation remains required.",
}


def select_modules(modules=None, category=None):
    """Default to all modules; preserve registry order and remove duplicates."""
    requested = set(modules or [])
    unknown = requested.difference(TESTS)
    if unknown:
        raise ValueError(f"Unknown modules: {', '.join(sorted(unknown))}")
    if category:
        if category not in CATEGORIES:
            raise ValueError(f"Unknown category: {category}")
        requested.update(CATEGORIES[category])
    return [name for name in TESTS if not requested or name in requested]


def report_filename(modules):
    if len(modules) == len(TESTS):
        return "benchmark_report.html"
    if len(modules) == 1:
        return f"{modules[0]}_report.html"
    return "selected_report.html"
