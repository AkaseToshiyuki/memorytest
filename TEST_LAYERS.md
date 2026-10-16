# 开发与测试

## 模块

`tests.json` 注册引擎，`benchmark_registry.py` 注册独立任务和界面分组。

| 引擎 ID | 内容 | 独立任务 ID |
|---|---|---|
| `cache_hierarchy` | 缓存带宽、延迟、层级检测 | `cache_bandwidth_test`、`cache_latency_test`、`cache_detection` |
| `memory_bandwidth` | 内存带宽、延迟 | `memory_bandwidth_test`、`memory_latency_test` |
| `inter_core` | 核间通信 | 同引擎 ID |
| `cpu_alu` | 整数运算 | 同引擎 ID |
| `cpu_float` | 浮点运算 | 同引擎 ID |
| `cpu_branch` | 分支模式 | 同引擎 ID |
| `cpu_multi` | ComputeMark | 同引擎 ID |
| `cpu_architecture` | 核心架构分析 | 同引擎 ID |

模块和分类选择取并集。仅构建选中的引擎；缓存延迟与层级检测合选时共享当前扫描。

## 运行与报告契约

- 每轮使用独立目录，失败模块的半成品不进入有效报告。
- 原始 JSON、样本顺序、统计信息与运行条件随报告保存。
- 退出码：成功 `0`，执行失败 `1`，仅含不支持模块 `77`，用户中断 `130`。
- 混合选择中的不支持项单独标记，其余模块继续执行。
- 报告重生成读取所选归档，不重新运行负载。

## 常用检查

```bash
make check-workflow
make check-compute
make check-platform
python -m unittest discover -s tests -p test_architecture.py -v
python -m unittest discover -s tests -p test_presentation.py -v
```

GUI 检查需安装 `packaging/requirements-desktop.txt`；无显示环境设置 `QT_QPA_PLATFORM=offscreen`。

| 范围 | 主要检查 | 运行条件 |
|---|---|---|
| 工作流 | 选择、构建失败、超时、取消、归档隔离、HTML 范围 | 确定性子进程夹具 |
| ComputeMark | 独立整数／浮点参考、计数、公式、线程梯度、故障清理 | 原生检查最多两个 CPU |
| 采样 | 校准、等工作量、Type-7 分位数、异常值、漂移、写入失败 | 合成时钟与有界原生负载 |
| 缓存／内存 | 完整环、全部输出元素、字节计数、共同时间窗、分配失败 | 小工作集与短采样 |
| 架构 | 指令保留、机器码布局、指针环、边界、算术参考、推断门槛、原始报告 | 静态内核与缩短目标时长 |
| 核间通信 | 绑核／迁移、任务代际、稀疏 CPU ID、超时与清理 | 两线程或平台夹具 |
| 硬件与监控 | 数据来源、单位、缺失值、SMART 位码、序列号隐藏、断连清理 | 夹具及只读本机接口 |
| 桌面 | 任务生命周期、状态、取消、报告恢复、语言与主题切换 | Qt 离屏 |
| 样式 | 图表坐标、对比度、数值标签、窄窗口、图标容器 | Qt／SVG 渲染 |
| 压力 | 独立计算参考、负载切换、预算、错误与退出处理 | 有限 CPU 负载，GPU 夹具 |

按改动选择检查。完整 `unittest discover -s tests` 包含有界原生压力负载。
`make smoke`、`make sanity`、`make regression` 是读取旧顶层报告的兼容工具，不属于默认报告流程。

## CI 与安装包

| 工作流 | 目标 | 检查 |
|---|---|---|
| [Portability](.github/workflows/portability.yml) | Linux GCC／Clang、macOS ARM64／x64 | 引擎构建、数值参考、流程与平台接口 |
| [Desktop packages](.github/workflows/desktop-packages.yml) | Linux、macOS、Windows 各 x64／ARM64 | 原生契约、GUI、打包、解包启动、只读助手、许可与源码 |

安装包工作流由 `main` 推送或手动触发，不自动发布 Release。
运行结果、日志和产物保存在 GitHub Actions；以提交 SHA 匹配验证结果。
CI 不执行全机核间矩阵、长期压力或管理员设备验收。

## 正确性参考

整数使用独立模算术，浮点使用有理数／高精度参考，统计使用手工向量与独立分位数计算。
架构检查覆盖随机链／计算链状态、所有资源变体的访存边界、混合指令计数、160 个 BTB 布局及 38 个吞吐流。
整组重测检查原始历史、配对工作量与跨组漂移；细扫检查相邻资源窗口的分离。
原生报告读取前保留原始证据，解析失败也可检查产物。

测试范围为代码正确性、功能行为和前端样式，不新增文案回归测试。
