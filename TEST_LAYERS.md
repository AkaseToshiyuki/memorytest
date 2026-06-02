# Test Layers — Layer 1 + Layer 2

Added 2026-06-02 to bring the project from "manual eyeballing" to
"machine-verifiable PASS/FAIL" without changing what gets measured.

## Layer 1 — Smoke (`test_smoke.sh`)

**Goal**: verify each of the 7 binaries runs to completion and produces
a non-empty report file. Catches regressions like build breakage,
segfault, or report-writer bug.

**Checks** (per binary):
- Binary runs to completion within 70 s timeout
- `exit code` is 0 or 124 (timeout; treated as failure)
- Report file at `reports/{test_name}_report.md` exists
- Report file was modified by *this* run (mtime check)
- Report file is > 200 bytes
- Report contains expected sections (`## System Configuration`)

**Wall time**: ~2:30 on a 24-core ARM64 host. Most binaries finish in
under 5 s; the bottleneck is `test_inter_core` (24×24 CAS matrix) and
`test_cpu_float` (12 ops × 100M iterations), each 45-60 s.

**Pass criterion**: 7/7 binaries PASS.

## Layer 2 — Sanity (`test_sanity.py`)

**Goal**: verify the *measured numbers* fall within a physically
plausible range. Catches:
- Code bugs (e.g. `cntvct_el0` miscalibration by 30× → "0.13 ns L1")
- Hardware detection errors (e.g. wrong cache level attribution)
- System misconfiguration (e.g. NUMA disabled, wrong freq)

**Checks** (one assertion per metric per test):

| Test | Metric | Range | Why |
|------|--------|-------|-----|
| cache_hierarchy | L1D latency | 0.1–10 ns | L1 hit + measurement floor |
| cache_hierarchy | L2 latency | 0.1–25 ns | L2 + hw prefetcher floor |
| cache_hierarchy | L3 latency | 5–80 ns | L3 hit |
| cache_hierarchy | RAM latency | 20–500 ns | DRAM access |
| memory_bandwidth | Read MB/s | ≥ 200 | Multi-channel saturation |
| memory_bandwidth | Write MB/s | ≥ 100 | Write BW floor |
| inter_core | Same-socket CAS | 5–300 ns | CC protocol latency |
| cpu_alu | Add IPC | 0.01–1.0 | Mod-bound loop (~0.05 typical) |
| cpu_alu | Mul IPC | 0.01–1.0 | Mod-bound loop |
| cpu_float | float Add IPC | 0.05–4.0 | Superscalar FPU |
| cpu_float | double Mul IPC | 0.05–2.0 | FPU |
| cpu_branch | Always Taken | 0–2 ns/branch | Predictable branch |
| cpu_branch | Never Taken | 0–2 ns/branch | Predictable branch |
| cpu_multi | Mul @ 8 threads | 50–100% | Scaling efficiency |

**Pass criterion**: 0 FAIL. WARN is non-fatal unless `--strict`.

**Wall time**: < 1 s (pure Python file reading).

## Bugs caught during initial development

1. **cntvct_el0 miscalibration (30×)**: first `rdtsc_ns()` used
   `get_cpu_freq_mhz()` (CPU 3000 MHz) but the cycle counter on this
   ARM64 host runs at 100 MHz (`cntfrq_el0`). All cache latencies came
   out as `0.1-2 ns` (compressed 30×) instead of `2-60 ns`. **Real
   physical L1 = 2.7 ns**, not 0.1 ns. Fixed in `asm_helpers.h`.

2. **test_add / test_sub optimized to nothing**: simple `sum += i` loop
   had no `volatile`, so `-O2` removed it. `0.00 ms / IPC 476K` is the
   signature. Fixed in `src/test_cpu_alu.c` with a mod-1000000007 trick.

3. **prefetch_l1 over-eager**: prefetching the next random index pulled
   the entire working set into L1, so even "RAM"-sized tests measured
   L1 hits. Fixed in `src/test_cache_hierarchy.c` and `src/latency.c`
   with `use_prefetch = (working_set > L1D)`.

4. **Parser bugs in test_sanity.py** (initial cut): wrong table headers,
   markdown bold (`**Core**`) not handled, multi-row lookup. All fixed.

## Usage

```sh
make test           # build + Layer 1 + Layer 2 (~3 min)
make smoke          # just Layer 1
make sanity         # just Layer 2 (assumes reports/ are fresh)
./test_smoke.sh     # Layer 1 directly
python3 test_sanity.py --strict   # Layer 2 with WARN as FAIL
```

## Future layers

- **Layer 3 (regression)**: save per-run snapshots, compare to baselines,
  alert on ±N% deviations.
- **Layer 4 (full)**: `make all` → run all tests → generate PDF report
  (current `generate_report.py --all`).
- **CI integration**: `make test` as pre-commit hook and PR gate.

See `PERF_NOTES.md` and `CHANGES_SUMMARY.md` for the wider refactor
context.
