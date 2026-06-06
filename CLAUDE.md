# memorytest — Cross-Platform Memory Benchmark Suite

## Architecture
- **Language:** C (test binaries) + Python 3 (reporting/scoring/HTML)
- **Build:** `make` compiles 7 binaries into `bin/`
- **Test:** `make test` = build → smoke → sanity → regression → report
- **Report:** `make report` → generates `reports/benchmark_report.html`
- **Key files:**
  - `src/test_*.c` — 7 test binaries (cache_hierarchy, memory_bandwidth, inter_core, cpu_alu, cpu_float, cpu_branch, cpu_multi)
  - `src/detect.c` — hardware detection (cache/channels/DRAM/CPU freq)
  - `src/util.c` — hw_cache_save/load, sudo_popen
  - `report_score.py` — scoring system (0-100 per metric)
  - `report_html.py` — HTML report generation
  - `test_sanity.py` — physical range checks
  - `test_smoke.sh` — runs all 7 binaries, verifies reports

## 🚨 CRITICAL RULES (User Red Lines)

### Absolutely NO hardcoding
- No CPU-model→cache-size tables, no channel-count guesswork, no frequency heuristics
- All data MUST come from real-time system probes: sysfs → ARM registers → x86 CPUID → dmidecode → EDAC sysfs
- If all detection fails → prompt user (with hw_cache_save for one-time entry)

### Cross-platform only
- Every change must benefit ALL architectures (ARM64 + x86_64)
- No special-casing for specific hardware (no "if Oryon then X")
- Generic algorithms only: e.g., cluster detection from latency gaps, not from CPU model

### No breaking changes
- Maintain backward compatibility of report formats, scoring keys, and HTML output
- Existing metric keys must keep working (can add new ones)

## Communication Style
- High information density, low explanation overhead
- Direct deliverables preferred over discussion
- Be honest about uncertainty — don't fabricate

## Current State (2026-06-07)

### Working: 5 auto-detection fallback chain
1. sysfs `/sys/.../cache/indexN/size` (primary; on Snapdragon X Elite: size file missing → tries ways×sets×line_size)
2. ARM cache registers (aarch64 only)
3. x86 CPUID (x86_64 only)
4. dmidecode with sanity bounds (rejects absurd values like 16GB L1)
5. EDAC sysfs `/sys/devices/system/edac/mc/` for channel count

### Known Issues to Fix

#### P0: bandwidth test uses logical cores (HT)
- `test_memory_bandwidth` on i5-8250U (4C/8T) uses 8 threads → contention drags read bandwidth down
- Should use physical core count, not logical
- Universal fix: use `sysconf(_SC_NPROCESSORS_ONLN)` for logical, but bandwidth test should cap at physical

#### P1: ALU div IPC reference (0.1) is Kunpeng 920-specific
- i5-8250U Kaby Lake integer division IPC = 0.03 → scores 30/100 (fail)
- Reference value 0.1 is based on ARM64 Kunpeng 920 which has faster integer division
- Fix: adjust reference to be architecture-neutral (e.g., 0.05) or use history-based relative scoring

#### P2: cache scan start granularity on large L1D
- Radxa Orion O6N: L1D=640KB but scan starts at 320KB → misses 16-256KB data points
- First data point at 320KB already shows 38ns (L2-like latency), L1 latency never captured
- Fix: start scan at smaller sizes regardless of detected L1D

#### P3: inter-core scoring (DONE in 64a89de)
- Auto-detects clusters from latency gaps (threshold = 2× min adjacency)
- Scores intra-cluster median CAS; cross-cluster is topology info only

## Scoring System
- 18 metrics across 5 categories: cache(4), bandwidth(3), inter_core(1), alu(6), simd(4)
- `cas_intra_median_ns` (new in 64a89de) — scores intra-cluster only
- Reference values calibrated on Kunpeng 920 (ARM64, 3GHz, 2ch DDR4-2666)
- Scoring: higher-is-better → `min(100, 100×measured/ref)`, lower-is-better → `min(100, 100×ref/measured)`

## Test Machines
| # | CPU | Key quirk |
|---|-----|-----------|
| 1 | i5-8250U 4C/8T DDR3-1867 | HT doubles thread count, perf_paranoid=4 |
| 2 | Radxa Orion O6N A720×12 | L1D=640KB, sysfs cache size empty |
| 3 | Snapdragon X Elite Oryon×12 | dmidecode reports 16GB for L1/L2, no sysfs size |
