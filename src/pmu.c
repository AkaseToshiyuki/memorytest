/* SPDX-License-Identifier: MIT
 * pmu.c - PMU / perf_event_open / hardware performance counters
 *          + pmu_measure_cache_latency() (PMU-based cache latency path)
 *
 * pmu_measure_cache_latency is called by latency.c (measure_cache_latency dispatcher).
 * pmu_reset_counters / pmu_disable_counters / pmu_read_counter are also non-static
 * to support that call path.
 * Split from monolithic common.c (2026-06-02).
 */
#include "common.h"
#include <stddef.h>
#include <stdarg.h>
#include <string.h>
#include <signal.h>
#include <setjmp.h>
#include <sys/wait.h>
#include <linux/types.h>

/* PMUCacheCounters + pmu_available moved to global state in pmu.c; typedef in common.h */
PMUCacheCounters pmu_cache_counters = {-1, -1, -1, -1, -1, -1, -1};
int pmu_available = -1;

/* ARM64 PMU event IDs (architectural, portable across ARM cores) */
#define ARM_PMU_L1D_CACHE_REFILL   0x01
#define ARM_PMU_L1D_CACHE          0x04
#define ARM_PMU_L2D_CACHE_REFILL   0x16
#define ARM_PMU_L2D_CACHE          0x17
#define ARM_PMU_L3D_CACHE_REFILL   0x26
#define ARM_PMU_L3D_CACHE          0x27

/* ARM64 PMU events for CPI/IPC measurement */
#define ARM_PMU_CPU_CYCLES         0x11
#define ARM_PMU_INST_RETIRED       0x08
#define ARM_PMU_BRANCH_MISSPRED    0x10
#define ARM_PMU_MEM_ACCESS         0x06

/* x86 PMU events (Intel) */
#define X86_L1D_CACHE_REFILL       0xCB
#define X86_L2_L1D_CACHE_REFILL    0x24
#define X86_CPU_CYCLES             0x3C
#define X86_INST_RETIRED           0xC0
#define X86_BRANCH_MISSPRED        0xC5
#define X86_MEM_ACCESS             0x0D

/* ========== CPU Performance Counters ========== */
/* PerfCounters typedef: see common.h */

static PerfCounters perf_counters = {-1, -1, -1, -1, -1, -1};
static int perf_counters_initialized = 0;

/* ========== Cross-platform perf_event_open syscall ========== */
static long sys_perf_event_open(struct perf_event_attr *hw_event, pid_t pid, int cpu, int group_fd, unsigned long flags) {
#if defined(__x86_64__)
    return syscall(298, hw_event, pid, cpu, group_fd, flags);
#elif defined(__aarch64__)
    return syscall(241, hw_event, pid, cpu, group_fd, flags);
#elif defined(__riscv)
    return syscall(241, hw_event, pid, cpu, group_fd, flags);
#elif defined(__powerpc64__)
    return syscall(319, hw_event, pid, cpu, group_fd, flags);
#else
    return syscall(__NR_perf_event_open, hw_event, pid, cpu, group_fd, flags);
#endif
}

/* ========== PMU Cache Counter Initialization ========== */
int pmu_init_cache_counters(void) {
    /* Only init once */
    if (pmu_available >= 0) return pmu_available ? 0 : -1;
    pmu_available = 0;

    struct perf_event_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.size = sizeof(attr);
    attr.disabled = 1;
    attr.exclude_kernel = 1;
    attr.exclude_hv = 1;

#if defined(__aarch64__)
    /* ARM64: Use ARM PMU type (10) with RAW cache events */
    attr.type = 10;  /* PERF_TYPE_RAW for ARM */

    /* Try to open CPU cycles first - if this fails, PMU is not accessible */
    attr.config = 0x11;  /* cpu_cycles on ARM */
    pmu_cache_counters.cycles_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);
    if (pmu_cache_counters.cycles_fd < 0) {
        /* Try with type=0 (PERF_TYPE_HARDWARE) which may be more accessible */
        attr.type = 0;
        attr.config = 0;  /* CPU_CYCLES */
        pmu_cache_counters.cycles_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);
        if (pmu_cache_counters.cycles_fd < 0) {
            printf("[PMU] perf_event_open failed (cycles), trying cache events\n");
        }
    }

    /* L1D cache refill */
    attr.type = 10;
    attr.config = ARM_PMU_L1D_CACHE_REFILL;
    pmu_cache_counters.l1d_refill_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L1D cache access */
    attr.config = ARM_PMU_L1D_CACHE;
    pmu_cache_counters.l1d_access_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L2D cache refill */
    attr.config = ARM_PMU_L2D_CACHE_REFILL;
    pmu_cache_counters.l2_refill_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L2D cache access */
    attr.config = ARM_PMU_L2D_CACHE;
    pmu_cache_counters.l2_access_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L3D cache refill */
    attr.config = ARM_PMU_L3D_CACHE_REFILL;
    pmu_cache_counters.l3_refill_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L3D cache access */
    attr.config = ARM_PMU_L3D_CACHE;
    pmu_cache_counters.l3_access_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

