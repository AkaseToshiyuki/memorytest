# Memory Benchmark Suite

A comprehensive cross-platform memory, cache, and CPU benchmark suite supporting x86_64, ARM64, RISC-V, and PowerPC architectures.

## Test Hierarchy

```
memorytest/
├── Memory & Cache Subsystem
│   ├── test_cache_hierarchy    # L1/L2/L3/RAM latency & bandwidth
│   ├── test_memory_bandwidth   # Multi-channel read/write/copy bandwidth
│   └── test_inter_core         # Core-to-core CAS latency (NxN matrix)
│
└── CPU Performance
    ├── test_cpu_alu            # Integer operations (add/mul/div/mod/and/or/xor/shl/shr)
    ├── test_cpu_float          # Floating-point (float/double/sqrt/sin/log/pow)
    ├── test_cpu_branch         # Branch prediction patterns
    └── test_cpu_multi         # Multi-core scaling & bandwidth saturation
```

## Quick Start

```bash
# Build all tests
make

# Run all tests (generates PDF report with charts)
python3 generate_report.py --all

# Run specific test
python3 generate_report.py --test cache_hierarchy
```

## Test Categories

### Memory & Cache Subsystem Tests

| Test | Description | Metrics |
|------|-------------|---------|
| test_cache_hierarchy | L1/L2/L3/RAM latency & bandwidth | Read/Write latency, Sequential/Random access, Multi-thread bandwidth |
| test_memory_bandwidth | Large block memory bandwidth | Read/Write/Copy GB/s, Random access latency |
| test_inter_core | Core-to-core communication | CAS latency matrix (NxN), Throughput (Mops/s) |

### CPU Performance Tests

| Test | Description | Metrics |
|------|-------------|---------|
| test_cpu_alu | Integer arithmetic operations | Ops/sec, ns/op, CPI, IPC |
| test_cpu_float | Floating-point operations | Ops/sec, ns/op, CPI, IPC, SIMD support |
| test_cpu_branch | Branch prediction patterns | ns/branch, Misprediction rate |
| test_cpu_multi | Multi-core scaling | Speedup, Efficiency%, Bandwidth saturation |

## Architecture Support

| Architecture | Status |
|-------------|--------|
| x86_64 | Supported (Intel/AMD) |
| ARM64 | Supported (ARMv8-A) |
| RISC-V64 | Supported (RV64GC) |
| PowerPC64 | Supported (POWER9+) |

## Requirements

### Build Requirements
- GCC or Clang compiler
- POSIX-compliant OS (Linux)
- pthread support

### Runtime Requirements
- Python 3.x
- reportlab (for PDF generation)
- matplotlib + numpy (for charts)

### Optional
- sudo access for PMU/cache counter detection

Install Python dependencies:
```bash
pip install -r requirements.txt
```

## Build System

```bash
make              # Build all tests (default)
make memory       # Build memory/cache tests only
make cpu          # Build CPU tests only
make clean        # Remove built binaries
make distclean    # Remove binaries and reports
```

## Report Generation

The `generate_report.py` script runs all tests and generates a **PDF report** with visualizations.

```bash
python3 generate_report.py --all              # Run all tests, generate PDF report
python3 generate_report.py --test <name>    # Run single test
python3 generate_report.py --list            # Show available tests
```

**Features:**
- PDF output with professional formatting
- Charts: Cache latency bars, Multi-core scaling lines, Bandwidth comparison, Heatmaps
- System configuration table
- Detailed data tables

**Note**: The script caches sudo credentials after first password entry for subsequent tests.

## Visualizations Generated

| Chart | Type | Description |
|-------|------|-------------|
| Cache Latency | Bar + Line | L1/L2/L3/RAM read/write latency and bandwidth |
| Multi-Core Scaling | Line | Speedup and efficiency vs thread count |
| Memory Bandwidth | Bar | Read/Write/Copy bandwidth comparison |
| CPU Operations | Bar | ALU and Float operation latencies |
| Inter-Core Heatmap | Heatmap | NxN core-to-core CAS latency matrix |

## Features

### Automatic Hardware Detection
- CPU cores and frequency (sysfs, cpuid, dmidecode, lscpu)
- Cache sizes (L1/L2/L3) via sysfs
- Memory channel count
- NUMA topology
- SIMD capabilities (SSE/AVX/NEON/SVE)

**No hardcoded values. All configuration auto-detected.**

### PMU/Cache Counter Support
When running with sudo, hardware PMU events are used:
- CPU cycle counting
- L1D/L2D/L3D cache refill events
- Branch misprediction events

Enable unrestricted PMU access:
```bash
sudo sysctl -w kernel.perf_event_paranoid=0
```

When PMU is unavailable (perf_event_paranoid > 0), timing-based estimation is used as fallback.

### Measurement Techniques
- **Batch measurement** - Reduces timing overhead
- **LCG random access** - Defeats hardware prefetchers
- **Median filtering** - Rejects outlier measurements
- **Barrier synchronization** - Ensures thread-safe multi-core tests

## Project Structure

```
memorytest/
├── README.md                 # This file
├── Makefile                 # Build system
├── generate_report.py       # PDF report generator with charts
├── run_tests.py            # Legacy test runner
├── src/
│   ├── common.h             # Header with types and declarations
│   ├── common.c             # Shared utilities, detection, PMU
│   ├── test_cache_hierarchy.c
│   ├── test_memory_bandwidth.c
│   ├── test_inter_core.c
│   ├── test_cpu_alu.c
│   ├── test_cpu_float.c
│   ├── test_cpu_branch.c
│   └── test_cpu_multi.c
├── bin/                     # Built executables (generated)
└── reports/                 # PDF reports and charts (gitignored)
    └── charts/              # Generated visualization images
```

## Validation

Results validated against LMbench reference values:

| Level | This Suite | LMbench | Unit |
|-------|------------|---------|------|
| L1 Cache | 2-3 | 1-4 | ns |
| L2 Cache | 4-6 | 3-10 | ns |
| L3 Cache | 15-25 | 10-30 | ns |
| RAM | 30-65 | 60-100 | ns |

Differences expected due to LCG random vs stride-based access patterns.

## License

MIT License
