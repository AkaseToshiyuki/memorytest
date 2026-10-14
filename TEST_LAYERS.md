# Functional Test Modules / 功能子模块

用户按功能选择测试；默认全选，运行后自动生成对应范围的 HTML。
Smoke、sanity、regression 是可选开发检查，不是用户跑分层级。

## 模块 / Modules

| Module | 内容 | Group |
|---|---|---|
| `cache_hierarchy` | 缓存带宽、依赖链延迟、层级跃迁检测 | memory |
| `memory_bandwidth` | 读写／复制／Scale／Add／Triad／步长带宽与依赖链延迟 | memory |
| `inter_core` | 核间延迟矩阵与 CAS 吞吐量 | memory |
| `cpu_alu` | CPU 整数运算 | cpu |
| `cpu_float` | CPU 浮点与 SIMD 运算 | cpu |
| `cpu_branch` | 分支预测 | cpu |
| `cpu_multi` | 多核吞吐与扩展性 | cpu |

`tests.json` 是统一注册表，供模块选择和报告文件名解析使用。

## 运行 / Run

```bash
make test
make test MODULE=cpu_alu
make test MODULES="cpu_alu cpu_float"
make test CATEGORY=memory
make list-tests
python3 run_benchmarks.py --module cpu_alu --module cpu_multi
python3 run_benchmarks.py --module inter_core --timeout 3600
```

模块与分类选择取并集、去重，只编译和运行选中的模块。
非法模块名在运行前报错。已有二进制的发布包可用 `--skip-build` 跳过编译。

模块入口非交互，不会主动修改系统配置或清空系统缓存。C 硬件检测代码仍可能使用
预配置的凭据。标准输出与错误保存在各模块的日志中。`--timeout` 指定每个模块
的运行时限；未指定时，除核间外使用 `CPU_TIMEOUT` 或按核心数计算的默认值
（300–2400 秒），核间使用 `min(2400, 120 + N*N/3)` 秒。

## 结果 / Reports

- 全量：`reports/benchmark_report.html`。
- 单模块：`reports/<module>_report.html`，仅显示该模块。
- 多个指定模块：`reports/selected_report.html`。
- 每次运行独立归档：`reports/runs/<run-id>/`。

归档包含 `manifest.json`、自包含 HTML、`reports/` 下的有效原始报告，
以及各模块的工作目录和日志。失败模块的半成品留在工作目录，不进入有效数据或评分。
每次使用新目录，不读取顶层旧版 Markdown。顶层 HTML 是最新副本，历史归档独立保留。

退出码 0 表示至少一个模块成功且没有执行失败；成功模块的报告包含标题和非空结果表。
平台不支持的模块返回 77，在 manifest/HTML 中标记 `unsupported`，不计为通过、
不产生测量值，但不阻止其他模块执行。只选中不支持的模块时仍生成 HTML，整个命令返回 77。
`all_selected_passed` 用来区分“全部测完”和“包含不支持模块但其余成功”。
这**不代表测量科学性已经得到验证**。构建失败、非零退出、超时、报告缺失或无效
均在 HTML 中标记，并令命令返回非零退出码；其余选中的模块仍继续执行。
Ctrl-C 会停止当前进程组、标记剩余模块跳过，并尝试生成部分 HTML，返回 130。

macOS 精确 CPU-ID 绑核/核间矩阵、Linux PMU 不可用；ComputeMark 使用有记录的 QoS 策略。
`python3 run_benchmarks.py --system-info` 只查询硬件/大小核信息，不启动任何基准测试。

HTML 表格与内联 SVG 仅需 Python 标准库，包括核间热图和多线程扩展曲线。
桌面和 HTML 共用 `result_model.py`，不显示旧的 `/100` 诊断评分。
本次运行包含 `cpu_multi` 的合格 v2 原始采样时，显示实测单线程/多线程绝对分数；
不接受旧 v1 推算输入。报告仅显示数据、测试条件和状态；算法限制见 README。

```bash
make report                              # 重生成最近一次归档 HTML
make report RUN_DIR=reports/runs/<run-id>  # 指定归档
python3 report_html.py --run-dir reports/runs/<run-id> --module cpu_alu
```

## 开发检查 / Developer checks

`make check-workflow` 使用确定性子进程夹具，不运行硬件负载。
覆盖默认全选、每个单模块、分类去重、HTML 范围、历史数据隔离、
构建/进程失败、无效报告、超时、中断与报告重生成。

`make check-compute` 验证独立数值参考、评分公式/协议、时钟与故障处理，
并执行最多 2 个允许 CPU 上的短时原生评分内核验收；不运行内存或核间重负载。

`make smoke`、`make sanity`、`make regression` 保留为独立的旧版开发命令，
仍使用顶层 `reports/*.md`，尚未接入新归档，不是生成用户报告的前置条件。
它们在审计中发现的校验缺陷仍待后续修复，不能作为测量正确性的认证。

`generate_report.py` 保留兼容旧调用及图表辅助功能；
新运行应使用 `run_benchmarks.py` 或 `make test`。

