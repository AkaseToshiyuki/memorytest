/**
 * Memory Benchmark Common Definitions
 *
 * 拆分后此文件只保留:
 *   - 公共头文件包含
 *   - 全局配置变量 (定义)
 *
 * 各子系统实现已拆分到:
 *   util.c     - Sudo 密码管理 / 杂项工具
 *   detect.c   - 硬件检测 (cache/内存/频率/架构/NUMA)
 *   report.c   - 报告生成框架
 *   simd.c     - SIMD 能力检测
 *   pmu.c      - PMU/perf_event 计数器
 *   latency.c  - 缓存延迟测量统一接口
 */

#include "common.h"
#include <linux/perf_event.h>

#define REPORTS_DIR "reports"

/* Global cache configuration (see detect.c for initialization) */
CacheConfig global_cache_config;

/* Global system configuration (see detect.c for initialization) */
SystemConfig global_system_config;
