"""Shared module selection for the benchmark runner and report generator."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "tests.json").read_text(encoding="utf-8"))
TESTS = CONFIG["tests"]
CATEGORIES = CONFIG["categories"]

# Audit status, not a certification of every OS/ISA or hardware environment.
COMPARABILITY = {
    "cache_hierarchy": "Diagnostic v2: warmed full-cycle 64-byte-node latency, fixed working-set grid, single-thread useful-byte bandwidth. Compare matching size/page/compiler/placement conditions. Persistent latency transitions are intervals, not exact capacities or guaranteed cache-level assignments. Old results use incompatible protocols.",
    "memory_bandwidth": "Diagnostic v2: useful decimal MB/s over a common worker interval; eight access patterns and single-thread dependent latency. OS placement, first touch, compiler/vectorization and cache residency affect results. Not a standardized DRAM ranking; Linux container memory limits still require care.",
    "inter_core": "Topology diagnostic, not a CPU ranking: completion publication and repeated sampling are checked, but atomic primitive/lock-freedom, actual allowed CPU IDs and cache-line placement still need platform validation. Matrix averages across unlike topologies are not equivalent.",
    "cpu_alu": "Diagnostic v2: calibrated, warmed median timing of counted legacy dependency-chain iterations, including volatile and auxiliary work. Not peak instruction throughput; no frequency-derived CPI/IPC. Excluded from ComputeMark v2.",
    "cpu_float": "Diagnostic v2: retained bounded operations and correct scalar-lane counts, warmed medians; includes loop/input/reduction overhead. Compiler vectorization, FMA and libm differ across targets. Not peak hardware throughput; excluded from ComputeMark v2.",
    "cpu_branch": "Diagnostic v2: fixed balanced patterns and identical explicit conditional branch on x86_64/AArch64; wall time includes loads, loop branches and path work. Pattern sensitivity is not branch prediction accuracy or BTB capacity. No fabricated PMU percentages; excluded from ComputeMark v2.",
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
