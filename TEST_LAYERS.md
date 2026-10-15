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

### Archive, monitoring continuity and diagnostic-v2.1 checks (2026-10-15)

- `test_storage.py`: automatic output/fallback, ten combined benchmark/stress rounds,
  active-writer protection, OS cleanup locks, legacy/unknown/link preservation,
  bounded history restoration, corrupt-result fallback, inline licenses, short title,
  equal-width cache controls, centered empty views and the Tools/stress hierarchy.
- `test_monitor_continuity.py`: unchanged sensor process and authorized helper across
  stress start, preserved plot/selection, shared fresh snapshots, thermal/stale-stream
  stop, All tri-state behavior and per-launch authorization refusal. Uses fixtures;
  no elevation or actual stress is launched by these tests.
- `test_sampling_protocol.py`: an independent synthetic clock/kernel checks that
  cold warmup and descheduled calibration do not shorten the final equal-work batch;
  stalled/backward clocks and kernel errors fail within a bounded number of calls.
- Memory regression checks untimed full-array validation and injected corruption;
  intercore checks sparse allowed CPU IDs, placement failure, partial thread-creation
  cleanup and the existing two-worker bounded protocol. No whole-machine matrix.
- SMC fixtures check known M1–M4 distinctions, M4 base vs Pro/Max GPU keys, typed
  power/voltage/fan labels and unknown-model fallback. These do not establish full
  Apple Silicon or Intel Mac coverage or physical SMC availability.

Local full regression before the final small warning/documentation changes:
**152 discovered, 146 passed, 6 platform-dependent skips, 185.723 seconds**,
using `.venv-desktop/bin/python`; log `/tmp/memorytest-oct15-full-tests.log`.
All seven native benchmark binaries built successfully (`/tmp/memorytest-oct15-build.log`).
Final focused checks and six-target native installer results are recorded below
when completed. ComputeMark scoring/kernel/calibration remain unchanged.

Final focused local checks: storage **16/16 (6.153 s)**, monitoring transitions
**8/8 (1.508 s)**; logs `/tmp/memorytest-oct15-final-storage.log` and
`/tmp/memorytest-oct15-final-continuity.log`. The extra storage case verifies
that retention errors stay visible after a successful worker exit. Rebuild and
`git diff --check` passed.

A real Qt/QProcess shared-stream check also passed with **no load modes selected**,
one idle worker and a 1 MiB budget: the preview PID stayed unchanged, samples grew
from 2 to 4, and the supervisor stopped with `user_stop`. The initial harness
expected the UI key `stopped` instead of the supervisor's documented `user_stop`;
the corrected assertion passed without an implementation change. Evidence:
`/tmp/memorytest-oct15-shared-runtime-final.log` and
`build/validation-oct15-shared/stress-runs/20261015-095933-788qfn2x/`.
This was unprivileged Linux monitoring, not Mac authorization or SMC verification.

First native CI pass exposed two portability issues: the new archive writer
identity had introduced an unconditional `psutil` dependency into the standalone
CLI, and four new Mac test expectations compared `/var` with its canonical
`/private/var` path. The CLI now uses an optional process-identity dependency and
conservatively retains uncertain writers; test roots are canonicalized. The
standard-library-only workflow (`python -S`, 17/17, 4.265 s) and storage with the
new missing-dependency case (17/17, 6.135 s) passed locally. Logs:
`/tmp/memorytest-oct15-stdlib-workflow.log`, `/tmp/memorytest-oct15-stdlib-storage.log`.

### Repeat sampling and independently selectable tasks (diagnostic-v3)

- Defaults: cache **5**, memory/branch/intercore/ComputeMark **20**, ALU/FP **50**.
  `test_sampling_protocol.py` checks compiled module defaults. Native cache, memory,
  ALU/FP/branch tests use these real counts with 1 ms targets and small allocations;
  ComputeMark retains its real 100 ms lower bound with at most two CPUs.
- `test_sample_statistics.py` uses exact rational order-statistic references plus
  hand-worked quartiles, both-tail outliers at 20/50/100/200 samples, drift, excessive
  contamination, zero IQR, small N and invalid inputs. C/Python agreement alone is
  not the oracle. ComputeMark rates use this separate oracle and raw operation/time
  records; forged precomputed statistics cannot change points.
- Native audit files must retain ordered samples and exclusion metadata. Cache and
  two-CPU intercore tests inject `/dev/full` to verify evidence-write errors fail.
  The two-CPU matrix fixture is bounded (3 samples, 64 round trips, fixed throughput
  count), not a whole-machine matrix; an 80-dispatch completion check remains.
- ALU checks masked-integer references and x86 disassembly for actual multiply/divide
  without stack-backed dependency chains. FP retains high-precision/scalar lane and
  exact operation-count references. These are correctness/retention checks, not a
  measured speedup claim or certification of every compiler's generated assembly.