#elif defined(__x86_64__)
    /* x86_64: Use Intel cache events */
    attr.type = 6;  /* PERF_TYPE_RAW */

    /* L1D cache refill */
    attr.config = X86_L1D_CACHE_REFILL;
    pmu_cache_counters.l1d_refill_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L1D cache access */
    attr.type = 6;
    attr.config = 0x2B;  /* L1D_CACHE_ACCESS */
    pmu_cache_counters.l1d_access_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L2 cache refill */
    attr.config = X86_L2_L1D_CACHE_REFILL;
    pmu_cache_counters.l2_refill_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L2 cache access */
    attr.config = 0x24;  /* L2_CACHE_ACCESS */
    pmu_cache_counters.l2_access_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L3 cache refill (Intel SNB+) */
    attr.type = 6;
    attr.config = 0x22;  /* L3_CACHE_REFILL */
    pmu_cache_counters.l3_refill_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L3 cache access */
    attr.config = 0x23;  /* L3_CACHE_ACCESS */
    pmu_cache_counters.l3_access_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* CPU cycles */
    attr.type = 0;  /* PERF_TYPE_HARDWARE */
    attr.config = 0;  /* CPU_CYCLES */
    pmu_cache_counters.cycles_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

#else
    /* Other architectures: not supported */
    return -1;
#endif

    /* Check if at least some counters opened successfully */
    if (pmu_cache_counters.l1d_refill_fd >= 0 || pmu_cache_counters.cycles_fd >= 0) {
        pmu_available = 1;
        printf("[PMU] Cache counters initialized (cycles=%d, L1 refill=%d, L2 refill=%d, L3 refill=%d)\n",
               pmu_cache_counters.cycles_fd >= 0,
               pmu_cache_counters.l1d_refill_fd >= 0,
               pmu_cache_counters.l2_refill_fd >= 0,
               pmu_cache_counters.l3_refill_fd >= 0);
        return 0;
    }

    /* Read paranoid level for debug message */
    FILE *f = fopen("/proc/sys/kernel/perf_event_paranoid", "r");
    int paranoid = 4;
    if (f) {
        if (fscanf(f, "%d", &paranoid) != 1) paranoid = -1;
        fclose(f);
    }
    printf("[PMU] Cache counters not available (perf_event_paranoid=%d)\n", paranoid);
    return -1;
}

/* ========== PMU Counter Reset/Enable ========== */
void pmu_reset_counters(void) {
    if (pmu_cache_counters.cycles_fd >= 0) {
        ioctl(pmu_cache_counters.cycles_fd, PERF_EVENT_IOC_RESET, 0);
        ioctl(pmu_cache_counters.cycles_fd, PERF_EVENT_IOC_ENABLE, 0);
    }
    if (pmu_cache_counters.l1d_refill_fd >= 0) {
        ioctl(pmu_cache_counters.l1d_refill_fd, PERF_EVENT_IOC_RESET, 0);
        ioctl(pmu_cache_counters.l1d_refill_fd, PERF_EVENT_IOC_ENABLE, 0);
    }
    if (pmu_cache_counters.l1d_access_fd >= 0) {
        ioctl(pmu_cache_counters.l1d_access_fd, PERF_EVENT_IOC_RESET, 0);
        ioctl(pmu_cache_counters.l1d_access_fd, PERF_EVENT_IOC_ENABLE, 0);
    }
}

void pmu_disable_counters(void) {
    if (pmu_cache_counters.cycles_fd >= 0)
        ioctl(pmu_cache_counters.cycles_fd, PERF_EVENT_IOC_DISABLE, 0);
    if (pmu_cache_counters.l1d_refill_fd >= 0)
        ioctl(pmu_cache_counters.l1d_refill_fd, PERF_EVENT_IOC_DISABLE, 0);
    if (pmu_cache_counters.l1d_access_fd >= 0)
        ioctl(pmu_cache_counters.l1d_access_fd, PERF_EVENT_IOC_DISABLE, 0);
}

/* ========== PMU Read Single Counter ========== */
uint64_t pmu_read_counter(int fd) {
    if (fd < 0) return 0;
    uint64_t val;
    if (read(fd, &val, sizeof(val)) != sizeof(val)) return 0;
    return val;
}

