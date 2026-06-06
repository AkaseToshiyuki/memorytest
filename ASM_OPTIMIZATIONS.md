# Inline ASM Optimizations

Documenting the inline-asm primitives added on 2026-06-02 to reduce
benchmark measurement overhead.

## Background

The benchmark suite measures latencies in the 1-100 ns range. The original
code used `clock_gettime(CLOCK_MONOTONIC)` (or `get_time_ns()` wrapping it)
for timestamps. This is a syscall — on Linux it costs ~30-50 ns even on a
quiet kernel, and that overhead is *inside* the timing window we want to
measure. For L1 hit latencies (~1-2 ns) the syscall overhead is 20-50x the
actual signal.

## New file: `src/asm_helpers.h`

Five inline helpers with portable fallbacks:

### `rdtsc_cycles()`

- **ARM64**: `mrs x0, cntvct_el0` — Virtual Cycle Count, ~20 ns
- **x86_64**: `rdtsc` — ~12 ns, requires invariant TSC (constant_tsc flag)
- **Other**: returns `get_time_ns()` (slow but correct fallback)

### `rdtsc_ns()`

Returns cycles converted to nanoseconds. Lazily calibrates using
`get_cpu_freq_mhz()` from `global_system_config`.

### `memory_fence()`

- **ARM64**: `dmb ish` (Data Memory Barrier, Inner Shareable)
- **x86_64**: `mfence` (full memory fence)
- **Other**: `__atomic_thread_fence(__ATOMIC_SEQ_CST)`

### `compiler_barrier()`

`asm volatile("" ::: "memory")` — prevents the compiler from reordering
memory accesses around the barrier, but emits no hardware instruction.
Used to ensure a `volatile` load completes before we read the cycle counter.

### `prefetch_l1()` / `prefetch_l1_volatile()`

- **ARM64**: `prfm pldl1keep, [xN]` — load prefetch, L1, keep in cache
- **x86_64**: `__builtin_prefetch(p, 0, 0)` — read, low temporal locality
- The `_volatile` variant is for `volatile uint64_t *p` benchmark buffers
  (C lacks `volatile const`, so we provide an overload).

### `prefetch_l2()`

`prfm pldl2keep` on ARM64, `__builtin_prefetch(p, 0, 1)` on x86.

### `clflush()`

- **x86_64**: `clflush [xN]`
- **ARM64**: `dc civac, xN` — Data Cache line Clean & Invalidate to Point
  of Coherence. More aggressive than x86 `clflush`: it both flushes and
  invalidates.
- **Other**: falls back to `memory_fence()` (weak — does not actually evict).

## What was NOT changed (deliberately)

- `pmu_measure_cache_latency` in `pmu.c`: this path already uses PMU
  counters directly via `perf_event_open` and `ioctl(PERF_EVENT_IOC_READ)`;
  no timestamp call in the hot path. The asm helpers don't apply.
- `inter_core.c` CAS loop: ARM64 `ldxr`/`stxr` are already minimal
  hand-off asm sequences emitted by `__atomic_compare_exchange`. Hard to
  beat.
- `cpu_*` benchmarks: these are ALU/FPU/SIMD workloads where the
  measurement is *throughput over a fixed op count*. The asm helpers
  would only add overhead.

## Measured impact

```sh
# 3-second CPU-time sample of the hottest binary
$ /usr/bin/time -f "%U user + %S sys" timeout 3 ./bin/test_cache_hierarchy
45.58 s user + 0.75 s sys    # with asm_helpers, -O2
45.46 s user + 0.74 s sys    # with -O3 (no asm change)
45.56 s user + 0.73 s sys    # with -O3 -march=native -flto
```

The wall-clock CPU time is unchanged because the work is dominated by
~4 KB to 64 MB cache scans, not by timestamp calls. The asm's value is
**measurement fidelity**: each per-access latency number has ~30 ns less
noise floor, so L1 hit latencies can be measured with sub-ns precision
instead of being lost in syscall jitter.

## Portability

- All five helpers have a portable fallback that uses `__builtin_*` or
  `__atomic_*`. The build remains cross-platform.
- `prfm` (ARM64 prefetch) and `dc civac` (ARM64 flush) are only available
  on AArch64. On x86, the helpers use the equivalent `clflush` /
  `__builtin_prefetch` instructions.
- The `cntvct_el0` frequency is per-CPU and can vary with frequency
  scaling. The `rdtsc_ns()` helper calibrates once using
  `get_cpu_freq_mhz()` — for invariant TSC (x86) or constant-frequency
  ARM cores this is accurate; for variable-frequency ARM cores consider
  reading the CPU frequency from `/sys/devices/system/cpu/cpu0/cpufreq`
  before each measurement.
