/**
 * CPU Multi-Core Performance Test
 *
 * 测试多核心并行性能:
 * - 所有核心同时运行
 * - 计算总吞吐量和并行效率
 * - 内存带宽饱和检测
 * - NUMA效率分析
 *
 * 跨平台支持: X86_64, ARM64, RISC-V, PowerPC
 */

#include "common.h"
#include "util.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <pthread.h>

#define ITERATIONS 500000000
#define WARMUP_ITERATIONS 1000000
/* MAX_THREADS: per-stack-array limit. See test_memory_bandwidth.c for
 * the rationale on the value 256. */
#define MAX_THREADS 256
#define MEM_TEST_SIZE (64 * 1024 * 1024)  /* 64MB for memory bandwidth test */

/* ========== 线程参数 ========== */
typedef struct {
    int thread_id;
    int num_threads;
    double result;
    double time_ms;
    PerfResult perf;
    void *buffer;  /* 用于内存带宽测试 */
} ThreadArgs;

/* ========== 同步屏障 ========== */
static pthread_barrier_t barrier;

/* ========== CPU绑定ALU运算线程 ========== */
static void *thread_alu_add(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    uint64_t per_thread = ITERATIONS / t->num_threads;
    uint64_t start = t->thread_id * per_thread;
    uint64_t end = start + per_thread;

    /* CPU绑定 */
    set_cpu_affinity(t->thread_id);

    uint64_t sum = 0;
    for (uint64_t i = start; i < end; i++) {
        sum += i;
    }
    t->result = (double)sum;
    return NULL;
}

/* Naming note: the dominant op is x86_64/ARM64 'div' (mod = remainder of div).
 * The % 1000000007 is a REAL modulo measurement, not just anti-opt noise. */
static void *thread_alu_mod(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    uint64_t per_thread = ITERATIONS / t->num_threads;
    uint64_t start = t->thread_id * per_thread;
    uint64_t end = start + per_thread;

    set_cpu_affinity(t->thread_id);

    uint64_t product = 1;
    for (uint64_t i = start + 1; i < end; i++) {
        product = (product * 3 + 7) % 1000000007;
    }
    t->result = (double)product;
    return NULL;
}

/* ========== 浮点运算线程 ========== */
static void *thread_double_add(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    double per_thread = (double)ITERATIONS / t->num_threads;
    double start = t->thread_id * per_thread;
    double end = start + per_thread;

    set_cpu_affinity(t->thread_id);

    double result = 0.0;
    for (double i = start; i < end; i++) {
        result += i * 0.5;
    }
    t->result = result;
    return NULL;
}

static void *thread_double_mul(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    double per_thread = (double)ITERATIONS / t->num_threads;
    double start = t->thread_id * per_thread;
    double end = start + per_thread;

    set_cpu_affinity(t->thread_id);

    double result = 1.0;
    for (double i = start + 1; i < end; i++) {
        result *= i * 0.00001;
        if (result < 0.0001) result = 1.0;
    }
    t->result = result;
    return NULL;
}

/* ========== 内存带宽测试线程 ========== */
static void *thread_mem_bw_read(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    volatile uint8_t *buf = (volatile uint8_t *)t->buffer;
    size_t size_per_thread = MEM_TEST_SIZE;  /* each thread reads a full 64 MB slice */
    size_t offset = (size_t)t->thread_id * MEM_TEST_SIZE;

    set_cpu_affinity(t->thread_id);

    /* 等待所有线程开始 */
    pthread_barrier_wait(&barrier);

    uint64_t sum = 0;
    size_t iterations = size_per_thread / 64;  /* 64字节步进 */

    for (size_t i = 0; i < iterations; i++) {
        /* 读一行缓存行 */
        sum += buf[offset + i * 64];
    }

    t->result = (double)sum;  /* 使用result作为临时存储 */
    (void)sum;  /* suppress unused warning */
    return NULL;
}

/* ========== 多核测试配置 ========== */
typedef struct {
    const char *name;
    const char *category;
    void *(*thread_func)(void *);
    int uses_memory;  /* 是否是内存带宽测试 */
} MultiCoreTest;

static MultiCoreTest tests[] = {
    {"Add",         "ALU",    thread_alu_add,      0},
    {"Mod",         "ALU",    thread_alu_mod,      0},
    {"double Add",  "FPU",    thread_double_add,   0},
    {"double Mul",  "FPU",    thread_double_mul,    0},
};

/* Get theoretical memory bandwidth (GB/s) from runtime DRAM speed detection.
 * Uses global_system_config.dram_speed_mt_s (detected via dmidecode/lscpu/sysfs
 * in initialize_dram_speed()). Returns 0 if DRAM speed is unknown (non-fatal:
 * the report shows "N/A" rather than a false estimate). */