/* ========== CPU Performance Counter Initialization ========== */
int perf_init_counters(void) {
    if (perf_counters_initialized) return 0;
    perf_counters_initialized = 1;

    struct perf_event_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.size = sizeof(attr);
    attr.disabled = 1;
    attr.exclude_kernel = 1;
    attr.exclude_hv = 1;

#if defined(__aarch64__)
    attr.type = 10;  /* PERF_TYPE_RAW for ARM */

    /* CPU cycles */
    attr.config = ARM_PMU_CPU_CYCLES;
    perf_counters.cycles_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* Instructions retired */
    attr.config = ARM_PMU_INST_RETIRED;
    perf_counters.inst_retired_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* Branch misprediction */
    attr.config = ARM_PMU_BRANCH_MISSPRED;
    perf_counters.branch_mispred_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* Memory accesses */
    attr.config = ARM_PMU_MEM_ACCESS;
    perf_counters.mem_access_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L1D cache misses */
    attr.config = ARM_PMU_L1D_CACHE_REFILL;
    perf_counters.l1d_miss_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

#elif defined(__x86_64__)
    attr.type = 6;  /* PERF_TYPE_RAW */

    /* CPU cycles */
    attr.config = X86_CPU_CYCLES;
    perf_counters.cycles_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* Instructions retired */
    attr.config = X86_INST_RETIRED;
    perf_counters.inst_retired_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* Branch mispredictions */
    attr.config = X86_BRANCH_MISSPRED;
    perf_counters.branch_mispred_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* Memory accesses */
    attr.config = X86_MEM_ACCESS;
    perf_counters.mem_access_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

    /* L1 cache misses */
    attr.config = X86_L1D_CACHE_REFILL;
    perf_counters.l1d_miss_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);

#else
    /* Other architectures: not supported with PMU */
    return -1;
#endif

    return 0;
}

/* ========== Performance Counter Control ========== */
static void perf_reset_counters(void) {
    if (perf_counters.cycles_fd >= 0) {
        ioctl(perf_counters.cycles_fd, PERF_EVENT_IOC_RESET, 0);
        ioctl(perf_counters.cycles_fd, PERF_EVENT_IOC_ENABLE, 0);
    }
    if (perf_counters.inst_retired_fd >= 0) {
        ioctl(perf_counters.inst_retired_fd, PERF_EVENT_IOC_RESET, 0);
        ioctl(perf_counters.inst_retired_fd, PERF_EVENT_IOC_ENABLE, 0);
    }
    if (perf_counters.branch_mispred_fd >= 0) {
        ioctl(perf_counters.branch_mispred_fd, PERF_EVENT_IOC_RESET, 0);
        ioctl(perf_counters.branch_mispred_fd, PERF_EVENT_IOC_ENABLE, 0);
    }
}

static void perf_disable_counters(void) {
    if (perf_counters.cycles_fd >= 0)
        ioctl(perf_counters.cycles_fd, PERF_EVENT_IOC_DISABLE, 0);
    if (perf_counters.inst_retired_fd >= 0)
        ioctl(perf_counters.inst_retired_fd, PERF_EVENT_IOC_DISABLE, 0);
    if (perf_counters.branch_mispred_fd >= 0)
        ioctl(perf_counters.branch_mispred_fd, PERF_EVENT_IOC_DISABLE, 0);
}

/* ========== Measure Performance with PMU ========== */
/**
 * Run a function and measure its CPI/IPC and cache performance
 * Returns 0 on success, -1 if PMU not available
 */
int perf_measure(void (*func)(void*), void *arg, PerfResult *result) {
    if (perf_init_counters() != 0) return -1;
    if (result == NULL) return -1;

    /* Reset and start counters */
    perf_reset_counters();

    /* Run the function */
    func(arg);

    /* Stop counters and read values */
    perf_disable_counters();

    result->cycles = pmu_read_counter(perf_counters.cycles_fd);
    result->instructions = pmu_read_counter(perf_counters.inst_retired_fd);
    result->branch_mispred = pmu_read_counter(perf_counters.branch_mispred_fd);
    result->mem_accesses = pmu_read_counter(perf_counters.mem_access_fd);
    result->l1d_misses = pmu_read_counter(perf_counters.l1d_miss_fd);
    result->l2_misses = 0; /* Not always available */

    /* Calculate derived metrics */
    if (result->instructions > 0) {
        result->cpi = (double)result->cycles / result->instructions;
        result->ipc = (double)result->instructions / result->cycles;
    } else {
        result->cpi = 0;
        result->ipc = 0;
    }

    if (result->branch_mispred > 0 && result->instructions > 0) {
        result->branch_mispred_rate = (double)result->branch_mispred / result->instructions * 100.0;
    } else {
        result->branch_mispred_rate = 0;
    }

    return 0;
}

