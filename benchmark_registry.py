"""Shared module selection for the benchmark runner and report generator."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "tests.json").read_text(encoding="utf-8"))
TESTS = CONFIG["tests"]
CATEGORIES = CONFIG["categories"]

# Stable legacy aggregate IDs stay readable and runnable. Desktop selections
# are independent tasks using explicit engine modes and separate evidence files.
SUBTESTS = {
    "cache_bandwidth_test": ("cache_hierarchy", "bandwidth"),
    "cache_latency_test": ("cache_hierarchy", "latency"),
    "cache_detection": ("cache_hierarchy", "detection"),
    "memory_bandwidth_test": ("memory_bandwidth", "bandwidth"),
    "memory_latency_test": ("memory_bandwidth", "latency"),
}
TASKS = dict(TESTS)
_TASK_LABELS = {
    "cache_bandwidth_test": ("Cache bandwidth test", "缓存带宽测试"),
    "cache_latency_test": ("Cache latency test", "缓存延迟测试"),
    "cache_detection": ("Cache hierarchy detection", "缓存层级检测（含必要的延迟扫描）"),
    "memory_bandwidth_test": ("Memory bandwidth test", "内存带宽测试"),
    "memory_latency_test": ("Memory latency test", "内存延迟测试"),
}
for _name, (_parent, _mode) in SUBTESTS.items():
    TASKS[_name] = dict(TESTS[_parent], report=f"{_name}_report.md", engine=_parent,
                        args=["--mode", _mode], name=_TASK_LABELS[_name][0],
                        description=_TASK_LABELS[_name][0], description_zh=_TASK_LABELS[_name][1])
SELECTION_GROUPS = {"cache_group": list(SUBTESTS)[:3], "memory_group": list(SUBTESTS)[3:],
                    "core_group": ["cpu_alu", "cpu_float", "cpu_branch", "cpu_multi", "inter_core", "cpu_architecture"]}

# Audit status, not a certification of every OS/ISA or hardware environment.
COMPARABILITY = {
    "cache_hierarchy": "Diagnostic v3: 5 equal-work samples, unfiltered median and raw samples; full-cycle 64-byte-node latency, fixed grid, single-thread useful-byte bandwidth. Persistent latency transitions use raw IQR bands and drift checks, not exact capacities or guaranteed cache levels. Match protocol/size/page/compiler/placement conditions.",
    "memory_bandwidth": "Diagnostic v3: 20 equal-work samples with declared outer-IQR policy and raw evidence; useful decimal MB/s over a common worker interval. Bandwidth and single-thread dependent latency can run separately. OS placement, first touch, compiler/vectorization and cache residency affect results. Not a standardized DRAM ranking.",
    "inter_core": "Linux topology diagnostic, not a CPU ranking: verified allowed logical CPU IDs (SMT included), lock-free CAS and isolated flags. Native Apple Silicon instead uses bounded experimental scheduler-observed CPU pairs, no pinning, endpoint-only migration rejection and explicit incomplete coverage. Never mix these protocols or infer P/E clusters from macOS observations. Windows/Intel macOS remain unsupported.",
    "cpu_alu": "Diagnostic v3: 10 samples, register dependency chains retained with compiler barriers; includes loop and auxiliary work (and deliberate memory traffic in Load+ALU). Not peak instruction throughput; no frequency-derived CPI/IPC. Incompatible with old volatile-loop timings; excluded from ComputeMark v2.",
    "cpu_float": "Diagnostic v3: 10 samples, retained bounded operations and correct scalar-lane counts; includes loop/input/reduction overhead. Compiler vectorization, FMA and libm differ across targets. Not peak hardware throughput; excluded from ComputeMark v2.",
    "cpu_branch": "Diagnostic v3: 20 samples, fixed balanced patterns and identical explicit conditional branch on x86_64/AArch64; wall time includes loads, loop branches and path work. Pattern sensitivity is not prediction accuracy or BTB capacity; excluded from ComputeMark v2.",
    "cpu_multi": "ComputeMark v2: fixed scalar uint64/binary64 kernels, direct common wall-time and counted work. Linux verifies CPU pinning; Windows verifies pinning within the current process group (not necessarily the whole machine); macOS verifies USER_INITIATED QoS, NOT pinning or P-core selection. Compare matching version/profile/compiler and placement policy; never mix placement policies in rankings. Scalar workload rating, not peak SIMD or universal application speed. Native Windows/Apple Silicon validation remains required.",
    "cpu_architecture": "Architecture probes v2: static taken-branch targets at 8/16/32-byte spacing and 128-B to 256-KiB instruction footprints; ten samples at 10-ms targets, plus twelve 50-ms instruction/memory probes. BTB transitions are empirical capacity candidates, not physical entry counts. Hardware counters use Linux thread perf events or macOS process rusage when available. Physical fetch/decode widths, queues, registers and ROB sizes remain unmeasured. OS cache metadata is labelled. Linux pins one logical CPU; macOS verifies QoS only; Windows is OS-scheduled. No P/E claims or score.",
}
COMPARABILITY["cpu_multi"] += " Sampling repeat10-outer-iqr3-v1 is distinct from repeat20-outer-iqr3-v1 and legacy5-median; kernel/operations/formula are unchanged."
COMPARABILITY["cpu_multi"] += " New runs measure powers-of-two-plus-max-v1 thread stages; legacy endpoint-only archives retain their measured endpoints."
COMPARABILITY["inter_core"] += " Linux intercore-v3 retains 20 samples per direction/metric; macOS retains ten endpoint-consistent samples per reported observed pair."
COMPARABILITY["memory_bandwidth"] += " Latency v1 adds ordered 64-B write and copy transactions, not isolated store/DRAM latency; copy footprint includes both arrays."
for _child, (_parent, _mode) in SUBTESTS.items():
    COMPARABILITY[_child] = COMPARABILITY[_parent]


def select_modules(modules=None, category=None):
    """Default to all modules; preserve registry order and remove duplicates."""
    requested = set(modules or [])
    unknown = requested.difference(TASKS)
    if unknown:
        raise ValueError(f"Unknown modules: {', '.join(sorted(unknown))}")
    if category:
        if category not in CATEGORIES:
            raise ValueError(f"Unknown category: {category}")
        requested.update(CATEGORIES[category])
    if not requested:
        return list(TESTS)
    # Selecting a legacy aggregate already includes its child workloads.
    requested -= {child for child, (parent, _) in SUBTESTS.items() if parent in requested}
    if requested.intersection(SUBTESTS):
        order = list(dict.fromkeys([name for names in SELECTION_GROUPS.values() for name in names] + list(TESTS)))
        return [name for name in order if name in requested]
    return [name for name in TASKS if name in requested]


def report_filename(modules):
    if set(modules) == set(TESTS):
        return "benchmark_report.html"
    if len(modules) == 1:
        return f"{modules[0]}_report.html"
    return "selected_report.html"
