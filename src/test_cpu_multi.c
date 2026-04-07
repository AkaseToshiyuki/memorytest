/**
 * CPU Multi-Core Performance Test
 *
 * 测试多核心并行性能:
 * - 所有核心同时运行
 * - 计算总吞吐量和并行效率
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

/* ========== 线程参数 ========== */
typedef struct {
    int thread_id;
    int num_threads;
    double result;
} ThreadArgs;

/* ========== 整数运算线程 ========== */
static void *thread_alu_add(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    uint64_t per_thread = ITERATIONS / t->num_threads;
    uint64_t start = t->thread_id * per_thread;
    uint64_t end = start + per_thread;

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

    uint64_t product = 1;
    for (uint64_t i = start + 1; i < end; i++) {
        product = (product * 3 + 7) % 1000000007;
    }
    t->result = (double)product;
    return NULL;
}

/* ========== 浮点运算线程 ========== */
static void *thread_float_mul(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    double per_thread = (double)ITERATIONS / t->num_threads;
    double start = t->thread_id * per_thread;
    double end = start + per_thread;

    double result = 1.0;
    for (double i = start + 1; i < end; i++) {
        result *= i * 0.00001;
        if (result < 0.0001) result = 1.0;
    }
    t->result = result;
    return NULL;
}

static void *thread_float_sqrt(void *arg) {
    ThreadArgs *t = (ThreadArgs *)arg;
    double per_thread = (double)ITERATIONS / t->num_threads;
    double start = t->thread_id * per_thread;
    double end = start + per_thread;

    double result = 0.0;
    for (double i = start + 1; i < end; i++) {
        result = sqrt(i);
    }
    t->result = result;
    return NULL;
}

/* ========== 绑定到指定核心 ========== */
static void bind_to_core(int core_id) {
#if defined(__linux__)
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(core_id, &cpuset);
    pthread_setaffinity_np(pthread_self(), sizeof(cpuset), &cpuset);
#endif
}

/* ========== 多核测试 ========== */
typedef struct {
    const char *name;
    const char *category;
    void *(*thread_func)(void *);
    int threads;
    double single_core_time_ms;
} MultiCoreTest;

static MultiCoreTest tests[] = {
    {"Add", "ALU", thread_alu_add, 0, 0},
    {"Mul", "ALU", thread_alu_mul, 0, 0},
    {"double Mul", "FPU", thread_float_mul, 0, 0},
    {"double Sqrt", "FPU", thread_float_sqrt, 0, 0},
};

static uint64_t run_single_core_benchmark(void *(*func)(void *), ThreadArgs *args) {
    uint64_t start = get_time_ns();
    func(args);
    uint64_t end = get_time_ns();
    return end - start;
}

static double run_multi_core_benchmark(void *(*func)(void *), int num_threads) {
    pthread_t tids[MAX_THREADS];
    ThreadArgs args[MAX_THREADS];

    for (int i = 0; i < num_threads; i++) {
        args[i].thread_id = i;
        args[i].num_threads = num_threads;
        args[i].result = 0;
    }

    uint64_t start = get_time_ns();

    for (int i = 0; i < num_threads; i++) {
        pthread_create(&tids[i], NULL, func, &args[i]);
    }

    for (int i = 0; i < num_threads; i++) {
        pthread_join(tids[i], NULL);
    }

    uint64_t end = get_time_ns();
    return (double)(end - start) / 1000000.0;  /* ms */
}

void run_cpu_multi_core_test(void) {
    print_header("CPU MULTI-CORE PERFORMANCE TEST");

    long num_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    int max_threads = (int)num_cpus;

    printf("Available CPU cores: %ld\n", num_cpus);
    printf("Iterations: %d per core\n\n", ITERATIONS);

    int num_tests = sizeof(tests) / sizeof(tests[0]);

    /* 预热单核 */
    printf("Warming up (single core)...\n");
    for (int i = 0; i < num_tests; i++) {
        ThreadArgs args = {0, 1, 0};
        for (int w = 0; w < 3; w++) {
            tests[i].thread_func(&args);
        }
    }

    /* 测试不同线程数 */
    int thread_counts[] = {1, 2, 4, 8, max_threads};
    int num_thread_counts = sizeof(thread_counts) / sizeof(thread_counts[0]);

    printf("\n=== Multi-Core Performance ===\n\n");

    printf("%-14s | %-8s | %-10s | %-12s | %-10s | %-10s\n",
           "Operation", "Category", "Threads", "Time(ms)", "Speedup", "Efficiency");
    printf("%-14s | %-8s | %-10s | %-12s | %-10s | %-10s\n",
           "--------------", "--------", "----------", "------------", "----------", "----------");

    for (int i = 0; i < num_tests; i++) {
        const char *cat = tests[i].category;
        const char *current_cat = "";

        /* 先测试单核时间 */
        double single_time = run_multi_core_benchmark(tests[i].thread_func, 1);
        tests[i].single_core_time_ms = single_time;

        printf("\n-- %s --\n", cat);

        for (int t = 0; t < num_thread_counts; t++) {
            int threads = thread_counts[t];
            if (threads > max_threads) continue;

            /* 多核运行 */
            double multi_time = run_multi_core_benchmark(tests[i].thread_func, threads);

            double speedup = single_time / multi_time;
            double efficiency = speedup / threads * 100.0;

            printf("%-14s | %-8s | %-10d | %-12.2f | %-10.2fx | %-10.1f%%\n",
                   tests[i].name, cat, threads, multi_time, speedup, efficiency);
        }
    }

    /* 生成报告 */
    ReportContext *report = report_init("cpu_multi_core", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "Multi-Core Performance");

        report_write(report, "| Operation | Category | Threads | Time(ms) | Speedup | Efficiency |\n");
        report_write(report, "|----------|----------|---------|----------|---------|------------|\n");

        for (int i = 0; i < num_tests; i++) {
            const char *cat = tests[i].category;
            double single_time = tests[i].single_core_time_ms;

            for (int t = 0; t < num_thread_counts; t++) {
                int threads = thread_counts[t];
                if (threads > max_threads) continue;

                double multi_time = run_multi_core_benchmark(tests[i].thread_func, threads);
                double speedup = single_time / multi_time;
                double efficiency = speedup / threads * 100.0;

                report_write(report, "| %s | %s | %d | %.2f | %.2fx | %.1f%% |\n",
                           tests[i].name, cat, threads, multi_time, speedup, efficiency);
            }
            report_write(report, "\n");
        }

        report_section(report, "Notes");
        report_write(report, "- Available CPU cores: %ld\n", num_cpus);
        report_write(report, "- Speedup = Single-core time / Multi-core time\n");
        report_write(report, "- Efficiency = Speedup / Threads * 100%%\n\n");

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }
}

int main(int argc, char *argv[]) {
    initialize_cache_config();
    print_system_info();
    run_cpu_multi_core_test();
    return 0;
}