- Split-mode tests check actual native reports/audit record sets; memory latency
  additionally asserts one array allocation and zero bandwidth-worker creations.
  Workflow tests check explicit engine mode arguments, isolated report names and
  copied raw sidecars. Legacy combined IDs and archives remain compatible.
- Presentation checks exercise cache three-way and memory two-way selection,
  standalone run buttons, old archive mapping, decimal units capped at GB/s, equal
  cache controls, compact layouts, both languages/themes and unchanged raw data.

Local full regression: **169 discovered, 163 passed, 6 platform-dependent skips,
255.225 s**; `/tmp/memorytest-repeat-split-full-final.log`. Final report/selection
checks: **25/25, 5.105 s**, `/tmp/memorytest-repeat-split-final-views.log`.
Final legacy-duration presentation/restore checks: **24/24, 6.176 s**,
`/tmp/memorytest-repeat-split-legacy-duration.log`; combined-run duration is not
invented for individual restored tasks, and the combined origin is visible.
The extra two-CPU raw-to-matrix reference check passed **1/1, 3.928 s** in
`/tmp/memorytest-repeat-intercore-reference.log`; standalone dependency-free CLI
checks passed **18/18, 5.040 s**, `/tmp/memorytest-repeat-split-stdlib.log`.
All seven native engines built; `/tmp/memorytest-repeat-split-build-final.log`.
`git diff --check` passed. UI fixture previews: `build/ui-revision/`.

The first full pass found two reconstruction failures: even sample medians may
contain half a nanosecond, but old Markdown time formatting rounded to whole
nanoseconds. Diagnostic time fields now retain nine decimal places in milliseconds;
raw JSON was already lossless for these durations. Assertions were not loosened.
The scripted-clock case also explicitly checks a **101.5 ns** median.

Six-target native installer / four-target portability CI evidence is recorded
after completion; local Linux checks alone do not establish Mac/Windows runtime behavior.
No long default working sets, whole-machine matrix, new privileged access or
unbounded stress load is part of this acceptance run.

### Live monitoring, status pulse and hardware inspector tools (2026-10-15)

Current startup-permission policy (supersedes the manual authorization UI below):
**114 passed, 0 skipped** in the changed/relevant local suites.

| Suite | Passed | Time | Evidence |
|---|---:|---:|---|
| Startup tokens / inventory / bounded native bridge | 26 | 0.465 s | `build/hardware-startup-access.log` |
| Hardware pages / automatic mode / no permission controls | 23 | 38.422 s | `build/hardware-startup-tools.log` |
| Existing telemetry / direct startup-token helper | 18 | 1.766 s | `build/hardware-startup-monitoring.log` |
| Live monitoring continuity | 12 | 8.428 s | `build/hardware-startup-continuity.log` |
| Desktop lifecycle / status transitions | 16 | 24.674 s | `build/hardware-startup-desktop.log` |
| Bilingual themes / layout / no permission prompts | 19 | 48.980 s | `build/hardware-startup-presentation.log` |

- No in-app elevation launcher remains: no UAC `runas`, AppleScript administrator
  request, polkit command, permission dialog, admin-read button, enhanced-mode
  checkbox or authorization retry. Standard launch never starts a privileged
  reader; elevated launch automatically uses its inherited token. Refresh,
  page changes, stored preferences and failure states do not expose a mode switch.
- Windows elevation uses `GetTokenInformation(TokenElevation)` rather than
  membership in an administrator group; POSIX uses the effective UID. Detection
  failures fall back to standard access. Unit fixtures cover both token states,
  token-query failure/handle cleanup and administrator-only reader admission.
- Both parameter-page modes keep stale data cleared, bounded timeout/cancel and
  original SMART bitmasks. Direct helpers receive a grace period to clean up query
  children on disconnect before forced termination.
- All four bilingual parameter previews and stress-page themes were regenerated
  and inspected; previews contain fixture data. Python compilation, strict Linux
  C11 syntax and Git whitespace checks passed. Actual M4 Pro administrator startup
  and protected device fields remain outside this Linux host's verification.
- Prior handoff/builds using the manual authorization design are not final package
  evidence. Record the current commit's native CI and artifact checks separately.

Historical handoff checks (before the startup-only permission requirement):

Administrator inspection and Apple Silicon follow-up (2026-10-15): local
inspector/UI suite **20/20**, native bridge/platform fixtures **11/11**, existing
monitoring contracts **17/17**, desktop suite **16/16**. Evidence is retained in
`build/hardware-{tools-repair,access-repair,monitor-regression,desktop-regression}.log`.
The native IPC tests run as the ordinary test user, reject arbitrary commands,
paths and write options, and verify disconnect cleanup. They do not establish
successful OS elevation. Linux standard DRAM inspection reproduced a
`dmidecode` permission failure and now reports it explicitly. This host lacks
passwordless sudo; actual privileged device access and the supplied M4 Pro
remain unverified. New macOS fixtures cover alternate profiler keys, native
Metal identity, shared-memory semantics, ARM instruction features and separate
performance-level cache domains. The six-platform installer matrix includes the
new bridge suite and builds the Metal inventory helper on both Mac targets.

