# Memorytest — Portable Memory & CPU Benchmark Suite

A CPU/memory microbenchmark suite with Linux, macOS and Windows backends and a
Qt Widgets desktop. Published V0.1.0 releases target x64 and ARM64 on all three OSes.
The 0.1.1 working branch adds grouped results, bilingual themes and supervised stress
sessions; it is a test build, not a published release.
Linux CLI additionally contains RISC-V and PowerPC code paths.
Architecture code paths are not a certification of equal workloads or measurement
validity. macOS source adaptation and cross-build checks do not replace native Mac
acceptance; see the platform scope and comparability audit below.

**Single command to run everything:** `make test`

Desktop installation/builds: see [V0.1.0 desktop distributions](#v010-desktop-distributions).
The source archives retain the CLI; installing the GUI is optional.

## Quick Start

```bash
git clone https://github.com/AkaseToshiyuki/memorytest.git
cd memorytest
make test
```

A C compiler and Python 3.10+ are sufficient for HTML tables and inline SVG charts.
Heatmaps and scaling charts are also inline SVG; Matplotlib is not needed.

### macOS / Apple Silicon

Install Xcode Command Line Tools (`xcode-select --install`) and Python 3.10+.
Use an arm64 terminal/Python on Apple Silicon, not Rosetta. No root access required.

```bash
python3 run_benchmarks.py --system-info       # sysctl topology/OS metadata only
make tests                                  # six benchmarks + unsupported inter-core entry point
make check-compute check-workflow check-platform
make test MODULE=cpu_multi                   # single + all-online CPU throughput
open reports/cpu_multi_report.html
make test                                   # all selected; inter_core explicitly unsupported
```

macOS uses public sysctl/Mach detection, dynamically enumerates `hw.nperflevels`
and `hw.perflevelN.*` (OS names, P/E core counts, L1 and shared L2), and records
page/cache-line sizes and the hardware/OS model. Missing data stays unknown.
It never multiplies a cluster-shared L2 by all cores or invents an L3/SLC size.
ComputeMark requests and verifies USER_INITIATED QoS, **not CPU-ID/P-core pinning**.
Exact-pair inter-core matrices and Linux PMU counters are unsupported. Other selected
modules still run, and HTML records unsupported status, never fake measurements.
Apple Silicon cache diagnostics use bounded capacity eviction, not privileged cache
invalidation; this is not guaranteed DRAM residency or a cross-OS cache protocol.

Scored v2 kernels/units remain unchanged; GCC and Clang use their corresponding
no-vectorization flags. `macos-qos-user-initiated` and `linux-pinned` are distinct
placement policies: **do not mix their scores in one ranking**. Rosetta execution
is rejected by the scored binary. Native optimization is allowed for diagnostics:
`make opt-native MARCH_OVERRIDE=1 BUILD_DIR=bin-native` uses `-mcpu=native` on ARM64.
It does not modify the fixed scalar scoring kernel. A validated native SIMD score
would be a separate workload/profile, not a relabelled scalar v2 score.

`.github/workflows/portability.yml` defines Linux GCC/Clang, macOS ARM64 and Intel
build/reference/API checks. Adding the workflow does not mean it has run or passed;
CI/virtualized timings are not hardware leaderboard results.

Local verification on 2026-10-14 (Linux host): `make check-compute check-workflow
check-platform` ran 42 checks: 37 passed, 5 native-Mac checks skipped. All seven Linux
entry points built; a 1 MiB/two-thread memory diagnostic completed. Zig 0.14.1's
Clang cross-toolchain compiled and linked seven arm64 and seven x86_64 Mach-O
executables, plus the bounded macOS API/cache probe. A separate Clang/Linux
compute run passed 18 checks with one native-Mac skip before the HTML-only fixture
was added. This is **not** Apple Clang/macOS runtime validation and no GitHub CI run
has been performed here. Local evidence: `/tmp/memorytest-darwin-cross-BWIvhU/`
(temporary; not included in the repository or a release).

## Test Architecture

Tests are organized by **functional module**, not by smoke/sanity/regression layers.

```bash
make test                                 # All 7 modules + summary HTML
make test MODULE=cpu_alu                   # One module + its own HTML
make test MODULES="cpu_alu cpu_float"       # Selected modules + scoped HTML
make test CATEGORY=cpu                    # CPU group (or CATEGORY=memory)
make list-tests                           # List available modules
# Equivalent direct CLI:
python3 run_benchmarks.py --module cpu_alu
```

All-module output: `reports/benchmark_report.html`. Single-module output:
`reports/<module>_report.html`. Multiple selected modules: `reports/selected_report.html`.
Each invocation also saves an independent archive under `reports/runs/<run-id>/`,
including its manifest, logs, raw results, and self-contained HTML.
Only selected modules are built/run. Failed or timed-out modules are marked in HTML,
their partial measurements are excluded, and the command returns nonzero.
The remaining selected modules still run. Historical raw reports are never reused.

`make report` regenerates the most recently finished run's archived HTML;
`make report RUN_DIR=reports/runs/<run-id>` selects an archive explicitly.
`make smoke`, `make sanity`, and `make regression` remain optional **legacy developer**
tools and still operate on top-level `reports/*.md`; the new workflow does not invoke
them or update their historical data. `make check-workflow` tests the module runner
without hardware workloads. Details: [TEST_LAYERS.md](TEST_LAYERS.md)

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
| `test_cpu_multi` | Multi-core scaling efficiency across thread counts |

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
| `CPU_TIMEOUT` | `min(2400, max(300, 300 + (N-24)*4))` | Default per-module timeout, except inter-core. Explicit CPU_TIMEOUT overrides the formula. |
| `MEMORYTEST_SUDO_PASSWORD` | *(unset)* | Password for `dmidecode` (memory channels, DRAM speed, CPU frequency). When unset, TTY users are prompted interactively once per session. |

```bash
# Example: give CPU tests more time on slow hardware
make test MODULE=cpu_alu ARGS="--timeout 600"

# Example: enable full hardware detection
export MEMORYTEST_SUDO_PASSWORD=$(cat /path/to/password_file)
make test
```

## Auto-Detection

Hardware discovery uses the following sources; some legacy fallback values remain guesses and are not a basis for ComputeMark v2:

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

A serial dependency chain makes each load produce the next address. Diagnostic v2
uses one randomized permutation cycle with a node every 64 bytes over the full
listed working set, with no auxiliary permutation allocation or sparse-chain shortcut.

### Warmup and hierarchy detection

Ordinary OS pages, a complete warm traversal and discarded calibration precede
five equal-work samples. The default minimum warmup is 50 ms and the per-sample
target is 150 ms. Results use the median and include the sample range/median.
No privileged flush is used. Hierarchy detection reports sustained latency-jump
intervals separately from OS-reported cache sizes; TLB, page and scheduling
effects can also cause transitions. Old eviction-based results are incompatible.

### Bandwidth

Cache bandwidth uses one thread and equal total working sets for read/write/copy.
Memory diagnostics add scale, add, triad, 64/256-byte-stride reads and dependent-load
latency. Useful bytes are divided by one common worker interval, in decimal MB/s;
no inferred cache-line or write-allocation traffic is counted. Three memory arrays
default to 256 MiB each (increased for reported large caches, capped at 2 GiB each)
and together use at most half of observed available memory. Cache residency and
OS/NUMA placement remain measurement conditions, not guaranteed DRAM saturation.

## Validation

Historical results (not rerun or scientifically revalidated by this audit):

| Level | EPYC 7R13 (x86_64) | Kunpeng 920 (ARM64) |
|-------|---------------------|----------------------|
| L1D | 1.9 ns | 1.5 ns |
| L2 | 2.3 ns | 1.8 ns |
| L3 | 11.9 ns | 7.6 ns |
| DRAM | 89.2 ns | 67.6 ns |
| Read BW | 127 GB/s (8-ch DDR4-2666) | 36 GB/s |

## Scoring

`make test MODULE=cpu_multi` now runs **ComputeMark v2**, independently of the
legacy ALU, floating-point and branch modules. v1 extrapolated values are no longer
accepted or silently converted. Old and new points are **not comparable**.

```bash
make test MODULE=cpu_multi                             # 1 thread and all allowed logical CPUs
make test MODULE=cpu_multi ARGS="--cpu-threads 2"       # explicitly limited capacity
make score                                            # read this run's v2 points
make check-compute                                    # independent references + <=2 CPU native checks
```

For single-thread and multi-thread results **separately**:

`points = floor(1000 × sqrt(integer_Gop/s × scalar_FP64_GFLOP/s) + 0.5)`

This is an unbounded, fixed-unit benchmark score, not a percentage or normalization
against a reference machine. The two components are measured, never extrapolated:

- Integer: four uint64 xorshift streams, 24 semantic shift/XOR operations per iteration.
- FP64: four binary64 logistic recurrences, 12 scalar floating-point operations per iteration.
- The same deterministic source/seed/operation definitions on every ISA; no libm,
  PMU, guessed CPU clock or ISA-specific SIMD path is used by the scored kernels.
- Linux: one worker per allowed logical CPU ID (including SMT), with verified
  affinity; single-thread uses the first allowed ID. macOS: one worker per online
  logical CPU, with verified USER_INITIATED QoS but OS-selected placement. Separate
  these policies when comparing; QoS does not guarantee P-core-only execution.
- Warmup and thread creation are excluded. Total completed work is divided by a
  common monotonic wall-time interval; calibration work is excluded. Five accepted
  samples per workload/thread count, each at least 100 ms, median throughput.
- `portable-scalar-v2` has pinned flags, no vectorization, fast-math, contraction
  or LTO. `make opt`/`ofast`/`opt-native` do not change this scored profile.
- Independent reference-vector self-tests, source hash, exact operation accounting,
  deterministic checksums, timer, profile and complete sample sets are checked.
  Unsupported FP environments, failed affinity/QoS/creation, or malformed data give
  **no score**. Repeat spread above 15% marks the result unstable and not rankable.

Compare only matching version/workload/profile/compiler and allocation policy.
All-allowed runs compare each process's available capacity; a limited-2 result is
not a whole-CPU result. Keep OS conditions, quota/SMT policy and background load
controlled. Reports record allowed IDs, compiler, kernel/architecture, source/binary
hashes and visible cgroup/governor information; unavailable constraints remain
unknown. A source hash is provenance, **not anti-tamper certification**.

This is a scalar mixed-workload rating, **not** peak SIMD/FLOPS or a universal
application-performance score. Semantic operations are not retired instructions.
Architecture-independent C and reference checks establish a protocol; they do not
substitute for real ARM64/RISC-V/PowerPC hardware validation.

HTML displays validated ComputeMark v2 points only. The deprecated `--legacy-scores`
flag is accepted but does not restore old grades. Algorithm limitations remain in
this README; the result UI shows data, conditions and concise status only.

Protocol references: [GCC optimization/FP semantics](https://gcc.gnu.org/onlinedocs/gcc-15.1.0/gcc/Optimize-Options.html),
[POSIX monotonic clocks](https://pubs.opengroup.org/onlinepubs/9799919799/functions/V2_chap02.html),
[SPEC reporting/compiler discipline](https://www.spec.org/cpu2017/Docs/runrules.html).
The point formula above is this project's definition, not a SPEC formula.

## Project Structure

```
memorytest/
├── README.md
├── Makefile
├── tests.json                  # Test registry
├── run_benchmarks.py           # Module runner + isolated run archives
├── benchmark_registry.py       # Shared module selection
├── tests/                      # Workflow acceptance fixtures
├── test_smoke.sh               # Optional legacy developer check
├── test_sanity.py              # Optional legacy range check
├── test_regression.py          # Optional legacy history check
├── generate_report.py          # Legacy runner and optional chart helpers
├── report_html.py              # HTML report generator (stdlib only)
├── report_score.py             # Legacy metrics and v2 integration
├── compute_score.py            # Strict v2 protocol validation and absolute points
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

- **The module runner is non-interactive.** It does not ask for passwords; configure hardware-detection credentials beforehand if needed. **`dmidecode` needs sudo.** On a real terminal (TTY), the benchmark prompts once and caches the password for 10 minutes across all binaries. In CI, set `MEMORYTEST_SUDO_PASSWORD` or leave it unset (benchmarks still run, but channels/speed will be prompted or shown as N/A).
- **PMU counters need `perf_event_paranoid ≤ 2`.** Falls back to timing-based estimates gracefully.
- **Virtualized environments** (cloud VMs, containers) may report cached or noisy latency values — the benchmark assumes bare-metal or near-bare-metal access to CPU caches.
- **Inter-core test** on large machines (96+ cores) can take 5+ minutes. Use `make test MODULE=inter_core ARGS="--timeout 3600"` to set its time limit.

## References

- [ASM_OPTIMIZATIONS.md](ASM_OPTIMIZATIONS.md) — inline assembly primitives (rdtsc, clflush, barriers)
- [TEST_LAYERS.md](TEST_LAYERS.md) — detailed test architecture

## License

MIT

---

# Memorytest — 跨平台内存与 CPU 性能基准测试

包含 Linux 与 macOS 后端的内存与 CPU 微基准套件；macOS 面向 Apple Silicon / Intel。
各路径的可比性不同，不能将“存在代码支持”视为已通过跨平台验收；详见算法审计。

**一条命令运行全部测试：** `make test`

## 快速开始

```bash
git clone https://github.com/AkaseToshiyuki/memorytest.git
cd memorytest
make test
```

仅需 C 编译器和 Python 3.10+ 即可生成 HTML 表格与内联 SVG，包括热图与扩展曲线；不需要 Matplotlib。

## 测试架构

按**功能子模块**组织测试，不再把 smoke / sanity / regression 作为用户测试层级。

```bash
make test                                 # 默认运行全部 7 个模块，生成汇总 HTML
make test MODULE=cpu_alu                   # 仅运行整数模块，生成专属 HTML
make test MODULES="cpu_alu cpu_float"       # 运行指定多个模块
make test CATEGORY=memory                 # 运行内存组；CPU 组用 CATEGORY=cpu
make list-tests                           # 查看可选模块
python3 run_benchmarks.py --module cpu_alu  # 等价的直接入口
```

全量报告：`reports/benchmark_report.html`；单模块报告：
`reports/<模块名>_report.html`；多模块选择报告：`reports/selected_report.html`。
每次执行另有独立归档 `reports/runs/<run-id>/`，保存状态清单、日志、原始结果和 HTML。
只编译和执行选中的模块；报告只展示本次选中的模块，不混入旧数据。
失败或超时会在 HTML 中明确标记、排除半成品数据并返回非零退出码，其余选中模块继续执行。

`make report` 可重新生成最近完成的一次归档 HTML；也可用
`make report RUN_DIR=reports/runs/<run-id>` 指定某次运行。
`make smoke`、`make sanity`、`make regression` 仅保留为可选旧版开发校验，
仍读取顶层 `reports/*.md`，不会自动应用于新归档。
`make check-workflow` 使用轻量夹具验证流程，不执行硬件跑分。
详见 [TEST_LAYERS.md](TEST_LAYERS.md)。

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
| `test_cpu_multi` | 不同线程数下的多核扩展效率 |

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
| `CPU_TIMEOUT` | `min(2400, max(300, 300 + (N-24)*4))` | 除核间模块外的默认超时（秒）；显式设置可覆盖默认公式。 |
| `MEMORYTEST_SUDO_PASSWORD` | *(未设置)* | `dmidecode` 密码（内存通道数、DRAM 速度、CPU 频率）。未设置时终端用户每次会话交互式提示一次。 |

## 自动检测

硬件信息使用以下探测来源；旧模块仍有回退猜测，不能作为 ComputeMark v2 的评分依据：

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

每次加载产生下一次加载的地址。diagnostic-v2 在完整工作集上每隔 64 字节布置一个
节点，组成覆盖所有节点的单一随机环；不再按大小改用稀疏链，也不需要额外排列数组。

### 预热与层级检测

使用普通系统页，先完成全链预热和不计入结果的校准，再采集 5 次等工作量样本。
默认最低预热 50 ms、单次目标 150 ms，记录中位数与极差／中位数。不执行特权清缓存。
层级检测输出持续的延迟跃迁区间，系统报告的容量单列；TLB、分页及调度也可能造成
跃迁，不能据此保证精确缓存容量。旧逐出协议的结果不能与新协议直接混用。

### 带宽

缓存带宽使用单线程，读取／写入／复制的总工作集保持一致。内存测试增加缩放、相加、
Triad、64／256 字节步长读取与依赖链延迟。带宽按共同计时窗口内的有效字节数计算，
单位为十进制 MB/s，不推算缓存行或写分配流量。三个内存数组默认各 256 MiB，针对
较大已知缓存适当增大、每个最多 2 GiB，合计不超过检测到的可用内存的一半。
缓存驻留、系统／NUMA 调度仍影响结果，不能保证所有工作集都来自 DRAM 或跑满全部通道。

## 实测数据

以下为历史记录，本次未重跑或重新认证这些测量值：

| 级别 | EPYC 7R13 (x86_64) | Kunpeng 920 (ARM64) |
|------|---------------------|----------------------|
| L1D | 1.9 ns | 1.5 ns |
| L2 | 2.3 ns | 1.8 ns |
| L3 | 11.9 ns | 7.6 ns |
| DRAM | 89.2 ns | 67.6 ns |
| 读带宽 | 127 GB/s (8通道 DDR4-2666) | 36 GB/s |

## 评分

CPU 评分改为 **ComputeMark v2 实测标量基准**，由 `cpu_multi` 模块独立完成，
不再把旧 ALU/SIMD 单核结果乘以另一套负载的加速比。v1 已停用，不能与 v2 分数比较。

```bash
make test MODULE=cpu_multi                       # 单线程 + 全部允许的逻辑 CPU
make test MODULE=cpu_multi ARGS="--cpu-threads 2" # 指定线程数，明确标记为受限容量
make score                                      # 查看最近运行的 v2 绝对分数
make check-compute                              # 独立参考与最多 2 CPU 的本机验证
```

单线程、多线程分别计算：

`分数 = floor(1000 × sqrt(整数 Gop/s × 标量 FP64 GFLOP/s) + 0.5)`

无满分上限，不依赖某个参考 CPU。1000 是固定的分数单位换算系数。
整数每轮 4 路 xorshift，共 24 个移位/XOR 操作；FP64 每轮 4 路 logistic 递推，
共 12 个浮点操作。使用同一套 C 内核、固定种子、明确操作数与独立参考向量。

每个负载与线程数均预热后实测 5 次、每次至少 100 ms，取吞吐量中位数。
操作数除以所有线程共同的墙钟时间；不计校准、预热或创建线程时间。
使用 `CLOCK_MONOTONIC`，不按 MHz 换算时间，不依赖 PMU 或特权探测。
内核固定为 `portable-scalar-v2`：禁用自动向量化、fast-math、浮点收缩和 LTO；
其他构建优化预设不会改变评分内核。

仅允许完整、计数一致、自检通过、调度策略验证成功、版本/负载/源码哈希/构建配置匹配的数据评分。
Linux 验证实际绑核；macOS 验证 USER_INITIATED QoS，明确记录未绑核，拒绝 Rosetta 转译评分。
线程失败、缺失采样、不支持的浮点环境不产生分数。
最大重复波动超过 15% 时明确标记“不稳定，不用于排名”。

比较条件：相同版本、负载、构建配置、编译器与 CPU 分配策略。
Linux 默认测试当前进程允许的逻辑 CPU（含 SMT）；macOS 默认线程数来自 `hw.logicalcpu`。
`limited-2` 仅表示指定 2 个线程的吞吐量，不能当作整颗 CPU 的成绩。
Linux 单线程使用第一个允许的 CPU；macOS 由系统调度，不保证性能核或固定 CPU。
两种调度策略分开标识，不混为同一个排行榜；同公式同单位不等于测试条件完全相同。
报告记录允许的 CPU ID、编译器、内核、架构、源码/二进制哈希和可见的 cgroup/governor；
未知配额不等于没有限制。

此分数反映固定**标量负载**的计算吞吐量，不代表 SIMD 峰值或全部实际应用性能。
当前仅完成 x86_64/Linux/GCC 原生验证；跨 ISA 语义自检和协议约束不等于其他硬件已验收。

## 跨平台与算法审计（2026-10-14）

以下记录首次审计时的状态；浮点、分支、内存、缓存及采样的后续修复见下方追加章节。

结论：**存在多架构代码，不等于所有模块具备跨硬件可比性。**
本轮实现 CPU 评分修正及公共计时/探测修正；其他模块按以下结论保留为诊断工具，
没有把待修复的算法问题标成已解决。

| 模块 | 当前结论与证据 | 后续修改建议 |
|---|---|---|
| `cpu_multi` | 已重写为 v2 公共标量内核；直接实测总工作量/共同时间；移除旧固定总工作量、浮点溢出负载、带宽混测及不可取消屏障。仅本机 x86_64 原生验证。 | ARM64/RISC-V/PowerPC 真机执行相同参考向量、反汇编检查、重复采样；建立跨机验收矩阵。 |
| `cpu_alu` | `test_add` 等使用 volatile 依赖链；部分测试同时执行多种操作。估算 IPC 不是退休指令 IPC，跨 ISA 的 IPC 也不能直接当算力。 | 保留“特定依赖链耗时”定位，明确操作数；硬件 IPC 与语义 ops/s 分开，不用 MHz 估算 IPC 排名。 |
| `cpu_float` | SIMD 循环 `i += 2` 却额外乘 2 计数；不同 FMA 路径递推式不同；无 SIMD 路径是空操作。GCC 13.3 本机汇编确认 float div 被移出循环、SSE2 FMA 路径只有加法。 | 统一标量/SIMD 数学表达式与操作计数；防消除、避免上溢/次正规数；不支持的 ISA 输出 N/A；原始汇编与数值双验收。 |
| `cpu_branch` | 常真/常假分支可消失；本机 `test_never_taken` 只剩写零和返回。libc `rand()` 不保证跨库序列一致；PMU 错误率分母目前是 instructions 而非 branches。 | 固定 PRNG/分支序列；确认实际条件跳转保留；使用真实 branch-misses/branches；按实现/ISA 单独解释。 |
| `memory_bandwidth` | 汇总各线程不同计时窗的速率，不是共同时间窗的总字节数；64 字节步长并非加载/写入完整 64 字节；MB/s 与 MiB/s 混用。随机访问不是严格依赖链延迟。 | 同步开始并按实际完成字节/共同时间计算；分清有效载荷与估算缓存流量，统一 SI/IEC 单位；固定尺寸协议与容量自适应诊断分开。 |
| `cache_hierarchy` | 指针追踪有合理基础，但链长、工作集、逐出策略和采样位置随硬件改变；RISC-V 无 Zicbom 时 fence 不能等价于 flush，ARM 缓存维护指令依赖权限。旧 MHz 换算时间错误已在公共路径移除。 | 固定字节数/页策略/依赖链分布用于横向比较；探测功能单独报告；不支持逐出时标注策略，不冒充等价实现。 |
| `inter_core` | 是拓扑相关的原子通信测试，不能把不同核数/NUMA 的矩阵均值直接排名。硬编码 64B 槽位、忽略绑核返回、以 `0..物理核数-1` 代替真实 CPU ID；完成/再派发状态存在竞态风险。 | 使用允许 CPU ID 与真实拓扑；检查原子是否 lock-free；按缓存行对齐；修复任务代际/完成协议；分开同核簇、跨芯粒、跨 NUMA。 |

公共问题与本次处理：

- 已修复：缓存路径的 `rdtsc_ns()` 统一使用单调墙钟，消除 CPU MHz/TSC、RISC-V/PowerPC 猜频率和惰性初始化竞态；旧时钟数据不可直接与新值对比。
- 已修复：x86 SIMD 检测检查 CPUID 最大叶号及 OS 保存状态；ARM/RISC-V 使用内核 HWCAP，
  移除不安全的 EL1/misa 直接探测与猜测 RVV 宽度。
  依据 [Linux ARM64 HWCAP](https://www.kernel.org/doc/html/v7.0/arch/arm64/elf_hwcaps.html) 的用户态能力约定。
- 本轮补充：内存带宽双缓冲合计预算限制到可用内存的 50%，切片按完整字对齐，
  拒绝过小/溢出缓冲；这不代表操作系统一定能兑现内存分配。
- 未解决：旧 Linux 模块的硬件容量/DRAM 回退猜测、容器有效内存预算，
  以及通用构建预设改变后可能复用旧二进制（优化构建应使用独立 BUILD_DIR）。
  CPU v2 不链接这些探测/内存路径，且每次强制按固定评分配置重建。
- OS 范围：已新增 macOS 公开接口后端，Linux 专用探测/PMU 不在 Mac 构建中链接。
  Mac/Windows 精确核间矩阵不可用；其他模块的旧算法限制仍成立。Windows 新增 Win32 后端，
  QPC 计时和当前进程处理器组内的线程绑定；不能据此声称覆盖整台多处理器组机器。
  原生 Apple Silicon 运行与跨硬件分数验收仍需真机，不能以交叉编译代替。

建议优先级：P1 先修浮点/分支的负载与计数、内存共同计时窗及资源预算；
P1 修核间绑定与完成协议；P2 再统一缓存实验协议、平台覆盖和多编译器验收。
原始复核证据：`/tmp/memorytest-portability-N6nJeR/` 中的构建/测试日志及
`cpu_float.s`、`cpu_branch.s`（临时目录可能被系统清理）。

验收口径：独立 Python uint64 掩码算术和 binary64 参考；CPU v2 原生有限核运行、
计数/公式独立复算、错误数据拒绝、线程创建/绑核故障注入、时钟参考检查；
流程夹具验证选中模块与报告隔离。没有运行全套重内存/核间硬件测试。

## 0.1.1 测试版本

### 追加的测试方法修订（diagnostic-v2）

- 缓存页和 HTML 分为缓存带宽、缓存延迟、缓存层级检测三部分；原 `cache_hierarchy`
  模块 ID 保持兼容，层级检测复用本次延迟样本，不重复跑一次测量。
- 浮点保留每个平方根／除法结果，限制每块数据范围，修正 SIMD 标量分量计数，
  不支持的 SIMD 不再输出空操作。分支使用固定长序列和真实条件跳转；耗时不是
  预测准确率，没有硬件计数器就不显示虚构错误率。当前显式分支内核支持 x64／ARM64。
- 整数、浮点、分支、内存和缓存使用校准、预热、5 次样本中位数；报告与界面共用同一批
  样本。核间延迟增至 15 个批次并丢弃两批预热，吞吐量取 5 次中位数，修正完成通知竞态。
  核间的 CPU ID／拓扑／绑定限制仍需单独验证，Mac／Windows 仍不输出不可靠矩阵。
- 不再用标称 MHz 生成 CPI／IPC。以上均为诊断工作负载，不能视为峰值执行单元性能。
  ComputeMark v2 的工作内核、评分公式及既有校准／预热／中位数协议保持不变。
- FPU 压力使用 8 条独立向量链（NEON FMA、SSE2 或运行时检查后的 AVX2 FMA），
  每个分量仍按独立参考校验。Metal／OpenCL 保留 3 个有界待执行批次，逐批完成校验；
  不增加 CPU 回退或多显卡聚合。授权成功后隐藏重复授权选项，采样故障提供重试。

本地数值、计数、退出、采样和界面验证见 `TEST_LAYERS.md`。M4 Pro 实际 GPU 利用率、
FPU 功耗及长时间稳定性仍需新安装包真机测试；不能从编译通过或其他 GPU 的短时运行推断。

本轮仅交付工作分支、验证记录和测试构件；没有创建 0.1.1 标签或 Release，
也不会自动替换 V0.1.0 资产。三大界面分组是缓存、内存、核心性能测试，
原 CLI 模块 ID 和 `memory` / `cpu` 分类语义不变。

桌面原生图表与离线 HTML 使用同一结构化结果模型，支持中文／英文和浅色／深色。
默认中文系统用中文，其他系统用英文；默认深色，选择会保存。结果页只显示实测指标、
测试条件、曲线、表格和简短状态，原始引擎日志单独打开。Mac 无 L3 不再导致缓存报告
提前丢失；缺失缓存数据不推算，性能核／能效核缓存保留系统报告值。
Windows ARM64 与 Apple Silicon 缓存扫描使用有界容量逐出，不执行用户态不可用的
逐行缓存失效指令，也不声称容量逐出等同于严格清空所有缓存。

压力测试入口单独位于最右侧，不进入默认基准流程；在独立进程中运行，
不能与基准测试同时启动，也不生成性能评分。首页新增一次性读取的硬件概览，
标称／最高频率与实时监控分开；系统未公开的内存速率等字段明确显示不可用。
CPU、FPU、缓存、内存共用一套线程预算；GPU 使用 Metal（macOS）或设备驱动的
OpenCL（Windows/Linux），找不到 GPU 后端时禁用，不回退到 CPU 冒充 GPU。
默认仅 CPU、手动停止；运行中切换负载立即生效，全取消则仅监控。
内存默认可用内存 25%、最多 2 GiB，调整上限 50% 并扣除缓存／GPU 分配预算。

```bash
# 只探测能力，不启动压力负载
python desktop_app.py --stress-worker --probe
# 有限验收示例：单线程、1 MiB、5 秒；不默认执行
python desktop_app.py --stress-worker --mode cpu --threads 1 --memory-mib 1 --duration 5
```

监控每秒采样，图表默认显示最近五分钟，可选择 1–10 分钟，缓存硬上限十分钟；
所有温度曲线排在占用率、频率等信息之前。Min/Max/Avg 使用本会话全部有效样本。
隐藏曲线不改变统计，缺失读数显示不可用而不是零。完整样本与负载切换保存在
`stress-runs/<run-id>/samples.jsonl` 和 `manifest.json`，可以导出自包含 HTML。

| 系统 | 普通权限 | 主动开启的增强读取 |
|---|---|---|
| Linux | 系统负载、内存、可读 hwmon/DRM；按 CPU 策略标识区分硬件频率与 OS 报告频率 | 读取 RAPL；授权后补充受限的只读 hwmon/RAPL |
| macOS | 系统负载、内存、可读 SMC 温度区域和系统临界热状态 | SMC 已识别功耗／电压／风扇；授权后用 powermetrics 读取原生 E/P 核簇活跃平均频率和功耗 |
| Windows | 系统负载、内存；已有 LibreHardwareMonitor WMI 提供的传感器和核心频率 | 数据源拒绝普通用户读取时，可申请本次会话的只读授权 |

首次启动先探测普通权限；有额外权限需求时先说明用途，经用户同意再打开系统授权框。
仅固定功能的原生读取助手提权，界面、Python 工作进程与负载仍保持普通用户身份。
授权仅本次会话有效，不安装驱动或常驻服务，不写 SMC，不控制风扇／频率／电压；
拒绝授权保留普通监控，之后可通过增强监控重试。授权不会凭空创建 Windows 数据源。
传感器定义和 SMC 元数据缓存；Windows 使用持续读取进程；隐藏监控页且无负载时暂停。
SMC 为机型相关私有接口，兼容性参考 [Stats](https://github.com/exelban/stats)，
已知键按芯片代际命名为热区域而非单核温度，未知键保留 ID 并注明未映射。
实时频率只使用数据源实际返回的读数，不以标称频率代替，不按频率高低猜测大小核。
Apple Silicon 的频率是核簇活跃期间的平均值，不是逐核瞬时频率；其他系统保留来源的
核心／策略标识，只有数据源给出大小核语义时才可据此区分。
协议参考许可随包提供。没有温度数据时不承诺过热保护；可靠临界信号、计算错误和
工作进程异常会停止负载，关窗与父进程退出会清理子进程。

四个 SVG 图标方向和浅／深／小尺寸预览在 `packaging/icon-concepts/`。
用户已选定 04「脉冲」，正式源文件为 `packaging/memorytest.svg`；构建脚本生成
Windows ICO、macOS ICNS 及多尺寸 PNG，并接入应用与安装包。
检查范围与精确测试结果见 [TEST_LAYERS.md](TEST_LAYERS.md)。

### macOS 严格核间矩阵调研

结论：当前证据不足以支持把 macOS 普通用户态线程往返测量标为指定 CPU-ID 对的严格
矩阵。因此 0.1.1 仍明确显示不支持；没有新增要求降低系统安全性的代码。
以下为源码／接口证据与工程判断，不是 Mac 私有接口的真机验证。

| 路线 | CPU-ID 绑定与执行核心验证 | 兼容性和分发判断 |
|---|---|---|
| 公开 Mach affinity / QoS | affinity tag 表达共享缓存偏好，不是 CPU 编号；QoS 表达调度需求，不保证 P 核或某个核 | 可用于调度条件记录，不能承诺指定核对。Apple Silicon 的公开 XNU ARM affinity 集合数为 0，相关策略可返回不支持 |
| 私有线程策略与核心编号读取 | 所检查私有策略主要是 QoS／实时约束；`_os_cpu_number()` 能提供瞬时编号，但源码明确不是稳定 ABI。能读到编号不等于能绑定，也不能仅靠采样两端编号相同排除中途迁移 | 可作为后续诊断实验，需按 OS／架构验证；未找到已验证可随普通桌面包分发的严格绑定路径 |
| 开发／调试内核及内核内部接口 | XNU 的 `thread_bind` 能指定处理器；开发／调试条件编译的 `sysctl_thread_bind_cpuid` 路径还会触发调度切换。这不证明普通发布内核暴露该能力 | 需要专用环境、版本耦合与失败清理；第三方内核扩展还涉及授权、重启和 Apple Silicon 的安全策略成本，不纳入普通测试包 |

证据固定到 XNU `f6217f891ac0bb64f3d375211650a4c1ff8ca1ea`：
[公开策略定义](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/osfmk/mach/thread_policy.h)、
[ARM affinity](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/osfmk/arm/cpu_affinity.h)、
[策略拒绝路径](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/osfmk/kern/thread_policy.c)、
[私有线程策略](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/osfmk/mach/thread_policy_private.h)、
[内部核心编号读取](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/libsyscall/os/tsd.h)、
[内核绑定与调试路径](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/osfmk/kern/sched_prim.c)。
平台语义另见 [Apple affinity 说明](https://developer.apple.com/library/archive/releasenotes/Performance/RN-AffinityAPI/index.html)、
[Apple Silicon 调度说明](https://developer.apple.com/videos/play/tech-talks/110147/) 和
[Apple 扩展分发要求](https://support.apple.com/guide/deployment/system-extensions-in-macos-depa5fb8376f/web)。

如以后开展严格矩阵实验，验收必须同时具备目标 CPU-ID 的绑定成功证据、独立的执行
核心／迁移验证、完整核对覆盖、可取消超时与解绑清理，并记录 OS build 和机型。
仅有两个线程、不同 affinity tag、较低方差或首尾相同核心编号均不足以替代这些条件。

## V0.1.0 desktop distributions

桌面端采用 **Qt Widgets / PySide6 + 独立 C 引擎**，不是 Electron，不包含 Chromium、
Qt WebEngine、QML UI、Matplotlib 或 NumPy。主页可一键运行全部可用模块，也可勾选运行；
七个模块标签页分别支持单独测试、查看详细表格、状态和结果来源。ComputeMark 标签显示
严格校验后的单/多线程分数及波动提示；其他诊断模块不提供未经验证的综合评分。
各模块显示运行指示，主页进度按已完成模块计数，并非伪造的剩余时间百分比。
默认使用石墨深色主题与低饱和冰蓝强调色；主页分区展示模块、配置与运行日志，
结果页保持同一深色配色并支持选择复制。窄窗口下主页可滚动，无持续主题动画或额外后台进程。

| 平台 | 架构 | 发布格式 | 最低桌面运行环境 |
|---|---|---|---|
| macOS | x64、ARM64 | DMG（分别包含原生 `.app`） | macOS 13+；Apple Silicon 使用 ARM64 包，不使用 Rosetta 评分 |
| Windows | x64、ARM64 | 每用户安装 EXE + ZIP 便携包 | x64 Windows 10 1809+；ARM64 Windows 11 |
| Linux | x64 | DEB、RPM、便携 tar.gz | glibc 2.35+；如 Ubuntu 22.04+、Debian 12+、现代 Fedora/openSUSE |
| Linux | ARM64 | DEB、RPM、便携 tar.gz | glibc 2.39+；如 Ubuntu 24.04+、Debian 13+、现代 Fedora/openSUSE |
| 源码 / CLI | 所有支持的后端 | Source.tar.gz、Source.zip | Python 3.10+、C 编译器；不要求安装 Qt |

这张表是构建目标与兼容性下限，不等于对所列每个发行版做过完整真机测试。
Alpine/musl、旧版 glibc Linux 不支持上述预编译 GUI 包，可自行构建源码 CLI。
DEB 用 `sudo apt install ./MemoryTest_0.1.0_amd64.deb`（ARM 为 `arm64`）；
RPM 用发行版包管理器安装对应架构包。便携 Linux 解压后运行 `MemoryTest/MemoryTest`。
Windows ZIP 解压后运行 `MemoryTest/MemoryTest.exe`，不需要 Python/Qt，也不需要管理员权限。
Mac 将 DMG 中的应用拖到 Applications。V0.1.0 默认无开发者签名/公证；系统可能警告或阻止首次打开，
发布前须核对校验和及来源，不建议关闭系统全局安全功能。

界面是离线原生控件，完整 HTML 报告仍可用系统浏览器打开。日志最多保留 500 行，
单个结果页内存显示限制 5M 字符（完整文件不裁剪）。报告保存在 Qt 用户数据目录内，
不会写进 `.app` 或安装目录；便携版默认同样使用用户数据目录，可在主页另选位置。
同时只运行一个任务，取消/关窗先停止测试再保存说明报告；后台 worker 检测 GUI 父进程消失。
没有遥测、账号、自动更新或后台守护服务。测试本身可能占用大量 CPU/内存；
主页线程参数仅限制 ComputeMark，不代表其他诊断模块的线程或内存上限。

### 源码命令行与桌面构建

```bash
# CLI：不安装 Qt / PyInstaller
python3 run_benchmarks.py --list
make test MODULE=cpu_multi

# Windows 原生 CLI：先安装目标架构 MinGW/Clang，并把编译器放入 PATH
python run_benchmarks.py --module cpu_multi

# 各目标系统分别构建，不在 Linux 上声称完成 Mac/Windows 安装包
python3 -m venv .venv-desktop
# 激活环境后：
python -m pip install -r packaging/requirements-desktop.txt
python packaging/build_desktop.py
python packaging/check_artifact.py
python packaging/build_source.py             # 从当前已提交的 Git SHA 归档
```

Linux 另外需要 `gcc`、`dpkg-deb`、`rpmbuild` 及 Qt 的 X11/OpenGL 系统依赖；
macOS 需要 Xcode Command Line Tools / `hdiutil`；Windows 需要原生 MinGW/Clang 和 NSIS。
`CC` 可指定编译器；Windows ARM64 使用 MSYS2 CLANGARM64 / clang，x64 使用 UCRT64 / gcc。
完整依赖安装和原生验收见 `.github/workflows/desktop-packages.yml`。
预编译程序亦可通过 `MemoryTest --cli --skip-build --module cpu_multi --output-dir PATH` 调用命令行。

包采用 onedir 结构，Qt 动态库可替换；依赖许可与对应版本源码地址随包保留在 `licenses/`，
界面“许可”按钮可打开。构建检查限制单包不超过 100 MiB、展开应用不超过 250 MiB；
实际包大小、SHA256 和离屏空闲 RSS/CPU 实测写入随构建产物提供的 JSON。
离屏测试不能代表真实桌面驱动、显示器配置、功耗或所有硬件的内存占用。

验证范围：独立整数/浮点参考、有限双线程原生 ComputeMark、来源/失败/取消隔离、标签页选择与
详细结果、GUI 冒烟及打包运行时检查。发布包的验证不等于所有旧诊断算法已可用于跨 CPU 排行。
签名、公证、热状态/低电量实验、各发行版实际桌面安装及长期硬件稳定性仍须分别验证。

## 项目结构

```
memorytest/
├── README.md
├── Makefile
├── tests.json                  # 测试注册表
├── run_benchmarks.py           # 模块执行入口与独立归档
├── benchmark_registry.py       # 模块选择共享逻辑
├── tests/                      # 流程验收夹具
├── test_smoke.sh               # 可选旧版开发检查
├── test_sanity.py              # 可选旧版数值范围检查
├── test_regression.py          # 可选旧版历史回归检查
├── generate_report.py          # 旧入口与可选图表工具
├── report_html.py              # HTML 报告生成器（仅标准库）
├── report_score.py             # 旧指标与 v2 集成
├── compute_score.py            # v2 协议校验与绝对分数
├── src/                        # C 源码
├── bin/                        # 构建产物（gitignore）
└── reports/                    # 报告与 HTML（gitignore）
```

## 注意事项

- **模块入口以非交互方式执行。** 如需特权硬件信息请预先配置凭据，不会在运行时索取密码。**`dmidecode` 需要 sudo。** 在真实终端（TTY）中，基准测试会提示一次密码并缓存 10 分钟供所有二进制使用。CI 中可设置 `MEMORYTEST_SUDO_PASSWORD`，或留空（测试仍可运行，通道/速度将被提示或显示 N/A）。
- **PMU 计数器需要 `perf_event_paranoid ≤ 2`。** 否则自动退回到基于时间的估算。
- **虚拟化环境**（云主机、容器）可能报告缓存值或噪声延迟——基准测试假定裸金属或近裸金属的 CPU 缓存访问。
- **核间测试**在大机器（96+ 核）上可能需要 5+ 分钟。可用 `make test MODULE=inter_core ARGS="--timeout 3600"` 指定时限。

## 参考文档

- [ASM_OPTIMIZATIONS.md](ASM_OPTIMIZATIONS.md) — 内联汇编原语（rdtsc、clflush、屏障）
- [TEST_LAYERS.md](TEST_LAYERS.md) — 测试架构详解

## 许可证

MIT
