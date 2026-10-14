# Functional Test Modules / 功能子模块

用户按功能选择测试；默认全选，运行后自动生成对应范围的 HTML。
Smoke、sanity、regression 是可选开发检查，不是用户跑分层级。

## 模块 / Modules

| Module | 内容 | Group |
|---|---|---|
| `cache_hierarchy` | L1/L2/L3/内存延迟与带宽 | memory |
| `memory_bandwidth` | 内存读取、写入、复制吞吐量 | memory |
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

HTML 表格与内联 SVG 仅需 Python 标准库；Matplotlib 只用于可选 PNG 图表。
没有 Matplotlib 时仍展示核间原始矩阵。默认不显示旧的 `/100` 诊断评分。
本次运行包含 `cpu_multi` 的合格 v2 原始采样时，显示实测单线程/多线程绝对分数；
不接受旧 v1 推算输入。报告同时展示各模块可比性限制。详见 README 的评分与算法审计。

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