## 0.1.1 regression and stress checks

`python -m unittest discover -s tests -v` covers the new cache, presentation and
stress checks as well as existing contracts. It does not run the legacy heavy
top-level smoke/sanity/regression commands. Native CPU checks use at most two
threads; the stress check uses one thread, 1 MiB, and a 1.2-second limit.

- `test_cache_report.py`: bounded 128 KiB end-to-end cache scan; no L3, sparse samples, unknown cache
  metadata, allocation failure, unknown available memory, creation/write failures.
- `test_presentation.py`: hand-calculated values/units/SVG coordinates, stale-result
  exclusion, all four language/theme combinations, state preservation, contrast,
  independent plot/statistics windows. Optional Node check exercises offline JS
  switches with localStorage denied; Node is not a packaged dependency.
- `test_stress.py`: independent Python integer and Decimal closed-form references,
  bounded native CPU/FPU/cache/RAM switching, sensor disappearance/permissions,
  shared budgets, simulated GPU switching, thermal/fault/parent-exit cleanup.
- `test_monitoring.py`: independent E/P/native-cluster output fixtures and unit
  conversions, SMC generation-specific region names, hardware-vs-OS frequency
  provenance, hardware overview missing fields, stale sensor expiry, fixed-command
  authorization quoting, IPC rejection, pause and parent-exit cleanup. The Linux
  bridge test reads existing sensors as the current user; it does not elevate.
- Presentation regression includes fixed checkbox/equal initial column widths,
  user resizing, larger spin targets, 1–10-minute plot bounds, all temperature
  series, permission refusal/failure, and compact home/stress screenshots in all
  four language/theme combinations (`build/ui-revision/`).
- Native desktop CI builds six targets and checks extracted installer startup,
  heavyweight-dependency exclusion, idle resources after one-shot discovery and
  a monitoring-only session. macOS additionally verifies bundle signatures and
  a copied application's Cocoa/LaunchServices startup; this does not reproduce
  browser quarantine or third-party archive-tool metadata.
  It does not certify real GPU stress, SMC access on every Mac, or Windows LHM.

Revision verification on 2026-10-14 before installer CI: 107 discovered, 101
passed, 6 platform-dependent checks skipped on Linux x64 (55.044 seconds).
Subsequent focused checks cover the final small UI/encoding changes and two
additional hardware-overview fixtures: monitoring 15/15 (1.701 seconds),
presentation 14/14 (9.427 seconds); strict C11 helper compilation also passed.
During the preceding implementation, an additional
1.5-second OpenCL-only session on a Tesla P4 completed with validated dispatch
heartbeats and a duration stop. This is not long-duration stability coverage.
Metal, physical Mac sensor access and Windows GPU/sensor coverage remain
device-dependent; a successful build is not that coverage.

### diagnostic-v2 follow-up checks (2026-10-14)

- `test_diagnostic_kernels.py`: independent Decimal/float32 references for every
  floating result lane, uint64 ALU references, exact operation counts and timing
  divisors; conditional-branch outcomes and x86 disassembly. Reports use a shortened
  1-ms target / 3 samples in tests, not the production 150-ms / 5-sample default.
- `test_memory_kernels.py`: independently calculated values for all 65 elements,
  useful-byte counts including stride tails, a complete 65-node cycle, common-time
  accounting with two workers and 384 KiB allocation, partial creation/allocation failure.
- `test_cache_report.py`: three output sections, synthetic sustained-jump versus
  single-spike/noisy-overlap fixtures, plus the existing bounded failure cases.
- `test_intercore_protocol.py`: Linux only, two workers, 80 small redispatches and
  exact completed CAS/sample counts; no whole-machine matrix or placement certification.
- GPU mock checks verify nonblocking read ordering, bounded slot recycling,
  corruption rejection and drain-before-release. FPU references check all 16/32 lanes.
- Native/HTML cache-part separation and tab selection survive language/theme
  changes. Twelve new fixture screenshots are in `build/ui-revision/cache-*.png`.

Local full suite: **125 discovered, 119 passed, 6 platform-dependent skips,
107.035 seconds**, using `.venv-desktop/bin/python`; log
`/tmp/memorytest-algorithms-all-tests.log`. The system Conda Qt installation lacks
an ICNS reader; its icon test failure is not used as the desktop acceptance result.

Production-default single-thread run of ALU, float and branch: all three passed
(12.121 s, 21.375 s, 8.169 s respectively); raw samples/reports are under
`build/validation-algorithms/runs/20261014-200738-u0ob7g18/`. Both sqrt rows are nonzero;
all branch rows retain measured work. Sample spread is reported, not guaranteed zero.
No default-size memory/cache scan was launched during this verification.

OpenCL-only Tesla P4 follow-up: 1.5-second configured limit, 2.636-second total
including cleanup, two validated-completion heartbeats, normal duration stop.
Evidence: `build/validation-algorithms-gpu/stress-runs/20261014-200015-nasa2bmh/`.
This does not validate M4 Pro Metal utilization or prolonged thermal behavior.
Native six-target installer CI must still pass for this revision before handoff.
