#!/usr/bin/env python3
"""Deterministic subprocess fixture. No hardware benchmark work is performed."""
import json
import os
from pathlib import Path
import sys
import time

config = json.loads((Path(__file__).resolve().parents[1] / "tests.json").read_text())["tests"]
name, info = next((name, info) for name, info in config.items() if info["bin"] == Path(sys.argv[0]).name)
parent = name
shared = False
if "--mode" in sys.argv:
    selected = sys.argv[sys.argv.index("--mode") + 1]
    shared = selected == "latency-detection"
    if shared:
        selected = "latency"
    child_names = {("cache_hierarchy", "bandwidth"): "cache_bandwidth_test",
                   ("cache_hierarchy", "latency"): "cache_latency_test",
                   ("cache_hierarchy", "detection"): "cache_detection",
                   ("memory_bandwidth", "bandwidth"): "memory_bandwidth_test",
                   ("memory_bandwidth", "latency"): "memory_latency_test"}
    name = child_names[parent, selected]
    info = dict(info, report=f"{name}_report.md")
mode = os.environ.get("FIXTURE_" + name, "pass")
Path("executed").write_text(name)
if mode == "timeout":
    time.sleep(10)
if mode == "missing":
    sys.exit(0)
if mode == "unsupported":
    print("UNSUPPORTED: fixture platform capability")
    sys.exit(77)

tables = {
    "cache_hierarchy": "| Size | RdLat(ns) | WrLat(ns) | BW(MB/s) | Expected | Analysis |\n|---|---|---|---|---|---|\n| 4 KB | 2 | 2 | 10000 | L1 | L1 |\n",
    "memory_bandwidth": "| Operation | Bandwidth (MB/s) |\n|---|---|\n| Read | 12345 |\n| Write | 9876 |\n| Copy | 5678 |\n",
    "inter_core": "| **Core** | 0 | 1 |\n|---|---|---|\n| 0 | - | 20 |\n| 1 | 21 | - |\n",
    "cpu_alu": "| Operation | Time(ms) | Ops/sec | ns/op | CPI | IPC | Data Source |\n|---|---|---|---|---|---|---|\n| Add | 100 | 1000000000 | 1 | 4 | 0.25 | Estimated |\n",
    "cpu_float": "| Operation | Type | Time(ms) | Ops/sec | ns/op | CPI | IPC |\n|---|---|---|---|---|---|---|\n| FMA | SIMD | 100 | 1000000000 | 1 | 4 | 0.25 |\n",
    "cpu_branch": "| Pattern | Category | Time(ms) | ns/branch | Mispred Rate | Data Source |\n|---|---|---|---|---|---|\n| Always Taken | Predictable | 100 | 1 | N/A | Estimated |\n",
    "cpu_multi": "| Operation | Category | Threads | Time(ms) | Speedup | Efficiency | Status |\n|---|---|---|---|---|---|---|\n| Add | Integer | 1 | 100 | 1.00x | 100% | OK |\n| Add | Integer | 2 | 50 | 2.00x | 100% | OK |\n",
    "cpu_architecture": "| Probe | ns/op |\n|---|---|\n| iadd_dep | 1.5 |\n",
}
report = Path("reports") / info["report"]
system = "## System Configuration\n\n| Item | Value |\n|---|---|\n| CPU Model | Fixture CPU " + name + " |\n"
content = f"# {name} Test Report\n\n{system}\n## Results\n\n{tables[parent]}"
if mode == "invalid":
    content = "# Empty Report\n"
elif mode == "system_only":
    content = "# Incomplete Report\n" + system
report.write_text(content, encoding="utf-8")
if name == "cpu_architecture" and mode != "missing_evidence":
    # Explicit synthetic raw evidence for runner/presentation tests, not hardware.
    probes = {"nop_stream": 64, "iadd_dep": 32, "iadd_ind8": 32, "imul_dep": 32, "imul_ind8": 32,
              "fadd_dep": 32, "fadd_ind8": 32, "fmul_dep": 32, "fmul_ind8": 32,
              "load_chain_32k": 256, "write_ordered_32k": 256, "copy_ordered_64k": 256}
    raw = {"schema_version": 1, "protocol": "architecture-probes-v1", "isa": "x86_64-sse2",
        "placement": "os-scheduled-single-thread", "cpu_id": -1, "sample_count": 10,
        "sample_target_ns": 50_000_000, "timer": "monotonic-wall-time", "cache_source": "OS-reported-not-probed",
        "reported_cache_bytes": dict.fromkeys(("l1i", "l1d", "l2", "l3"), 0),
        "records": [{"probe": probe, "blocks": 1_000_000, "operations_per_block": count,
                     "validated": True, "elapsed_ns": [100_000_000] * 10} for probe, count in probes.items()]}
    (report.parent / "cpu_architecture.json").write_text("{}" if mode == "bad_evidence" else json.dumps(raw))
if mode == "audit":
    (report.parent / f"{name}_samples.json").write_text(json.dumps({"elapsed_ns": [100, 99, 101, 98, 102]}))
if shared:
    if mode != "missing_peer":
        (report.parent / "cache_detection_report.md").write_text(content)
    if mode == "audit":
        (report.parent / "cache_detection_samples.json").write_text(json.dumps({"elapsed_ns": [100, 99, 101, 98, 102]}))
if mode == "fail":
    sys.exit(7)
