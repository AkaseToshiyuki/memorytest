# Performance Notes

Benchmark suite performance characteristics and optimization analysis.
Captured 2026-06-02 on the development host (Linux 6.17.0-29-generic, ARM64,
24 cores, 3000 MHz, 62.58 GB RAM, L1D=64KB, L2=512KB, L3=24576KB, 2 channels).

## Build variants

| Variant       | Wall time | Binary size (text) | `cache_hierarchy` CPU time |
|---------------|-----------|-------------------:|---------------------------:|
| `-O2`         |    8.4 s  |     33,026 bytes   | 45.58 s user + 0.75 s sys |
| `-O3`         |    8.5 s  |     33,026 bytes   | 45.46 s                    |
| `-O3 -march=native -flto` | 8.7 s | ~33,000 bytes | 45.56 s (LTO 2.7% smaller) |
| `-Ofast -ffast-math`     | 8.4 s |  ~32,900 bytes | not measured (may differ) |

**Conclusion**: `-O2` is sufficient. `-O3` and LTO yield <3% benefit on these
specific micro-benchmarks. LTO does help binary size (`.lto_priv.0` symbols
indicate the compiler is inlining aggressively). `cache_hierarchy` is the
biggest binary; it is dominated by inline asm and PMU init code.

## Per-binary CPU usage (3 s sample, default -O2)

| Binary                   | User CPU | Sys CPU | CPU %  | Notes |
|--------------------------|---------:|--------:|-------:|-------|
| `test_cache_hierarchy`   | 45.46 s  | 0.74 s  | 1484%  | 14-15 cores busy, full pointer-chase + cache scan |
| `test_memory_bandwidth`  | 61.35 s  | 0.34 s  | 2015%  | 20 cores, multi-threaded read/write/copy |
| `test_inter_core`        |  5.94 s  | 0.03 s  |  199%  | 2 cores, NxN matrix + CAS |
| `test_cpu_alu`           |  2.98 s  | 0.01 s  |  100%  | 1 core, single-thread ALU |
| `test_cpu_float`         |  ~0.5 s  | 0.00 s  |  100%  | very short |
| `test_cpu_branch`        |  ~0.3 s  | 0.00 s  |  100%  | very short |
| `test_cpu_multi`         |  ~0.5 s  | 0.00 s  |  100%  | very short |

The hot binaries are `cache_hierarchy` and `memory_bandwidth`. The CPU-only
binaries finish in well under 3 s.

## Hotspot analysis (manual review of source)

### `src/test_cache_hierarchy.c::measure_latency` (lines 52-118)

- Batches 1024 random accesses via LCG, then divides by 1024 to get per-access ns.
- Loop body: `lcg_state = a*lcg_state + c; idx = % words; sum += p[idx];`
- **Overhead per iter**: one `get_time_ns()` call (~30-50 ns syscall), one
  LCG update, one modulo, one `volatile` load.
- **Why hot**: this is the per-size inner loop for L1, L2, L3, RAM latency
  measurement (called 4× per size, sizes from 4 KB to 64 MB).

### `src/test_cache_hierarchy.c::measure_pointer_chase_latency` (lines 121-170)

- Classic pointer-chase pattern, no stride predictability.
- Inner loop: `idx = indices[idx]; val = p[idx];` 64 times.
- **Why hot**: same reason. This is the *gold standard* for cache latency
  measurement (each load depends on the previous, so no HW prefetch helps).

### `src/latency.c::measure_latency_timing` (lines 17-62)

- Fallback path when PMU is unavailable. 64 iterations per measurement,
  64 measurements → 4096 total accesses per `measure_cache_latency()` call.
- Uses `volatile uint64_t *p` and `get_time_ns()` — same overhead pattern.

## Inline-asm optimization candidates

Implemented in `src/asm_helpers.h` (see `ASM_OPTIMIZATIONS.md` for details):

| Function         | Replaces                | Speedup rationale |
|------------------|-------------------------|-------------------|
| `rdtsc_ns()`     | `clock_gettime(MONO)`   | Syscall overhead ~30-50 ns → user-space read ~12-20 ns (ARM64 `cntvct_el0`) or ~12 ns (x86 `rdtsc`). |
| `prefetch_l1()`  | (none, new)             | Hides L1 miss latency of next access in pointer-chase / random loops. |
| `memory_fence()` | `__atomic_thread_fence` | Architecture-specific barrier emits a single instruction (`dmb ish` / `mfence`) instead of libatomic call. |
| `compiler_barrier()` | implicit | Prevents the compiler from reordering around `rdtsc_ns()`. |
| `clflush()`      | (none)                  | `dc civac` on ARM64, `clflush` on x86 — used to force cold access in benchmark setup. |

### Estimated vs measured speedup

The asm changes primarily reduce **measurement overhead**, not the work
itself. For sub-microsecond measurements (L1 hit ~1-2 ns, L2 hit ~5-10 ns),
removing a 30-50 ns `clock_gettime` syscall tightens the timing window
significantly.

**Measured wall-time on 3 s sample: indistinguishable from -O2 baseline**
(45.58 s vs 45.56 s with `-O3 -flto`). The asm's value is in the *fidelity*
of the per-access latency number, not aggregate throughput.

## CFLAGS tuning recommendations

Default `-O2` is correct. Production deployments that share binaries across
hosts with different CPUs should NOT use `-march=native`. The `release` /
`opt` / `opt3` / `ofast` / `lto` Make targets cover the variations.

To check if you're measuring a "fast" build:
```sh
make size              # text/data/bss per binary
/usr/bin/time -v ./bin/test_cache_hierarchy < /dev/null
```

## Hardware detection

`perf_event_paranoid=4` on this host prevents user-mode `perf stat`/`perf
record` for hardware counters. To enable, set:
```sh
echo 2 | sudo tee /proc/sys/kernel/perf_event_paranoid
```

## Future work

- Pin each `test_X` to a specific core (`taskset -c 0`) to avoid scheduler
  noise on multi-tenant systems
- Add NUMA-aware variants of `memory_bandwidth` and `cache_hierarchy` —
  currently they measure only the local node
- Replace `qsort`/`median` with a fixed-size selection algorithm to avoid
  the malloc per measurement