Handoff follow-up: the inspector/UI suite **21/21**, bridge/platform/payload
suite **22/22**, and existing monitoring contracts **17/17** passed locally.
Evidence: `build/hardware-tools-takeover.log` (29.637 s),
`build/hardware-access-takeover.log` (0.467 s), and
`build/hardware-monitor-takeover.log` (1.753 s). Additional cases cover whole-reply
deadlines, malformed/partial RPC responses, authorization timeout versus denial,
cancel during launch, no post-cancel fallback reads, and launcher reaping.
Negative bridge errors are separate from all 256 child exit codes, so valid
SMART health bitmasks 124/125/127 remain device data rather than transport errors.
Native payload checks reject missing helpers and exercise the packaged protocol
without elevation; the same checks now run against every installer payload.
Apple cache domains are checked against Apple's documented performance-level
semantics, including three-level fixtures and separate cache-sharing counts.
All four parameter-page previews were regenerated in both languages under
`build/hardware-repair-reviewed/`; they are explicitly fixture data, not M4 Pro
measurements. Strict Linux C11 warnings-as-errors syntax, Python compilation and
Git whitespace checks passed. Fresh six-platform CI and artifact verification
must correspond to the follow-up commit, not the older `69b0e05` package.
The final fixture-only preview refinement passed **1/1** (1.384 s), recorded in
`build/hardware-preview-reviewed.log`; this is a subset rerun, not an extra unique test.

Previous UI/status acceptance: **99 passed, 0 skipped** across the relevant suites:

| Suite | Passed | Time | Evidence |
|---|---:|---:|---|
| Hardware parser / native UI tools | 18 | 15.123 s | `/tmp/memorytest-hardware-tools-clean-ui.log` |
| Desktop / status transitions | 16 | 24.346 s | `/tmp/memorytest-ui-pulse-desktop-final.log` |
| Archive restoration / storage | 17 | 7.724 s | `/tmp/memorytest-ui-pulse-storage.log` |
| Monitor continuity | 12 | 8.294 s | `/tmp/memorytest-ui-pulse-monitor-final.log` |
| Bilingual / theme / layout presentation | 19 | 53.145 s | `/tmp/memorytest-ui-pulse-presentation.log` |
| Existing telemetry contracts | 17 | 1.690 s | `/tmp/memorytest-ui-monitoring-final.log` |

- Async fixture sensor workers update the idle chart after archive restoration,
  without starting a load. Tests cover visible post-load monitoring, rapid
  hide/show restart, shutdown, and preserved active-load shared monitoring.
- Running status alternates green/muted at a configured 600 ms phase; tests check
  both rendered colors, completion/failure reset, requeue after old success,
  unsupported yellow and both themes. No pulse timer runs when nothing is running.
- Every benchmark page has full-width equal action buttons at compact and wide
  sizes in both languages. Hardware pages cover four language/theme combinations,
  device selection, no horizontal clipping, and absence of UI methodology prose.
- Independent fixtures cover OS/firmware versus SPD, Mac unified memory, Windows
  CIM capacity bounds, identical-name GPU identity, ATA hexadecimal attribute IDs,
  NVMe warnings and endurance >100%, and smartctl health-bit exit codes.
  Unknown/unreadable SMART is never a passing health result.
- Inspector lifecycle checks cover lazy startup, a real CPU-inventory worker,
  stale-data removal, failed startup, output cap, timeout configuration, serial
  masking on screen/clipboard/export and explicit serial reveal.
- `py_compile` and `git diff --check` passed. Preview PNGs in `build/ui-revision/`
  use labelled test fixtures, not real measured hardware. Actual unprivileged Linux
  inspection found CPU and four GPU entries; module DMI and SMART reads were
  permission-limited. No new elevation, load, disk self-test or SPD write was run.

Native installer CI now runs the inspector suite and all four packaged read-only
inventory commands. `inspector-check.json` records available field names and
backend status, without hardware serial numbers. Final CI/artifact verification
is recorded separately after the build; fixture coverage does not certify every
device's SPD/SMART access or equivalence to the four reference applications.

Final CIM-unit refinement: inspector suite **18/18, 14.943 s**,
`/tmp/memorytest-hardware-tools-units-final.log`. The ambiguous legacy CIM `Speed`
field is not relabelled as MHz; zero configured clock/voltage means unavailable.
Screen/clipboard/export copy checks passed **2/2**,
`/tmp/memorytest-hardware-tools-export-final.log`; exported snapshots also omit
non-operational commentary metadata.
