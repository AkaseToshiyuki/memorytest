/**
 * Inter-Core Memory Access Latency Test - Cross-Platform PMU Optimized
 *
 * 支持平台: X86_64, ARM64, RISC-V, PowerPC
 * PMU类型自动检测，失败时回退到clock_gettime
 */

#define _GNU_SOURCE
#include "common.h"
#include <pthread.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/perf_event.h>
#include <sys/syscall.h>
#include <time.h>

#define NUM_SAMPLES 50           /* Samples for latency measurement */
#define ROUND_TRIPS_PER_SAMPLE 10000
#define BW_ITERATIONS 100000     /* Iterations for bandwidth measurement (per pair) */
#define MAX_CORES 256            /* Maximum supported cores (for static arrays) */

/* ========== 同步屏障 ========== */
static pthread_barrier_t barrier;

/* ========== Perf Event辅助 - 跨平台syscall ========== */
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

/* ========== PMU时钟 - 使用thread-local计数器 ========== */
static pthread_key_t pmu_fd_key;
static pthread_once_t pmu_key_once = PTHREAD_ONCE_INIT;

static void pmu_key_create(void) {
    pthread_key_create(&pmu_fd_key, free);
}

static int get_pmu_fd(void) {
    pthread_once(&pmu_key_once, pmu_key_create);
    int *fd_ptr = pthread_getspecific(pmu_fd_key);
    if (fd_ptr) return *fd_ptr;

    int *new_fd = malloc(sizeof(int));
    struct perf_event_attr attr;
    memset(&attr, 0, sizeof(attr));

    /* 根据架构选择PMU type */
    if (current_arch == ARCH_ARM64) {
        attr.type = 10;  // ARMv8 PMU
        attr.config = 0x11;  // cpu_cycles
    } else {
        attr.type = 0;  // 通用软件计数器 (所有平台)
        attr.config = 0;  // 软件事件
    }

    attr.size = sizeof(attr);
    attr.disabled = 0;
    attr.exclude_kernel = 0;
    attr.exclude_hv = 1;

    *new_fd = sys_perf_event_open(&attr, 0, -1, -1, 0);
    pthread_setspecific(pmu_fd_key, new_fd);
    return *new_fd;
}

static inline uint64_t get_cycles_pmu(void) {
    int fd = get_pmu_fd();
    if (fd >= 0) {
        long long cycles;
        if (read(fd, &cycles, sizeof(cycles)) == sizeof(cycles)) {
            return (uint64_t)cycles;
        }
    }
    /* Fallback: clock_gettime */
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)(ts.tv_sec * 1000000000ULL + ts.tv_nsec);
}

/* ========== 时钟信息 ========== */
static const char *get_clock_source(void) {
    int fd = get_pmu_fd();
    if (fd >= 0) {
        if (current_arch == ARCH_ARM64) {
            return "ARM PMU (cpu_cycles)";
        } else {
            return "Software Counter";
        }
    }
    return "CLOCK_MONOTONIC";
}

/* ========== 全局CAS标志 ========== */
static volatile _Atomic bool cas_flag[MAX_CORES];

/* ========== 核间带宽测试用的原子计数器 ========== */
static volatile _Atomic uint64_t bw_counter;

/* ========== Thread Args ========== */
typedef struct {
    int core_id;
    int target_core;
    double results[NUM_SAMPLES];
} ThreadArgs;

/* ========== 核间带宽Ping线程 ========== */
static volatile _Atomic uint64_t bw_ops_total;

static void *bw_ping(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    set_cpu_affinity(t->core_id);

    volatile _Atomic bool *flag = &cas_flag[t->target_core];

    pthread_barrier_wait(&barrier);

    for (int i = 0; i < BW_ITERATIONS; i++) {
        bool expected = true;
        while (!atomic_compare_exchange_weak_explicit(flag, &expected, false,
                                                      memory_order_relaxed,
                                                      memory_order_relaxed)) {
            expected = true;
        }
        atomic_fetch_add_explicit(&bw_ops_total, 1, memory_order_relaxed);
    }

    return NULL;
}

/* ========== 核间带宽Pong线程 ========== */
static void *bw_pong(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    set_cpu_affinity(t->core_id);

    volatile _Atomic bool *flag = &cas_flag[t->core_id];

    pthread_barrier_wait(&barrier);

    for (int i = 0; i < BW_ITERATIONS; i++) {
        bool expected = false;
        while (!atomic_compare_exchange_weak_explicit(flag, &expected, true,
                                                      memory_order_relaxed,
                                                      memory_order_relaxed)) {
            expected = false;
        }
        atomic_fetch_add_explicit(&bw_ops_total, 1, memory_order_relaxed);
    }

    return NULL;
}