/* ========== Fallback: Estimate CPI via timing ========== */
/**
 * Estimate CPI using timing-based method
 * This is the fallback when PMU is not available
 */
static PerfResult perf_estimate_cpi_fallback(uint64_t iterations, double ns_per_op) {
    PerfResult result = {0};

    /* Get CPU frequency for cycle estimation.
     * If freq unknown (0), we cannot derive cycles from wall-clock ns.
     * Return zeroed result to signal "no estimate possible" (caller must
     * check instructions > 0). */
    int freq = global_system_config.cpu_freq_mhz;
    if (freq <= 0) {
        /* No hardcoded fallback. cycles=0, instructions=0, cpi=0, ipc=0.
         * Caller (perf_measure) will see instructions=0 and return non-zero,
         * and use_pmu=0 fallback path will handle it. */
        return result;
    }

    double cycles_per_ns = freq / 1000.0;  /* MHz to cycles/ns */
    double total_cycles = ns_per_op * cycles_per_ns;

    result.cycles = (uint64_t)(total_cycles * iterations);
    result.instructions = iterations;

    if (result.instructions > 0) {
        result.cpi = (double)result.cycles / result.instructions;
        result.ipc = (double)result.instructions / result.cycles;
    }

    /* Timing-based estimates have limited accuracy */
    result.branch_mispred_rate = -1;  /* Indicates estimate, not measured */

    return result;
}

/* ========== Print Performance Result ========== */
void perf_print_result(const PerfResult *r, int show_pmu) {
    if (show_pmu && r->branch_mispred_rate >= 0) {
        printf("  CPI: %.2f, IPC: %.2f, BranchMispred: %.2f%%",
               r->cpi, r->ipc, r->branch_mispred_rate);
    } else if (show_pmu) {
        printf("  CPI: %.2f, IPC: %.2f (timing estimate)",
               r->cpi, r->ipc);
    } else {
        printf("  [PMU not available - using timing estimate]");
    }
    printf("\n");
}

/* ========== PMU-based Cache Latency Measurement ========== */
/**
 * Measure cache-level latency using PMU cache refill events
 * Returns latency in nanoseconds, or 0 if PMU unavailable
 */
uint64_t pmu_measure_cache_latency(void *ptr, int level) {
    if (pmu_available != 1) return 0;

    volatile uint64_t *p = (volatile uint64_t *)ptr;
    int fd_refill, fd_access;

    /* Select counters based on cache level */
    switch (level) {
        case 1:
            fd_refill = pmu_cache_counters.l1d_refill_fd;
            fd_access = pmu_cache_counters.l1d_access_fd;
            break;
        case 2:
            fd_refill = pmu_cache_counters.l2_refill_fd;
            fd_access = pmu_cache_counters.l2_access_fd;
            break;
        case 3:
            fd_refill = pmu_cache_counters.l3_refill_fd;
            fd_access = pmu_cache_counters.l3_access_fd;
            break;
        default:
            return 0;
    }

    if (fd_refill < 0 && fd_access < 0) return 0;

    /* Reset and enable counters */
    pmu_reset_counters();

    /* Warm up: access data first */
    volatile uint64_t sum = 0;
    for (int i = 0; i < 64; i++) {
        sum += p[i];
    }
    (void)sum;

    /* Reset counters after warmup */
    pmu_reset_counters();

    /* Perform controlled memory accesses */
    uint64_t start_cycles = pmu_read_counter(pmu_cache_counters.cycles_fd);
    uint64_t accesses = 0;

    /* Sequential access pattern - 64 iterations */
    for (int i = 0; i < 64; i++) {
        volatile uint64_t val = p[i];
        (void)val;
        accesses++;
    }

    uint64_t end_cycles = pmu_read_counter(pmu_cache_counters.cycles_fd);
    uint64_t cycle_count = end_cycles - start_cycles;

    pmu_disable_counters();

    /* Calculate latency per access */
    if (cycle_count > 0 && accesses > 0) {
        /* Get CPU frequency for cycle to ns conversion.
         * If freq unknown (0), we cannot convert cycles to ns.
         * Return 0 to signal "cannot measure latency". */
        static double cycles_per_ns = 0;
        if (cycles_per_ns == 0) {
            int freq = global_system_config.cpu_freq_mhz;
            if (freq <= 0) {
                /* No hardcoded fallback. Return 0 — caller treats as "not measured". */
                pmu_disable_counters();
                return 0.0;
            }
            cycles_per_ns = freq / 1000.0;  /* MHz to cycles/ns */
        }
        return (cycle_count / accesses) / cycles_per_ns;
    }

    return 0;
}