static double get_theoretical_bw_gbps(void) {
    if (global_system_config.dram_speed_mt_s <= 0 ||
        global_system_config.memory_channels <= 0) {
        return 0.0;  /* unknown */
    }
    /* bandwidth_per_channel (GB/s) = MT/s × 8 bytes × 0.85 efficiency factor */
    double bw_per_channel =
        (double)global_system_config.dram_speed_mt_s * 8.0 * 0.85 / 1000.0;
    return (double)global_system_config.memory_channels * bw_per_channel;
}

static double run_multi_core_benchmark(void *(*func)(void *), int num_threads, int use_pthread_barrier) {
    pthread_t tids[MAX_THREADS];
    ThreadArgs args[MAX_THREADS];

    if (use_pthread_barrier) {
        pthread_barrier_init(&barrier, NULL, num_threads);
    }

    for (int i = 0; i < num_threads; i++) {
        args[i].thread_id = i;
        args[i].num_threads = num_threads;
        args[i].result = 0;
        args[i].time_ms = 0;
        args[i].buffer = NULL;
    }

    uint64_t start = get_time_ns();

    int threads_created = 0;
    for (int i = 0; i < num_threads; i++) {
        int ret = pthread_create(&tids[i], NULL, func, &args[i]);
        if (ret != 0) {
            fprintf(stderr, "Warning: thread %d creation failed: %s\n", i, strerror(ret));
            break;
        }
        threads_created++;
    }

    for (int i = 0; i < threads_created; i++) {
        pthread_join(tids[i], NULL);
    }

    uint64_t end = get_time_ns();

    if (use_pthread_barrier) {
        pthread_barrier_destroy(&barrier);
    }

    return (double)(end - start) / 1000000.0;  /* ms */
}

static double run_mem_bw_benchmark(int num_threads, void *buffer) {
    pthread_t tids[MAX_THREADS];
    ThreadArgs args[MAX_THREADS];

    pthread_barrier_init(&barrier, NULL, num_threads);

    for (int i = 0; i < num_threads; i++) {
        args[i].thread_id = i;
        args[i].num_threads = num_threads;
        args[i].result = 0;
        args[i].time_ms = 0;
        args[i].buffer = buffer;
    }

    uint64_t start = get_time_ns();

    int threads_created = 0;
    for (int i = 0; i < num_threads; i++) {
        int ret = pthread_create(&tids[i], NULL, thread_mem_bw_read, &args[i]);
        if (ret != 0) {
            fprintf(stderr, "Warning: thread %d creation failed: %s\n", i, strerror(ret));
            break;
        }
        threads_created++;
    }

    for (int i = 0; i < threads_created; i++) {
        pthread_join(tids[i], NULL);
    }

    uint64_t end = get_time_ns();

    pthread_barrier_destroy(&barrier);

    return (double)(end - start) / 1000000.0;  /* ms */
}