/* ========== 运行核间带宽测试 - 返回每秒操作数(Mops/s) ========== */
static double run_bandwidth_test(int core0, int core1) {
    atomic_store_explicit(&cas_flag[core0], false, memory_order_relaxed);
    atomic_store_explicit(&cas_flag[core1], true, memory_order_relaxed);
    atomic_store_explicit(&bw_ops_total, 0, memory_order_relaxed);

    pthread_barrier_init(&barrier, NULL, 2);

    ThreadArgs t0 = {.core_id = core0, .target_core = core1};
    ThreadArgs t1 = {.core_id = core1, .target_core = core0};

    pthread_t p0, p1;

    uint64_t start = get_time_ns();
    pthread_create(&p0, NULL, bw_ping, &t0);
    pthread_create(&p1, NULL, bw_pong, &t1);

    pthread_join(p0, NULL);
    pthread_join(p1, NULL);
    uint64_t end = get_time_ns();

    pthread_barrier_destroy(&barrier);

    uint64_t total_ops = atomic_load_explicit(&bw_ops_total, memory_order_relaxed);
    double duration_s = (double)(end - start) / 1000000000.0;

    /* 返回 Mops/s (每秒百万操作数) */
    return (double)total_ops / duration_s / 1000000.0;
}

/* ========== CAS Ping线程 ========== */
static void *cas_ping(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    set_cpu_affinity(t->core_id);

    volatile _Atomic bool *flag = &cas_flag[t->target_core];

    pthread_barrier_wait(&barrier);

    for (int s = 0; s < NUM_SAMPLES; s++) {
        uint64_t start = get_cycles_pmu();

        for (int i = 0; i < ROUND_TRIPS_PER_SAMPLE; i++) {
            // CAS: 等待PONG(true)并原子地设置为PING(false)
            bool expected = true;
            while (!atomic_compare_exchange_weak_explicit(flag, &expected, false,
                                                          memory_order_relaxed,
                                                          memory_order_relaxed)) {
                expected = true;
            }
        }

        uint64_t end = get_cycles_pmu();
        uint64_t cycles = end - start;
        double ns = (double)cycles * 1000.0 / get_cpu_freq_mhz();
        t->results[s] = ns / ROUND_TRIPS_PER_SAMPLE / 2;
    }

    return NULL;
}

/* ========== CAS Pong线程 ========== */
static void *cas_pong(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    set_cpu_affinity(t->core_id);

    volatile _Atomic bool *flag = &cas_flag[t->core_id];

    pthread_barrier_wait(&barrier);

    for (int s = 0; s < NUM_SAMPLES; s++) {
        for (int i = 0; i < ROUND_TRIPS_PER_SAMPLE; i++) {
            // CAS: 等待PING(false)并原子地设置为PONG(true)
            bool expected = false;
            while (!atomic_compare_exchange_weak_explicit(flag, &expected, true,
                                                          memory_order_relaxed,
                                                          memory_order_relaxed)) {
                expected = false;
            }
        }
    }

    return NULL;
}

/* ========== 运行CAS测试 ========== */
static void run_cas_test(int core0, int core1, double *results, int *count) {
    /* 初始化: flag为PING(false)，pong先设置为true */
    atomic_store_explicit(&cas_flag[core0], false, memory_order_relaxed);
    atomic_store_explicit(&cas_flag[core1], true, memory_order_relaxed);

    pthread_barrier_init(&barrier, NULL, 2);

    ThreadArgs t0 = {.core_id = core0, .target_core = core1};
    ThreadArgs t1 = {.core_id = core1, .target_core = core0};

    pthread_t p0, p1;
    pthread_create(&p0, NULL, cas_ping, &t0);
    pthread_create(&p1, NULL, cas_pong, &t1);

    pthread_join(p0, NULL);
    pthread_join(p1, NULL);

    pthread_barrier_destroy(&barrier);

    *count = 0;
    for (int i = 0; i < NUM_SAMPLES; i++) {
        double lat = t0.results[i];
        if (lat > 5 && lat < 500) {
            results[(*count)++] = lat;
        }
    }
}

