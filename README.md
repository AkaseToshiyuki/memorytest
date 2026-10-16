# MemoryTest

跨平台 CPU、内存与缓存测试工具，提供原生桌面界面和命令行。
支持中文／英文、深色／浅色主题，以及离线 HTML 报告。

[下载安装包](https://github.com/AkaseToshiyuki/memorytest/releases) ·
[构建状态](https://github.com/AkaseToshiyuki/memorytest/actions) ·
[测量方法](MEASUREMENT.md) · [开发与测试](TEST_LAYERS.md)

当前源码版本：**0.1.1**。正式安装包以 Releases 页面为准。

## 功能

| 分组 | 内容 |
|---|---|
| 缓存 | 带宽、依赖链延迟、层级检测 |
| 内存 | 读写／复制／Scale／Add／Triad／步长带宽，读写／复制延迟 |
| 核心性能 | 整数、浮点、分支、核间通信、ComputeMark 单线程／多线程评分 |
| 核心架构分析 | 取指与解码扫描、BTB 布局、资源窗口、执行竞争、吞吐下界 |
| 小工具 | CPU、GPU、DRAM 参数，磁盘 SMART，压力测试与实时监控 |

测试可单独运行或组合运行。缓存延迟与层级检测同时选中时共用一次扫描。
核间热图显示整数标签，原始结果保留完整精度。

## 安装

| 系统 | 架构 | 格式 | 预编译桌面包环境 |
|---|---|---|---|
| macOS | x64、ARM64 | DMG | macOS 13+ |
| Windows | x64 | 安装 EXE、便携 ZIP | Windows 10 1809+ |
| Windows | ARM64 | 安装 EXE、便携 ZIP | Windows 11 |
| Linux | x64 | DEB、RPM、tar.gz | glibc 2.35+ |
| Linux | ARM64 | DEB、RPM、tar.gz | glibc 2.39+ |

- macOS：将应用拖入 Applications；Apple Silicon 选择 ARM64 包。
- Windows：运行安装程序，或解压 ZIP 后打开 `MemoryTest/MemoryTest.exe`。
- Linux：用系统包管理器安装 DEB／RPM，或解压后运行 `MemoryTest/MemoryTest`。

预览包未作发行签名／公证，首次启动可能出现系统提示。

## 源码运行

CLI 需要 Python 3.10+ 和 C 编译器。Linux 使用 GCC／Clang；macOS 安装 Xcode Command Line Tools；
Windows 使用 MSYS2 UCRT64（x64）或 CLANGARM64（ARM64）。HTML 和 SVG 报告只依赖 Python 标准库。

```bash
git clone https://github.com/AkaseToshiyuki/memorytest.git
cd memorytest
python3 run_benchmarks.py --list
python3 run_benchmarks.py --module cpu_alu
```

桌面端另需 Qt 依赖：

```bash
python3 -m venv .venv-desktop
# 激活虚拟环境后执行：
python -m pip install -r packaging/requirements-desktop.txt
python desktop_app.py
```

## 常用命令

```bash
make test                                 # 全部基准模块
make test MODULE=cpu_multi                 # ComputeMark
make test MODULES="cpu_alu cpu_float"       # 指定多个模块
make test CATEGORY=memory                  # 内存分类
python3 run_benchmarks.py -m cpu_architecture
python3 run_benchmarks.py -m cache_latency_test
python3 run_benchmarks.py -m memory_latency_test
python3 run_benchmarks.py -m cpu_multi --cpu-threads 2
python3 run_benchmarks.py --system-info     # 仅查询系统信息
```

CLI 的 `cpu` 分类包含核心架构分析；桌面端将它作为独立一级入口。
`--cpu-threads` 只限制 ComputeMark；压力测试从小工具单独启动。

| 参数 | 用途 |
|---|---|
| `--module / -m` | 选择模块，可重复指定；完整列表见 `--list` |
| `--category` | 选择 `cpu` 或 `memory` 分类 |
| `--timeout` | 单模块超时秒数 |
| `--output-dir` | 报告目录，CLI 默认 `reports/` |
| `--skip-build` | 使用已有引擎 |
| `--language zh/en`、`--theme light/dark` | 报告语言与主题 |

预编译应用也支持 CLI，例如：

```bash
MemoryTest --cli --skip-build --module cpu_multi --output-dir results
```

## 报告

桌面报告默认保存在程序旁的 `Outputs`，目录不可写时使用用户数据目录；界面显示实际位置。
macOS 使用 `.app` 同级目录。默认保留最近 10 轮基准／压力报告，长期留存请另行导出。

CLI 每轮保存在 `reports/runs/<run-id>/`，包含原始数据、日志、状态清单和自包含 HTML。
顶层 HTML 为最近结果：全部模块用 `benchmark_report.html`，单模块用 `<module>_report.html`，
多模块选择用 `selected_report.html`。

```bash
make report                               # 重生成最近一轮报告
make report RUN_DIR=reports/runs/<run-id>   # 重生成指定报告
```

## 硬件工具与压力测试

参数页支持刷新、取消、设备选择、复制与 JSON 导出。序列号默认隐藏。
安装包内置 smartctl；源码运行可使用系统安装的 smartmontools。
可读字段取决于设备、驱动与启动权限，缺失项显示原因。

权限在启动时确定，应用内不请求提权。硬件工具只读取数据，不修改 SPD、风扇、电压或磁盘状态。
压力工具支持 CPU、FPU、缓存、RAM 和 GPU；GPU 使用 macOS Metal 或 Windows／Linux OpenCL。
负载可随时停止，监控与结果单独保存。

## 结果含义

ComputeMark v2 采用固定标量负载，实测 1、2、4……至最大可用线程数：

`分数 = floor(1000 × sqrt(整数 Gop/s × FP64 GFLOP/s) + 0.5)`

比较成绩时匹配版本、编译配置、采样协议和调度策略。架构页以候选区间、吞吐下界或未定状态
显示结果；资源窗口的单位为填充指令。公式、采样默认值及判据见 [测量方法](MEASUREMENT.md)。

| 平台 | 核间测试 | 架构周期计数 |
|---|---|---|
| Linux | 绑定允许的逻辑 CPU，测量核间矩阵 | perf，受权限和硬件支持限制 |
| Apple Silicon | 实验性调度观察，允许核对缺失 | 可用时读取进程计数 |
| Intel Mac | 暂不支持 | 可用时读取进程计数 |
| Windows | 暂不支持 | 暂无，保留时间曲线 |

## 构建与开发

```bash
make tests                                # 构建原生引擎
make check-compute check-workflow check-platform
python packaging/build_desktop.py          # 在目标系统构建安装包
python packaging/check_artifact.py         # 检查本地安装包
python packaging/build_source.py           # 归档当前提交
```

安装包构建另需 Linux 的 DEB／RPM 工具、macOS 的 `hdiutil` 或 Windows 的 NSIS。
完整依赖与六平台流程见 [安装包工作流](.github/workflows/desktop-packages.yml)。
开发默认使用 `main`，约定见 [AGENTS.md](AGENTS.md)。

| 目录／文件 | 用途 |
|---|---|
| `src/` | 原生测量内核与平台接口 |
| `tests/` | 功能、数值与界面检查 |
| `packaging/` | 图标、许可证与打包工具 |
| `tests.json`、`benchmark_registry.py` | 模块与独立任务注册 |
| `result_model.py`、`presentation.py` | 桌面与 HTML 共享结果展示 |

## English

MemoryTest provides native desktop and CLI benchmarks for CPU, memory and cache on
Linux, macOS and Windows (x64/ARM64). It includes architecture probes, hardware
inventory, SMART details, stress tools, bilingual themes and offline HTML reports.

Download packages from [Releases](https://github.com/AkaseToshiyuki/memorytest/releases).
For source use, install Python 3.10+ and a C compiler, then run
`python3 run_benchmarks.py --list` or `python3 run_benchmarks.py --module cpu_multi`.
The desktop additionally requires `packaging/requirements-desktop.txt`.

## 许可证

项目使用 [MIT](LICENSE)。安装包附带 Qt、smartmontools 等依赖的许可与源码信息；
SMC 数据参考的归属见 [Stats-MIT.txt](packaging/Stats-MIT.txt)。
