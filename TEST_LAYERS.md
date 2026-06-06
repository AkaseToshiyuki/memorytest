# Test Architecture — Three Mandatory Layers

Every `make test` run executes all three test layers in sequence, then generates an HTML report.
No layer can be skipped; partial results are not accepted.

## Layer 1 — Smoke (`test_smoke.sh`)

**Goal**: every binary runs to completion and produces a valid report file.

| Check | What it catches |
|---|---|
| exit code ≠ 124 (timeout) | Infinite loops, deadlocks |
| report file created & non-empty | Segfault, report-writer bug |
| report contains header section | Truncated output |

Timeouts scale with CPU count so large machines (96-core EPYC) are not
penalised.

## Layer 2 — Sanity (`test_sanity.py`)

**Goal**: every metric falls within a physically-plausible range.

Parses the markdown reports from Layer 1. Knows what a reasonable L1
latency, bandwidth, and inter-core distance look like across x86_64,
ARM64, RISC-V, and PowerPC. Failures here mean the measurement itself
is suspect — hardware issue, kernel config problem, or a benchmarking
bug.

## Layer 3 — Regression (`test_regression.py`)

**Goal**: detect when performance changes relative to history.

Records the current run's key metrics into `history.jsonl`, then
compares against the median of the last 3 runs. Any metric that
deviates more than 50% from baseline triggers a FAIL.

This catches silent regressions: a compiler flag change that slowed
L2 bandwidth by 30%, a kernel update that altered the scheduler timing,
an inter-core algorithm change that broke NUMA affinity.

## Usage

```bash
make test    # All 3 layers (the ONLY recommended target)

# Debugging (use only when investigating a specific layer failure):
make smoke
make sanity
make regression
```
