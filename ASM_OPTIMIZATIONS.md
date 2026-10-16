# 计时与汇编辅助函数

实现位于 [src/asm_helpers.h](src/asm_helpers.h)。

## 计时

| 接口 | 当前行为 |
|---|---|
| `rdtsc_ns()` | 调用 `get_time_ns()` 返回单调时间，单位 ns；名称保留兼容 |
| `rdtsc_cycles()` | 旧原始计数接口：x86 TSC、ARM64 CNTVCT、RISC-V cycle、PowerPC time base |
| `ArchCounter` | 架构探针的硬件计数接口，见 [src/architecture_counters.h](src/architecture_counters.h) |

原始计数的单位、权限与排序依平台而异。耗时按批次墙钟计算，不按标称 MHz 换算。
ComputeMark 使用公共单调时钟与固定 C 标量内核。

## 屏障与预取

| 接口 | 实现 |
|---|---|
| `memory_fence()` | ARM64 `dmb ish`；x86 `mfence`；RISC-V `fence iorw, iorw`；PowerPC `sync`；其他使用原子栅栏 |
| `compiler_barrier()` | 编译器内存屏障，不发出硬件栅栏 |
| `prefetch_l1()` | ARM64 `prfm pldl1keep`；其他使用预取 builtin |
| `prefetch_l1_volatile()` | 接受 volatile 指针的 L1 预取包装 |
| `prefetch_l2()` | ARM64 `prfm pldl2keep`；其他使用预取 builtin |

## 缓存维护

`clflush()` 是平台相关旧接口：x86 使用 `clflush`，Linux ARM64 使用 `dc civac`，
Apple／Windows ARM64 仅执行屏障；其他架构按编译分支处理。
屏障回退不逐出缓存。当前缓存诊断使用完整置换环、预热及工作集扫描。

## 架构内核

`architecture_*.h` 使用编译期静态汇编，基线为 x86-64 SSE2／AArch64。
机器码布局、工作计数和计算结果由 [架构测试](tests/test_architecture.py) 校验。
采样与推断定义见 [测量方法](MEASUREMENT.md)。
