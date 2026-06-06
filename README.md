# Memorytest — Portable Memory & CPU Benchmark Suite

A zero-dependency, cross-platform microbenchmark suite that measures real hardware
latency and bandwidth from L1 cache to DRAM. Supports x86_64, ARM64, RISC-V, and
PowerPC.

**Single command to run everything:** `make test`

```
==============================================================================
  Memorytest Layer 2 Sanity Report — 2026-06-06
==============================================================================

[cache_hierarchy]
  ✓ L1D_latency_ns    =   1.86 ns   ✓ L2_latency_ns   =   2.30 ns
  ✓ L3_latency_ns     =  11.89 ns   ✓ RAM_latency_ns  =  89.24 ns

[memory_bandwidth]
  ✓ read_MBps         = 127213 MB/s ✓ write_MBps      =  78006 MB/s

  Summary: 14 pass, 0 warn, 0 fail
==============================================================================
```

## Quick Start

```bash
git clone https://github.com/AkaseToshiyuki/memorytest.git
cd memorytest
make test
```

That's it. No dependencies beyond a C compiler and Python 3 (stdlib only).

## Test Architecture

`make test` runs three mandatory layers:

| Layer | What | How |
|-------|------|-----|
| **L1 Smoke** | Did every binary run? | `test_smoke.sh` — exit codes, report presence |
| **L2 Sanity** | Are numbers physically plausible? | `test_sanity.py` — range checks per metric |
| **L3 Regression** | Did performance change vs history? | `test_regression.py` — ±15% warn, ±30% fail |

All three must pass. No shortcuts. Detailed architecture: [TEST_LAYERS.md](TEST_LAYERS.md)

## Test Binaries

### Memory & Cache

| Binary | Measures |
|--------|----------|
| `test_cache_hierarchy` | L1D/L2/L3/DRAM latency (pointer-chase), bandwidth per level, boundary detection |
| `test_memory_bandwidth` | Multi-thread read/write/copy bandwidth to DRAM |
| `test_inter_core` | Core-to-core CAS latency (N×N matrix), intra/跨 CCD latency |

### CPU

| Binary | Measures |
|--------|----------|
| `test_cpu_alu` | Integer IPC (add, mul, div, and, or, xor) |
| `test_cpu_float` | Floating-point IPC (add, mul, FMA, SIMD dot product) |
| `test_cpu_branch` | Branch predictor latency (always-taken, never-taken) |
| `test_cpu_multi` | Multi-core scaling efficiency at 8 threads |

## Build

```bash
make              # Build all binaries with -O2 (default)
make memory       # Memory/cache tests only
make cpu          # CPU tests only
make clean        # Remove binaries
make distclean    # Remove binaries and reports
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CPU_TIMEOUT` | `60` | Seconds for CPU-bound tests (scales automatically) |
| `MEMORYTEST_SUDO_PASSWORD` | *(unset)* | Password for `dmidecode` (memory channels, DRAM speed). No sudo needed for basic benchmarks |

```bash
# Example: give CPU tests more time on slow hardware
CPU_TIMEOUT=300 make test

# Example: enable full hardware detection
export MEMORYTEST_SUDO_PASSWORD=$(cat /path/to/password_file)
make test
```

## Auto-Detection

All hardware parameters are detected at runtime — no hardcoded values:

| Parameter | Method |
|-----------|--------|
| CPU model, cores, frequency | sysfs, cpuid, lscpu |
| Cache sizes (L1/L2/L3) | sysfs |
| L3 per-CCD / total (multi-chiplet) | sysfs + topology |
| Memory channels | dmidecode (with sudo) or CPU model heuristic |
| DRAM speed (MT/s) | dmidecode (with sudo) |
| NUMA topology | sysfs |

**Multi-CCD support:** On AMD EPYC/Ryzen with multiple CCDs (e.g. EPYC 7R13: 8 CCDs × 32 MB L3 = 256 MB total), the benchmark correctly accounts for the aggregate L3 when deciding buffer sizes and labelling measurement points. This prevents DRAM measurements from landing inside the LLC on large-cache servers.

## Measurement Methodology

### Latency (Pointer Chase)

A true serial dependency chain: each load produces the address for the next load, defeating the CPU's out-of-order engine and memory-level parallelism. This measures *real* load-to-use latency per cache level.

- ≤ 16 MB buffers: Fisher-Yates shuffle, full permutation array
- > 16 MB buffers: LCG-generated indices with bitset deduplication (prevents self-loops)