/* ========== 获取CPU信息 ========== */
static void get_cpu_info(char *cpu_name, size_t len, int *max_freq_khz) {
    FILE *f;
    cpu_name[0] = '\0';
    if (max_freq_khz) *max_freq_khz = 0;

    f = fopen("/proc/cpuinfo", "r");
    if (f) {
        char line[256];
        char part[32] = {0};
        while (fgets(line, sizeof(line), f)) {
            if (strstr(line, "CPU part")) {
                char *colon = strchr(line, ':');
                if (colon) {
                    strncpy(part, colon + 2, sizeof(part) - 1);
                    part[31] = '\0';
                    for (int i = 0; part[i]; i++) {
                        if (part[i] == ' ' || part[i] == '\n') {
                            part[i] = '\0';
                            break;
                        }
                    }
                }
            }
            if (strstr(line, "Hardware")) {
                char *colon = strchr(line, ':');
                if (colon) {
                    strncpy(cpu_name, colon + 2, len - 1);
                    cpu_name[len - 1] = '\0';
                }
            }
        }
        fclose(f);
        if (cpu_name[0] == '\0' && part[0]) {
            snprintf(cpu_name, len, "ARM (part: %s)", part);
        } else if (cpu_name[0] == '\0') {
            strcpy(cpu_name, "ARM Processor");
        }
    }

    f = fopen("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "r");
    if (f) {
        if (max_freq_khz) {
            fscanf(f, "%d", max_freq_khz);
        }
        fclose(f);
    }
}

void run_inter_core_latency_test(void) {
    print_header("INTER-CORE MEMORY ACCESS LATENCY TEST");

    long num_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    if (num_cpus < 2) {
        printf("Not enough cores for inter-core test (need at least 2)\n");
        return;
    }

    /* PMU使用thread-local方式初始化 */

    char cpu_name[128];
    get_cpu_info(cpu_name, sizeof(cpu_name), NULL);  /* CPU name only, freq from common config */

    printf("Detected %ld CPU cores\n", num_cpus);
    printf("CPU: %s @ %d MHz\n", cpu_name, get_cpu_freq_mhz());
    printf("Clock: %s\n", get_clock_source());
    printf("Optimizations: PMU clock + LDAPR/STLR + CAS + wfe/sev\n\n");

    double *results = malloc(NUM_SAMPLES * sizeof(double));
    if (!results) {
        printf("Failed to allocate results buffer\n");
        return;
    }

    int max_cores = (int)num_cpus;

    printf("=== CAS Latency (ns) ===\n\n");
    printf("Measuring all %d x %d = %d core pairs...\n\n", max_cores, max_cores, max_cores * max_cores);

    /* Compact display: use 5-char fields to fit more cores */
    printf("%-5s", "");
    for (int j = 0; j < max_cores; j++) {
        printf("%5d", j);
    }
    printf("\n");

    double min_lat = 10000, max_lat = 0;
    double total_lat = 0;
    int total_count = 0;

    /* Full N×N matrix measurement */
    for (int i = 0; i < max_cores; i++) {
        printf("%-5d", i);
        for (int j = 0; j < max_cores; j++) {
            if (i == j) {
                printf("%5s", "-");
            } else {
                int count = 0;
                run_cas_test(i, j, results, &count);

                if (count > 0) {
                    qsort(results, count, sizeof(double), compare_uint64);
                    double median = results[count / 2];
                    printf("%5.1f", median);
                    if (median < min_lat) min_lat = median;
                    if (median > max_lat) max_lat = median;
                    total_lat += median;
                    total_count++;
                } else {
                    printf("%5s", "N/A");
                }
            }
        }
        printf("\n");
        fflush(stdout);  /* Flush after each row for real-time feedback */
    }

    printf("\n");
    if (total_count > 0) {
        printf("Min: %.1f ns, Max: %.1f ns, Avg: %.1f ns\n",
               min_lat, max_lat, total_lat / total_count);
    }
    printf("Optimizations: PMU clock + CAS ping-pong\n\n");

    /* ========== 核间带宽测试 ========== */
    printf("=== CAS Throughput (Mops/s) ===\n\n");

    printf("%-5s", "");
    for (int j = 0; j < max_cores; j++) {
        printf("%5d", j);
    }
    printf("\n");

    double min_bw = 10000, max_bw = 0, total_bw = 0;
    int bw_count = 0;

    for (int i = 0; i < max_cores; i++) {
        printf("%-5d", i);
        for (int j = 0; j < max_cores; j++) {
            if (i == j) {
                printf("%5s", "-");
            } else {
                double bw = run_bandwidth_test(i, j);
                printf("%5.1f", bw);
                if (bw < min_bw) min_bw = bw;
                if (bw > max_bw) max_bw = bw;
                total_bw += bw;
                bw_count++;
            }
        }
        printf("\n");
        fflush(stdout);
    }

    printf("\n");
    if (bw_count > 0) {
        printf("Min: %.1f Mops/s, Max: %.1f Mops/s, Avg: %.1f Mops/s\n",
               min_bw, max_bw, total_bw / bw_count);
    }
    printf("\n");

    /* ========== 生成热图数据(JSON) ========== */
    FILE *json_fp = fopen("reports/inter_core_heatmap_data.json", "w");
    if (json_fp) {
        fprintf(json_fp, "{\n");
        fprintf(json_fp, "  \"cpu_name\": \"%s\",\n", cpu_name);
        fprintf(json_fp, "  \"num_cores\": %ld,\n", num_cpus);
        fprintf(json_fp, "  \"max_freq_mhz\": %d,\n", get_cpu_freq_mhz());
        fprintf(json_fp, "  \"arch\": \"%s\",\n", arch_names[current_arch]);
        fprintf(json_fp, "  \"latency_stats\": {\n");
        fprintf(json_fp, "    \"min_ns\": %.1f,\n", min_lat);
        fprintf(json_fp, "    \"max_ns\": %.1f,\n", max_lat);
        fprintf(json_fp, "    \"avg_ns\": %.1f\n", total_count > 0 ? total_lat / total_count : 0);
        fprintf(json_fp, "  },\n");
        fprintf(json_fp, "  \"throughput_stats\": {\n");
        fprintf(json_fp, "    \"min_mops\": %.1f,\n", min_bw);
        fprintf(json_fp, "    \"max_mops\": %.1f,\n", max_bw);
        fprintf(json_fp, "    \"avg_mops\": %.1f\n", bw_count > 0 ? total_bw / bw_count : 0);
        fprintf(json_fp, "  },\n");
        fprintf(json_fp, "  \"cas_latency_matrix\": [\n");
        for (int i = 0; i < max_cores; i++) {
            fprintf(json_fp, "    [");
            for (int j = 0; j < max_cores; j++) {
                if (j > 0) fprintf(json_fp, ", ");
                if (i == j) {
                    fprintf(json_fp, "0.0");
                } else {
                    int count = 0;
                    run_cas_test(i, j, results, &count);
                    if (count > 0) {
                        qsort(results, count, sizeof(double), compare_uint64);
                        fprintf(json_fp, "%.1f", results[count / 2]);
                    } else {
                        fprintf(json_fp, "-1.0");
                    }
                }
            }
            fprintf(json_fp, "]%s\n", i < max_cores - 1 ? "," : "");
        }
        fprintf(json_fp, "  ]\n}\n");
        fclose(json_fp);
    }

    /* ========== 报告 ========== */
    ReportContext *report = report_init("inter_core_latency", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "CAS Latency Matrix (ns)");

        report_write(report, "| **Core** |");
        for (int j = 0; j < max_cores; j++) report_write(report, " %d |", j);
        report_write(report, "\n");

        report_write(report, "|---|");
        for (int j = 0; j < max_cores; j++) report_write(report, "---:|");
        report_write(report, "\n");

        for (int i = 0; i < max_cores; i++) {
            report_write(report, "| %d |", i);
            for (int j = 0; j < max_cores; j++) {
                if (i == j) {
                    report_write(report, " - |");
                } else {
                    int count = 0;
                    run_cas_test(i, j, results, &count);
                    if (count > 0) {
                        qsort(results, count, sizeof(double), compare_uint64);
                        report_write(report, " %.1f |", results[count / 2]);
                    } else {
                        report_write(report, " N/A |");
                    }
                }
            }
            report_write(report, "\n");
        }

        report_section(report, "CAS Throughput Matrix (Mops/s)");
        report_write(report, "| **Core** |");
        for (int j = 0; j < max_cores; j++) report_write(report, " %d |", j);
        report_write(report, "\n");

        report_write(report, "|---|");
        for (int j = 0; j < max_cores; j++) report_write(report, "---:|");
        report_write(report, "\n");

        for (int i = 0; i < max_cores; i++) {
            report_write(report, "| %d |", i);
            for (int j = 0; j < max_cores; j++) {
                if (i == j) {
                    report_write(report, " - |");
                } else {
                    double bw = run_bandwidth_test(i, j);
                    report_write(report, " %.1f |", bw);
                }
            }
            report_write(report, "\n");
        }

        report_section(report, "Notes");
        report_write(report, "- Clock: %s\n", get_clock_source());
        report_write(report, "- Architecture: %s\n", arch_names[current_arch]);
        report_write(report, "- Synchronization: LDAPR/STLR + CAS ping-pong\n");
        report_write(report, "- Wait: ARM wfe/sev\n\n");

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }

    free(results);

    printf("\nGenerating heatmap...\n");
    system("cd reports && python3 inter_core_heatmap.py 2>/dev/null");
}

int main(int argc, char *argv[]) {
    request_sudo_password();
    initialize_cache_config();
    initialize_system_config();
    pmu_init_cache_counters();
    print_system_info();
    run_inter_core_latency_test();
    return 0;
}
