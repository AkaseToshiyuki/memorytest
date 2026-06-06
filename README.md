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

`make test` runs three mandatory layers plus HTML report generation:

| Layer | What | How |
|-------|------|-----|
| **L1 Smoke** | Did every binary run? | `test_smoke.sh` — exit codes, report presence |
| **L2 Sanity** | Are numbers physically plausible? | `test_sanity.py` — range checks per metric |
| **L3 Regression** | Did performance change vs history? | `test_regression.py` — ±15% warn, ±30% fail |
| **Report** | HTML summary with charts/heatmaps | `report_html.py` — auto-generated after all layers pass |

All three layers must pass. Detailed architecture: [TEST_LAYERS.md](TEST_LAYERS.md)

## Test Binaries

### Memory & Cache

| Binary | Measures |
|--------|----------|
| `test_cache_hierarchy` | L1D/L2/L3/DRAM latency (pointer-chase), bandwidth per level, boundary detection |
| `test_memory_bandwidth` | Multi-thread read/write/copy bandwidth to DRAM |
| `test_inter_core` | Core-to-core CAS latency + throughput (N×N matrix, heatmaps) |

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
| `CPU_TIMEOUT` | `300 + (N-24)*4` | Seconds for CPU-bound tests (auto-scales with core count). Set higher for ARM. |
| `MEMORYTEST_SUDO_PASSWORD` | *(unset)* | Password for `dmidecode` (memory channels, DRAM speed, CPU frequency). When unset, TTY users are prompted interactively once per session. |

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
| CPU model, cores, frequency | sysfs, dmidecode (with sudo), lscpu |
| Cache sizes (L1/L2/L3) | sysfs |
| L3 per-CCD / total (multi-chiplet) | sysfs + topology |
| Memory channels | dmidecode (with sudo), or interactive prompt |
| DRAM speed (MT/s), standard (DDR4/DDR5) | dmidecode (with sudo), then lscpu, then sysfs |
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

- **`dmidecode` needs sudo.** On a real terminal (TTY), the benchmark prompts once and caches the password for 10 minutes across all binaries. In CI, set `MEMORYTEST_SUDO_PASSWORD` or leave it unset (benchmarks still run, but channels/speed will be prompted or shown as N/A).
- **PMU counters need `perf_event_paranoid ≤ 2`.** Falls back to timing-based estimates gracefully.
- **Virtualized environments** (cloud VMs, containers) may report cached or noisy latency values — the benchmark assumes bare-metal or near-bare-metal access to CPU caches.
- **Inter-core test** on large machines (96+ cores) can take 5+ minutes. Set `CPU_TIMEOUT` to scale accordingly.

## References

- [ASM_OPTIMIZATIONS.md](ASM_OPTIMIZATIONS.md) — inline assembly primitives (rdtsc, clflush, barriers)
- [PERF_NOTES.md](PERF_NOTES.md) — build optimization analysis, -O2 vs -O3
- [TEST_LAYERS.md](TEST_LAYERS.md) — detailed test architecture

## License

MIT

---

# Memorytest — 跨平台内存与 CPU 性能基准测试

零依赖、跨平台微基准测试套件，测量从 L1 缓存到 DRAM 的真实硬件延迟与带宽。
支持 x86_64、ARM64、RISC-V、PowerPC。

**一条命令运行全部测试：** `make test`

## 快速开始

```bash
git clone https://github.com/AkaseToshiyuki/memorytest.git
cd memorytest
make test
```

仅需 C 编译器和 Python 3（仅标准库），无其他依赖。

## 测试架构

`make test` 运行三层强制测试加 HTML 报告：

| 层 | 目标 | 方式 |
|---|------|------|
| **L1 冒烟** | 所有二进制是否正常运行？ | `test_smoke.sh` — 检查退出码与报告文件 |
| **L2 合理性** | 数值是否在物理可行范围内？ | `test_sanity.py` — 逐指标范围校验 |
| **L3 回归** | 性能相比历史是否变化？ | `test_regression.py` — ±15% 警告，±30% 失败 |
| **报告** | HTML 汇总含图表与热图 | `report_html.py` — 所有层通过后自动生成 |

三层全部通过才算完成。详见 [TEST_LAYERS.md](TEST_LAYERS.md)

## 测试二进制

### 内存与缓存

| 二进制 | 测量内容 |
|--------|----------|
| `test_cache_hierarchy` | L1D/L2/L3/DRAM 延迟（指针追踪）、各级带宽、边界检测 |
| `test_memory_bandwidth` | 多线程读写复制带宽 |
| `test_inter_core` | 核间 CAS 延迟 + 吞吐量（N×N 矩阵、热图） |

### CPU

| 二进制 | 测量内容 |
|--------|----------|
| `test_cpu_alu` | 整数 IPC（加减乘除、与或异或） |
| `test_cpu_float` | 浮点 IPC（加减乘、FMA、SIMD 点积） |
| `test_cpu_branch` | 分支预测延迟（always-taken、never-taken） |
| `test_cpu_multi` | 8 线程多核扩展效率 |

## 构建