void run_cpu_multi_core_test(void) {
    print_header("CPU MULTI-CORE PERFORMANCE TEST");

    /* Use physical cores when HT/SMT is detected — same logic as inter_core test.
     * Testing HT siblings adds noise (shared execution units / caches) and
     * the points beyond physical-core count show scaling collapse anyway. */
    int physical = global_system_config.cpu_cores_physical;
    long num_cpus = (physical > 0) ? physical : sysconf(_SC_NPROCESSORS_ONLN);
    int max_threads = (int)num_cpus;
    if (max_threads < 1) max_threads = 1;
    if (max_threads > MAX_THREADS) {
        fprintf(stderr, "[multi] %d cores detected, but MAX_THREADS=%d. "
                        "Clamping to %d threads.\n",
                max_threads, MAX_THREADS, MAX_THREADS);
        max_threads = MAX_THREADS;
    }
    int channels = get_memory_channels();
    int freq = get_cpu_freq_mhz();

    printf("Available CPU cores: %ld\n", num_cpus);
    printf("Memory Channels: %d\n", channels);
    printf("CPU Frequency: %d MHz\n", freq);
    printf("Iterations: %d per core\n\n", ITERATIONS);

    /* Estimate theoretical bandwidth from DRAM speed detection */
    double theoretical_bw = get_theoretical_bw_gbps();
    if (theoretical_bw > 0.0) {
        printf("Estimated Memory Bandwidth: %.1f GB/s (DRAM %d MT/s × %d channels)\n\n",
               theoretical_bw, global_system_config.dram_speed_mt_s,
               global_system_config.memory_channels);
    } else {
        printf("Estimated Memory Bandwidth: N/A (DRAM speed unknown)\n\n");
    }

    int num_tests = sizeof(tests) / sizeof(tests[0]);
    int pmu_available = (perf_init_counters() == 0);

    printf("PMU Performance Counters: %s\n\n", pmu_available ? "Available" : "Not available");

    /* 预热单核 */
    printf("Warming up (single core)...\n");
    for (int i = 0; i < num_tests; i++) {
        ThreadArgs args = {0, 1, 0, 0, {0}};
        for (int w = 0; w < 3; w++) {
            tests[i].thread_func(&args);
        }
    }

    /* Generate thread counts: powers of 2, then evenly-spaced intermediates
     * up to max_threads so there is no huge jump (e.g. 16→96). */
    int thread_counts[16];
    int num_thread_counts = 0;
    for (int t = 1; t <= max_threads; t *= 2)
        thread_counts[num_thread_counts++] = t;
    if (thread_counts[num_thread_counts - 1] < max_threads) {
        int last = thread_counts[num_thread_counts - 1];
        int gap = max_threads - last;
        int step = gap / 5;
        if (step < 2) step = 2;
        for (int t = last + step; t < max_threads; t += step)
            thread_counts[num_thread_counts++] = t;
        thread_counts[num_thread_counts++] = max_threads;
    }

    printf("\n=== Multi-Core Scaling ===\n\n");

    printf("%-12s | %-8s | %-10s | %-10s | %-10s | %-10s | %-12s\n",
           "Operation", "Category", "Threads", "Time(ms)", "Speedup", "Efficiency", "Status");
    printf("%-12s | %-8s | %-10s | %-10s | %-10s | %-10s | %-12s\n",
           "------------", "--------", "----------", "----------", "----------", "----------", "------------");

    /* Store results for report reuse (avoid double measurement). */
    double stored_multi_time[16][16];
    double stored_speedup[16][16];
    double stored_eff[16][16];
    const char *stored_status[16][16];

    for (int i = 0; i < num_tests; i++) {
        const char *cat = tests[i].category;

        /* 先测试单核时间 */
        double single_time = run_multi_core_benchmark(tests[i].thread_func, 1, 0);

        printf("\n-- %s --\n", cat);

        for (int t = 0; t < num_thread_counts; t++) {
            int threads = thread_counts[t];
            if (threads > max_threads) continue;

            double multi_time = run_multi_core_benchmark(tests[i].thread_func, threads, 0);

            double speedup = single_time / multi_time;
            double efficiency = speedup / threads * 100.0;

            /* Status label: compute-bound tests drop efficiency from frequency
             * scaling / power limits when all cores are active, not bandwidth. */
            const char *status;
            int is_mem_test = tests[i].uses_memory;
            if (efficiency < 50.0) {
                status = is_mem_test ? "BANDWIDTH LIMIT" : "FREQ/TDP LIMIT";
            } else if (efficiency < 80.0) {
                status = is_mem_test ? "scalability limit" : "scaling limit";
            } else {
                status = "optimal";
            }

            printf("%-12s | %-8s | %-10d | %-10.2f | %-10.2fx | %-10.1f%% | %s\n",
                   tests[i].name, cat, threads, multi_time, speedup, efficiency, status);

            /* Store for report reuse */
            stored_multi_time[i][t] = multi_time;
            stored_speedup[i][t]   = speedup;
            stored_eff[i][t]       = efficiency;
            stored_status[i][t]    = status;
        }
    }

    /* 内存带宽饱和测试 */
    printf("\n\n=== Memory Bandwidth Saturation Test ===\n\n");

    printf("Testing memory bandwidth with increasing thread count...\n");
    printf("%-10s | %-12s | %-12s | %-15s | %-12s\n",
           "Threads", "BW(GB/s)", "Theor.BW", "Saturation", "Efficiency");
    printf("%-10s | %-12s | %-12s | %-15s | %-12s\n",
           "----------", "------------", "------------", "---------------", "------------");

    void *mem_buffer = malloc((size_t)MEM_TEST_SIZE * max_threads);
    size_t total_buffer = (size_t)MEM_TEST_SIZE * max_threads;

    /* Safety cap: never allocate more than 70% of available system memory.
     * Reduce per-thread buffer size proportionally if total would overflow. */
    {
        size_t mem_avail = detect_available_memory();
        if (mem_avail > 0) {
            size_t mem_cap = mem_avail * 70 / 100;
            if (total_buffer > mem_cap) {
                size_t new_per_thread = mem_cap / max_threads;
                if (new_per_thread < 16 * 1024 * 1024) new_per_thread = 16 * 1024 * 1024; /* 16 MB floor */
                total_buffer = new_per_thread * max_threads;
                printf("[multi] Available memory: %.1f GB → capping buffer to %.1f GB (70%%)\n",
                       (double)mem_avail / (1024.0*1024*1024),
                       (double)total_buffer / (1024.0*1024*1024));
                free(mem_buffer);
                mem_buffer = malloc(total_buffer);
            }
        }
    }
    if (mem_buffer) {
        memset(mem_buffer, 1, total_buffer);

        int mem_thread_counts[16];
        int num_mem_thread_counts = 0;
        for (int t = 1; t <= max_threads; t *= 2)
            mem_thread_counts[num_mem_thread_counts++] = t;
        if (mem_thread_counts[num_mem_thread_counts - 1] < max_threads) {
            int last = mem_thread_counts[num_mem_thread_counts - 1];
            int gap = max_threads - last;
            int step = gap / 5;
            if (step < 2) step = 2;
            for (int t = last + step; t < max_threads; t += step)
                mem_thread_counts[num_mem_thread_counts++] = t;
            mem_thread_counts[num_mem_thread_counts++] = max_threads;
        }

        /* 预热 */
        run_mem_bw_benchmark(1, mem_buffer);

        for (int t = 0; t < num_mem_thread_counts; t++) {
            int threads = mem_thread_counts[t];
            if (threads > max_threads) continue;

            double time_ms = run_mem_bw_benchmark(threads, mem_buffer);

            /* 计算实际带宽: size * threads / time */
            double bytes_per_sec = ((size_t)MEM_TEST_SIZE * threads) / (time_ms / 1000.0);
            double bw_gbs = bytes_per_sec / (1024.0 * 1024.0 * 1024.0);

            double saturation = (theoretical_bw > 0.0)
                ? (bw_gbs / theoretical_bw) * 100.0 : 0.0;
            if (saturation > 100.0) saturation = 100.0;
            double efficiency = (theoretical_bw > 0.0)
                ? (bw_gbs / theoretical_bw / threads) * 100.0 : 0.0;

            printf("%-10d | %-12.2f | %-12.1f | %-15.1f%% | %-12.1f%%\n",
                   threads, bw_gbs, theoretical_bw, saturation, efficiency);
        }

        free(mem_buffer);
    }

    /* Generate report */
    ReportContext *report = report_init("cpu_multi_core", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "Multi-Core Performance Test Results");

        report_write(report, "Available CPU cores: %ld\n", num_cpus);
        report_write(report, "Memory Channels: %d\n", channels);
        report_write(report, "CPU Frequency: %d MHz\n", freq);
        report_write(report, "Estimated Memory Bandwidth: %.1f GB/s\n\n", theoretical_bw);
        report_write(report, "PMU Available: %s\n\n", pmu_available ? "Yes" : "No");

        report_write(report, "| Operation | Category | Threads | Time(ms) | Speedup | Efficiency | Status |\n");
        report_write(report, "|-----------|----------|---------|----------|---------|------------|--------|\n");

        for (int i = 0; i < num_tests; i++) {
            const char *cat = tests[i].category;

            for (int t = 0; t < num_thread_counts; t++) {
                int threads = thread_counts[t];
                if (threads > max_threads) continue;

                /* Reuse values from display pass — no re-run */
                double multi_time = stored_multi_time[i][t];
                double speedup    = stored_speedup[i][t];
                double eff        = stored_eff[i][t];
                const char *status = stored_status[i][t];

                report_write(report, "| %s | %s | %d | %.2f | %.2fx | %.1f%% | %s |\n",
                           tests[i].name, cat, threads, multi_time, speedup, eff, status);
            }
            report_write(report, "\n");
        }

        report_section(report, "Notes");
        report_write(report, "- Speedup = Single-core time / Multi-core time\n");
        report_write(report, "- Efficiency = Speedup / Threads * 100%%\n");
        report_write(report, "- Compute-bound tests (ALU/FPU): efficiency < 50%% → FREQ/TDP LIMIT, 50-80%% → scaling limit (all-core frequency reduction / power throttling)\n");
        report_write(report, "- Memory-bound tests: efficiency < 50%% → BANDWIDTH LIMIT (memory controller saturated)\n");
        report_write(report, "- Efficiency > 80%% indicates near-optimal scaling\n\n");

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }
}

int main(int argc, char *argv[]) {
    init_platform_layer();
    pmu_init_cache_counters();
    print_system_info();
    run_cpu_multi_core_test();
    return 0;
}
