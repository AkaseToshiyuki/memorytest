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
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <pthread.h>

#define ITERATIONS 100000000
#define WARMUP_ITERATIONS 1000000
#define MAX_THREADS 64
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

static void *thread_alu_mul(void *arg) {
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
    size_t size_per_thread = MEM_TEST_SIZE / t->num_threads;
    size_t offset = t->thread_id * size_per_thread;

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
    {"Mul",         "ALU",    thread_alu_mul,      0},
    {"double Add",  "FPU",    thread_double_add,   0},
    {"double Mul",  "FPU",    thread_double_mul,    0},
};

/* 估计理论内存带宽 (GB/s) */
static double estimate_theoretical_bw(void) {
    int channels = get_memory_channels();
    int freq = get_cpu_freq_mhz();

    /* 简化的DDR带宽估计: channels * freq * 8 bytes (DDR8) */
    /* 实际带宽 = 理论带宽 * 0.85 (效率因子) */
    double bw_per_channel = freq * 8.0 * 0.85;  /* GB/s */
    return channels * bw_per_channel;
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

    for (int i = 0; i < num_threads; i++) {
        pthread_create(&tids[i], NULL, func, &args[i]);
    }

    for (int i = 0; i < num_threads; i++) {
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

    for (int i = 0; i < num_threads; i++) {
        pthread_create(&tids[i], NULL, thread_mem_bw_read, &args[i]);
    }

    for (int i = 0; i < num_threads; i++) {
        pthread_join(tids[i], NULL);
    }

    uint64_t end = get_time_ns();

    pthread_barrier_destroy(&barrier);

    return (double)(end - start) / 1000000.0;  /* ms */
}

void run_cpu_multi_core_test(void) {
    print_header("CPU MULTI-CORE PERFORMANCE TEST");

    long num_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    int max_threads = (int)num_cpus;
    int channels = get_memory_channels();
    int freq = get_cpu_freq_mhz();

    printf("Available CPU cores: %ld\n", num_cpus);
    printf("Memory Channels: %d\n", channels);
    printf("CPU Frequency: %d MHz\n", freq);
    printf("Iterations: %d per core\n\n", ITERATIONS);

    /* 估计理论带宽 */
    double theoretical_bw = estimate_theoretical_bw();
    printf("Estimated Memory Bandwidth: %.1f GB/s (%.1f GB/s per channel)\n\n",
           theoretical_bw, theoretical_bw / channels);

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

    /* 测试不同线程数 */
    int thread_counts[] = {1, 2, 4, 8, 16, max_threads};
    int num_thread_counts = sizeof(thread_counts) / sizeof(thread_counts[0]);

    printf("\n=== Multi-Core Scaling ===\n\n");

    printf("%-12s | %-8s | %-10s | %-10s | %-10s | %-10s | %-12s\n",
           "Operation", "Category", "Threads", "Time(ms)", "Speedup", "Efficiency", "Status");
    printf("%-12s | %-8s | %-10s | %-10s | %-10s | %-10s | %-12s\n",
           "------------", "--------", "----------", "----------", "----------", "----------", "------------");

    /* 存储单核时间用于speedup计算 */
    double single_core_times[16] = {0};

    for (int i = 0; i < num_tests; i++) {
        const char *cat = tests[i].category;

        /* 先测试单核时间 */
        double single_time = run_multi_core_benchmark(tests[i].thread_func, 1, 0);
        single_core_times[i] = single_time;

        printf("\n-- %s --\n", cat);

        for (int t = 0; t < num_thread_counts; t++) {
            int threads = thread_counts[t];
            if (threads > max_threads) continue;

            double multi_time = run_multi_core_benchmark(tests[i].thread_func, threads, 0);

            double speedup = single_time / multi_time;
            double efficiency = speedup / threads * 100.0;

            /* 判断是否带宽受限 */
            const char *status;
            if (efficiency < 50.0) {
                status = "BANDWIDTH LIMIT";
            } else if (efficiency < 80.0) {
                status = "scalability limit";
            } else {
                status = "optimal";
            }

            printf("%-12s | %-8s | %-10d | %-10.2f | %-10.2fx | %-10.1f%% | %s\n",
                   tests[i].name, cat, threads, multi_time, speedup, efficiency, status);
        }
    }

    /* 内存带宽饱和测试 */
    printf("\n\n=== Memory Bandwidth Saturation Test ===\n\n");

    printf("Testing memory bandwidth with increasing thread count...\n");
    printf("%-10s | %-12s | %-12s | %-15s | %-12s\n",
           "Threads", "BW(GB/s)", "Theor.BW", "Saturation", "Efficiency");
    printf("%-10s | %-12s | %-12s | %-15s | %-12s\n",
           "----------", "------------", "------------", "---------------", "------------");

    void *mem_buffer = malloc(MEM_TEST_SIZE * 2);
    if (mem_buffer) {
        memset(mem_buffer, 1, MEM_TEST_SIZE * 2);

        int mem_thread_counts[] = {1, 2, 4, 8, 16, max_threads};
        int num_mem_thread_counts = sizeof(mem_thread_counts) / sizeof(mem_thread_counts[0]);

        /* 预热 */
        run_mem_bw_benchmark(1, mem_buffer);

        for (int t = 0; t < num_mem_thread_counts; t++) {
            int threads = mem_thread_counts[t];
            if (threads > max_threads) continue;

            double time_ms = run_mem_bw_benchmark(threads, mem_buffer);

            /* 计算实际带宽: size * threads / time */
            double bytes_per_sec = (MEM_TEST_SIZE * threads) / (time_ms / 1000.0);
            double bw_gbs = bytes_per_sec / (1024.0 * 1024.0 * 1024.0);

            double saturation = (bw_gbs / theoretical_bw) * 100.0;
            if (saturation > 100.0) saturation = 100.0;

            printf("%-10d | %-12.2f | %-12.1f | %-15.1f%% | %-12.1f%%\n",
                   threads, bw_gbs, theoretical_bw, saturation,
                   (bw_gbs / theoretical_bw / threads) * 100.0);
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
            double single_time = single_core_times[i];

            for (int t = 0; t < num_thread_counts; t++) {
                int threads = thread_counts[t];
                if (threads > max_threads) continue;

                double multi_time = run_multi_core_benchmark(tests[i].thread_func, threads, 0);
                double speedup = single_time / multi_time;
                double eff = speedup / threads * 100.0;

                const char *status = (eff < 50.0) ? "BANDWIDTH LIMIT" :
                                     (eff < 80.0) ? "scalability" : "optimal";

                report_write(report, "| %s | %s | %d | %.2f | %.2fx | %.1f%% | %s |\n",
                           tests[i].name, cat, threads, multi_time, speedup, eff, status);
            }
            report_write(report, "\n");
        }

        report_section(report, "Notes");
        report_write(report, "- Speedup = Single-core time / Multi-core time\n");
        report_write(report, "- Efficiency = Speedup / Threads * 100%%\n");
        report_write(report, "- Efficiency < 50%% indicates memory bandwidth saturation\n");
        report_write(report, "- Efficiency 50-80%% indicates some scalability limits\n");
        report_write(report, "- Efficiency > 80%% indicates near-optimal scaling\n\n");

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }
}

int main(int argc, char *argv[]) {
    request_sudo_password();
    initialize_cache_config();
    initialize_system_config();
    pmu_init_cache_counters();
    print_system_info();
    run_cpu_multi_core_test();
    return 0;
}