### Eviction Strategy (Boundary Detection)

| Buffer size | Between-run eviction | Effect |
|-------------|---------------------|--------|
| ≤ L3 | Only before run 1 | Warm cache for runs 2..N — measures true cache-hit latency |
| > L3 | Every run | Cold cache every run — measures true DRAM latency |

This *selective eviction* preserves the natural latency curve across cache boundaries, enabling accurate L1→L2→L3→DRAM transition detection. Prior versions evicted every run for all sizes, collapsing small-buffer latencies to L2 values and preventing boundary discovery.

### Bandwidth

Buffer size is automatically scaled to 4× total L3 (minimum 1 GB, capped at 8 GB) to guarantee the working set exceeds the last-level cache. Multi-threaded sequential access saturates all memory channels.

## Validation

Results on representative hardware:

| Level | EPYC 7R13 (x86_64) | Kunpeng 920 (ARM64) |
|-------|---------------------|----------------------|
| L1D | 1.9 ns | 1.5 ns |
| L2 | 2.3 ns | 1.8 ns |
| L3 | 11.9 ns | 7.6 ns |
| DRAM | 89.2 ns | 67.6 ns |
| Read BW | 127 GB/s (8-ch DDR4-2666) | 36 GB/s |

## Scoring

`make test` produces an HTML report (`reports/benchmark_report.html`) with per-metric scoring (0-100) and an overall letter grade (A-F). Scoring adapts to the detected hardware: a DRAM latency of 90 ns scores 100/100 on EPYC but would score lower on a DDR5 system.

Scoring bounds (used by `report_score.py`):

| Metric | Ideal | Acceptable |
|--------|-------|------------|
| L1 latency | 1.0 ns | ≤ 5 ns |
| L2 latency | 3.0 ns | ≤ 15 ns |
| L3 latency | 10.0 ns | ≤ 50 ns |
| RAM latency | 80.0 ns | ≤ 150 ns |
| Bandwidth | ≥ theoretical×0.9 | ≥ theoretical×0.5 |

## Project Structure

```
memorytest/
├── README.md
├── Makefile
├── tests.json                  # Test registry
├── test_smoke.sh               # Layer 1: smoke
├── test_sanity.py              # Layer 2: sanity
├── test_regression.py          # Layer 3: regression
├── generate_report.py          # Run all binaries, produce reports
├── report_html.py              # HTML report generator (stdlib only)
├── report_score.py             # Scoring engine
├── src/
│   ├── common.h / common.c     # Shared types, init, helpers
│   ├── platform.h / platform.c # Architecture detection
│   ├── detect.c                # Hardware auto-detection
│   ├── latency.c               # Pointer-chase measurement
│   ├── util.h / util.c         # sudo_popen, env, timing
│   ├── pmu.c / simd.c          # PMU counters, SIMD detection
│   ├── report.c                # Markdown report writer
│   ├── asm_helpers.h           # Inline asm (rdtsc, clflush, barriers)
│   ├── test_cache_hierarchy.c
│   ├── test_memory_bandwidth.c
│   ├── test_inter_core.c
│   ├── test_cpu_alu.c
│   ├── test_cpu_float.c
│   ├── test_cpu_branch.c
│   └── test_cpu_multi.c
├── bin/                        # Built binaries (gitignored)
└── reports/                    # Reports and HTML (gitignored)
```

## Caveats

- **`dmidecode` needs sudo.** Set `MEMORYTEST_SUDO_PASSWORD` for full hardware detection. Without it, memory channels and DRAM speed are estimated.
- **PMU counters need `perf_event_paranoid ≤ 2`.** Falls back to timing-based estimates gracefully.
- **Virtualized environments** (cloud VMs, containers) may report cached or noisy latency values — the benchmark assumes bare-metal or near-bare-metal access to CPU caches.
- **Inter-core test** on large machines (96+ cores) can take 5+ minutes. Set `CPU_TIMEOUT` to scale accordingly.

## References

- [ASM_OPTIMIZATIONS.md](ASM_OPTIMIZATIONS.md) — inline assembly primitives (rdtsc, clflush, barriers)
- [PERF_NOTES.md](PERF_NOTES.md) — build optimization analysis, -O2 vs -O3
- [TEST_LAYERS.md](TEST_LAYERS.md) — detailed test architecture

## License

MIT