```bash
make              # 默认 -O2 构建全部二进制
make memory       # 仅内存/缓存测试
make cpu          # 仅 CPU 测试
make clean        # 删除二进制
make distclean    # 删除二进制和报告
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CPU_TIMEOUT` | `300 + (N-24)*4` | CPU 测试时间上限（秒），按核心数自动缩放。ARM 平台建议设置更高。 |
| `MEMORYTEST_SUDO_PASSWORD` | *(未设置)* | `dmidecode` 密码（内存通道数、DRAM 速度、CPU 频率）。未设置时终端用户每次会话交互式提示一次。 |

## 自动检测

所有硬件参数运行时检测，无硬编码值：

| 参数 | 方法 |
|------|------|
| CPU 型号、核心数、频率 | sysfs、dmidecode（需 sudo）、lscpu |
| 缓存大小（L1/L2/L3） | sysfs |
| 多 CCD 总 L3 | sysfs + 拓扑 |
| 内存通道数 | dmidecode（需 sudo），或交互式提示 |
| DRAM 速度（MT/s）、标准（DDR4/DDR5） | dmidecode（需 sudo），其次 lscpu、sysfs |
| NUMA 拓扑 | sysfs |

**多 CCD 支持：** 对 AMD EPYC/Ryzen 等多 CCD 处理器（如 EPYC 7R13：8 CCD × 32 MB L3 = 256 MB 总 L3），基准测试使用总计 L3 决定缓冲区大小和测量点标记，避免在大缓存服务器上将 DRAM 测量误落到 LLC 内。

## 测量方法

### 延迟（指针追踪）

真正的串行依赖链：每次加载产生下一次加载的地址，击败 CPU 的乱序引擎和内存级并行。测量真实的各级缓存加载延迟。

- ≤ 16 MB 缓冲区：Fisher-Yates 洗牌，全排列数组
- > 16 MB 缓冲区：LCG 生成索引 + 位集去重（防止自环）

### 逐出策略（边界检测）

| 缓冲区大小 | 轮间逐出 | 效果 |
|-----------|----------|------|
| ≤ L3 | 仅首次运行前 | 第 2..N 次热缓存 — 测量真实缓存命中延迟 |
| > L3 | 每轮 | 每轮冷缓存 — 测量真实 DRAM 延迟 |

此选择性逐出保留了跨缓存边界的自然延迟曲线，实现准确的 L1→L2→L3→DRAM 转换检测。

### 带宽

缓冲区自动缩放至 4× 总 L3（最小 1 GB，最大 8 GB），确保工作集超出末级缓存。多线程顺序访问饱和所有内存通道。

## 实测数据

| 级别 | EPYC 7R13 (x86_64) | Kunpeng 920 (ARM64) |
|------|---------------------|----------------------|
| L1D | 1.9 ns | 1.5 ns |
| L2 | 2.3 ns | 1.8 ns |
| L3 | 11.9 ns | 7.6 ns |
| DRAM | 89.2 ns | 67.6 ns |
| 读带宽 | 127 GB/s (8通道 DDR4-2666) | 36 GB/s |

## 评分

`make test` 生成 HTML 报告（`reports/benchmark_report.html`），包含逐指标评分（0-100）和总体字母等级（A-F）。评分依据检测到的硬件自适应：DRAM 延迟 90 ns 在 EPYC 上得 100/100，但在 DDR5 系统上得分更低。

## 项目结构

```
memorytest/
├── README.md
├── Makefile
├── tests.json                  # 测试注册表
├── test_smoke.sh               # 第 1 层：冒烟测试
├── test_sanity.py              # 第 2 层：合理性检查
├── test_regression.py          # 第 3 层：回归检测
├── generate_report.py          # 运行全部二进制并生成报告
├── report_html.py              # HTML 报告生成器（仅标准库）
├── report_score.py             # 评分引擎
├── src/                        # C 源码
├── bin/                        # 构建产物（gitignore）
└── reports/                    # 报告与 HTML（gitignore）
```

## 注意事项

- **`dmidecode` 需要 sudo。** 在真实终端（TTY）中，基准测试会提示一次密码并缓存 10 分钟供所有二进制使用。CI 中可设置 `MEMORYTEST_SUDO_PASSWORD`，或留空（测试仍可运行，通道/速度将被提示或显示 N/A）。
- **PMU 计数器需要 `perf_event_paranoid ≤ 2`。** 否则自动退回到基于时间的估算。
- **虚拟化环境**（云主机、容器）可能报告缓存值或噪声延迟——基准测试假定裸金属或近裸金属的 CPU 缓存访问。
- **核间测试**在大机器（96+ 核）上可能需要 5+ 分钟。设置 `CPU_TIMEOUT` 相应调整。

## 参考文档

- [ASM_OPTIMIZATIONS.md](ASM_OPTIMIZATIONS.md) — 内联汇编原语（rdtsc、clflush、屏障）
- [PERF_NOTES.md](PERF_NOTES.md) — 构建优化分析
- [TEST_LAYERS.md](TEST_LAYERS.md) — 测试架构详解

## 许可证

MIT
