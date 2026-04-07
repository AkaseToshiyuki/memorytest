/**
 * Cache Hierarchy and Memory Access Test
 *
 * 正确的测试方法:
 * - 延迟: 单线程随机访问,强制缓存未命中
 * - 缓存带宽: 单线程顺序访问 (L1/L2/L3是per-core)
 * - 内存带宽: 多线程顺序访问 (饱和4通道)
 */

#include "common.h"
#include <stdlib.h>
#include <string.h>
#include <pthread.h>

#define MAX_THREADS 64

typedef struct {
    int thread_id;
    int num_threads;
    void *ptr;
    size_t size;
    int iterations;
    double result;
} ThreadArgs;

static __thread double thread_sum;

/* ========== 单线程延迟测量 ========== */
static double measure_latency(void *ptr, size_t size, int samples) {
    volatile uint64_t *p = (volatile uint64_t *)ptr;
    size_t words = size / sizeof(uint64_t);

    uint64_t total = 0;
    int count = 0;

    /* 预热 */
    for (size_t i = 0; i < words && i < 1000; i += 16) {
        volatile uint64_t val = p[i];
        (void)val;
    }

    /* 测量 */
    for (int i = 0; i < samples; i++) {
        size_t idx = (i * 17) % words;
        uint64_t start = get_time_ns();
        volatile uint64_t val = p[idx];
        uint64_t end = get_time_ns();
        uint64_t lat = end - start;
        if (lat > 0 && lat < 10000) {
            total += lat;
            count++;
        }
        (void)val;
    }

    return (count > 0) ? (double)total / count : 0;
}

/* ========== 单线程顺序读带宽 ========== */
static void *single_seq_read(void *arg) {
    ThreadArgs *args = (ThreadArgs *)arg;
    volatile uint64_t *p = (volatile uint64_t *)args->ptr;
    size_t words = args->size / sizeof(uint64_t);

    uint64_t start = get_time_ns();
    thread_sum = 0;

    for (int iter = 0; iter < args->iterations; iter++) {
        for (size_t i = 0; i < words; i++) {
            thread_sum += p[i];
        }
    }

    uint64_t end = get_time_ns();
    uint64_t total = (uint64_t)args->iterations * args->size;
    args->result = ((double)total / (1024 * 1024)) / ((double)(end - start) / NS_PER_SEC);
    (void)thread_sum;

    return NULL;
}

/* ========== 多线程顺序读带宽 ========== */
static void *multi_seq_read(void *arg) {
    ThreadArgs *args = (ThreadArgs *)arg;
    size_t per_thread = args->size / args->num_threads;
    volatile uint64_t *p = (volatile uint64_t *)((char *)args->ptr + args->thread_id * per_thread);
    size_t words = per_thread / sizeof(uint64_t);

    uint64_t start = get_time_ns();
    thread_sum = 0;

    for (int iter = 0; iter < args->iterations; iter++) {
        for (size_t i = 0; i < words; i++) {
            thread_sum += p[i];
        }
    }

    uint64_t end = get_time_ns();
    uint64_t total = (uint64_t)args->iterations * per_thread;
    args->result = ((double)total / (1024 * 1024)) / ((double)(end - start) / NS_PER_SEC);
    (void)thread_sum;

    return NULL;
}

/* ========== 多线程顺序写带宽 ========== */
static void *multi_seq_write(void *arg) {
    ThreadArgs *args = (ThreadArgs *)arg;
    size_t per_thread = args->size / args->num_threads;
    volatile uint64_t *p = (volatile uint64_t *)((char *)args->ptr + args->thread_id * per_thread);
    size_t words = per_thread / sizeof(uint64_t);

    /* 预热 */
    for (size_t i = 0; i < words; i++) {
        p[i] = i;
    }

    uint64_t start = get_time_ns();

    for (int iter = 0; iter < args->iterations; iter++) {
        for (size_t i = 0; i < words; i += 16) {
            p[i] = iter + i;
        }
    }

    uint64_t end = get_time_ns();
    uint64_t total = (uint64_t)args->iterations * (words / 16) * sizeof(uint64_t);
    args->result = ((double)total / (1024 * 1024)) / ((double)(end - start) / NS_PER_SEC);

    return NULL;
}

/* ========== 多线程复制带宽 ========== */
typedef struct {
    int thread_id;
    int num_threads;
    void *ptr;          /* 源缓冲区起始 */
    size_t size;        /* 每个线程处理的大小 */
    void *dst;          /* 目标缓冲区起始 */
    int iterations;
    double result;
} CopyArgs;

static void *multi_seq_copy(void *arg) {
    CopyArgs *args = (CopyArgs *)arg;
    size_t per_thread = args->size / args->num_threads;
    volatile uint64_t *src = (volatile uint64_t *)((char *)args->ptr + args->thread_id * per_thread);
    volatile uint64_t *dst = (volatile uint64_t *)((char *)args->dst + args->thread_id * per_thread);
    size_t words = per_thread / sizeof(uint64_t);

    /* 预热 */
    for (size_t i = 0; i < words; i++) {
        dst[i] = src[i];
    }

    uint64_t start = get_time_ns();

    for (int iter = 0; iter < args->iterations; iter++) {
        for (size_t i = 0; i < words; i++) {
            dst[i] = src[i];
        }
    }

    uint64_t end = get_time_ns();
    /* 复制操作: 读+写 = 2x内存带宽 */
    uint64_t total = (uint64_t)args->iterations * per_thread * 2;
    args->result = ((double)total / (1024 * 1024)) / ((double)(end - start) / NS_PER_SEC);

    return NULL;
}

static double multi_copy_bandwidth(void *src, void *dst, size_t size, int threads, int iterations) {
    pthread_t tids[MAX_THREADS];
    CopyArgs args[MAX_THREADS];
    double results[MAX_THREADS];

    for (int i = 0; i < threads; i++) {
        args[i].thread_id = i;
        args[i].num_threads = threads;
        args[i].ptr = src;
        args[i].dst = dst;
        args[i].size = size;
        args[i].iterations = iterations;
        args[i].result = 0;
    }

    for (int i = 0; i < threads; i++) {
        pthread_create(&tids[i], NULL, multi_seq_copy, &args[i]);
    }
    for (int i = 0; i < threads; i++) {
        pthread_join(tids[i], NULL);
        results[i] = args[i].result;
    }

    double total = 0;
    for (int i = 0; i < threads; i++) {
        total += results[i];
    }

    return total;
}

/* 单线程带宽 */
static double single_bandwidth(void *ptr, size_t size, int iterations) {
    pthread_t tid;
    ThreadArgs args = {0, 1, ptr, size, iterations, 0};
    single_seq_read(&args);
    pthread_create(&tid, NULL, single_seq_read, &args);
    pthread_join(tid, NULL);
    return args.result;
}

/* 多线程带宽 */
static double multi_bandwidth(void *ptr, size_t size, int threads, int iterations, void *(*func)(void *)) {
    pthread_t tids[MAX_THREADS];
    ThreadArgs args[MAX_THREADS];
    double results[MAX_THREADS];

    for (int i = 0; i < threads; i++) {
        args[i].thread_id = i;
        args[i].num_threads = threads;
        args[i].ptr = ptr;
        args[i].size = size;
        args[i].iterations = iterations;
        args[i].result = 0;
    }

    for (int i = 0; i < threads; i++) {
        pthread_create(&tids[i], NULL, func, &args[i]);
    }
    for (int i = 0; i < threads; i++) {
        pthread_join(tids[i], NULL);
        results[i] = args[i].result;
    }

    double total = 0;
    for (int i = 0; i < threads; i++) {
        total += results[i];
    }

    return total;
}

void run_cache_hierarchy_test(void) {
    print_header("CACHE HIERARCHY TEST");

    int threads = 8;
    long num_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    if (threads > num_cpus) threads = num_cpus;

    printf("Test Configuration:\n");
    printf("  Threads: %d\n", threads);
    printf("  Memory: 4-channel LPDDR5 6000MT/s (理论: 192 GB/s)\n\n");

    /* ========== 缓存层级扫描 ========== */
    printf("=== 缓存层级扫描 ===\n\n");

    struct {
        size_t size;
        const char *name;
        const char *expected;
    } tests[] = {
        { 4 * KB,     "4KB",      "L1" },
        { 16 * KB,    "16KB",     "L1" },
        { 64 * KB,   "64KB",     "L1" },
        { 256 * KB,  "256KB",    "L2" },
        { 1 * MB,    "1MB",      "L2/L3" },
        { 4 * MB,    "4MB",      "L3" },
        { 16 * MB,   "16MB",     "RAM" },
        { 64 * MB,   "64MB",     "RAM" },
        { 128 * MB,  "128MB",    "RAM" },
    };

    int num_tests = sizeof(tests) / sizeof(tests[0]);

    printf("%-12s | %-10s | %-12s | %-15s | %s\n",
           "Size", "Lat(ns)", "BW(MB/s)", "Expected", "Analysis");
    printf("%-12s | %-10s | %-12s | %-15s | %s\n",
           "------------", "----------", "------------", "---------------", "------");

    srand(42);
    double prev_lat = 0;

    for (int t = 0; t < num_tests; t++) {
        size_t size = tests[t].size;
        const char *name = tests[t].name;

        void *ptr = malloc(size);
        if (!ptr) continue;
        memset(ptr, 0, size);

        /* 单线程延迟 - 强制缓存未命中 */
        int lat_iter = (size < 1 * MB) ? 5000 : (size < 16 * MB) ? 2000 : 500;
        double lat = measure_latency(ptr, size, lat_iter);

        /* 单线程带宽 - 缓存测试用单线程,内存测试用多线程 */
        int bw_iter = (size < 1 * MB) ? 1000 : (size < 16 * MB) ? 500 : 100;
        double bw;
        if (size <= 4 * MB) {
            /* 缓存层级: 单线程 */
            bw = single_bandwidth(ptr, size, bw_iter);
        } else {
            /* 内存层级: 多线程 */
            bw = multi_bandwidth(ptr, size, threads, bw_iter, multi_seq_read);
        }

        const char *analysis;
        if (t == 0) {
            analysis = "baseline";
            prev_lat = lat;
        } else {
            double ratio = lat / prev_lat;
            if (ratio > 2.0) analysis = "LEVEL UP";
            else if (ratio > 1.3) analysis = "transition";
            else analysis = "stable";
            prev_lat = lat;
        }

        printf("%-12s | %-10.2f | %-12.2f | %-15s | %s\n",
               name, lat, bw, tests[t].expected, analysis);
        fflush(stdout);

        free(ptr);
    }

    printf("\n");

    /* ========== 访问模式对比 ========== */
    printf("=== 访问模式对比 (顺序 vs 随机) ===\n\n");

    size_t pattern_sizes[] = {8 * KB, 64 * KB, 256 * KB};
    const char *pattern_names[] = {"8KB", "64KB", "256KB"};
    int num_patterns = sizeof(pattern_sizes) / sizeof(pattern_sizes[0]);

    printf("%-10s | %-12s | %-12s | %s\n",
           "Size", "Seq Lat(ns)", "Rnd Lat(ns)", "Ratio");
    printf("%-10s | %-12s | %-12s | %s\n",
           "--------", "------------", "------------", "------");

    for (int i = 0; i < num_patterns; i++) {
        size_t size = pattern_sizes[i];
        void *ptr = malloc(size);
        if (!ptr) continue;
        memset(ptr, 0, size);

        /* 顺序延迟: 顺序读整个buffer */
        uint64_t start = get_time_ns();
        volatile uint64_t sum = 0;
        size_t words = size / sizeof(uint64_t);
        for (size_t j = 0; j < words; j++) {
            sum += ((volatile uint64_t *)ptr)[j];
        }
        uint64_t end = get_time_ns();
        double seq_lat = (double)(end - start) / words;

        /* 随机延迟 */
        double rnd_lat = measure_latency(ptr, size, 5000);

        double ratio = (seq_lat > 0) ? rnd_lat / seq_lat : 0;

        printf("%-10s | %-12.2f | %-12.2f | %.2fx\n",
               pattern_names[i], seq_lat, rnd_lat, ratio);
        fflush(stdout);

        free(ptr);
    }

    printf("\n说明: Ratio = 随机延迟 / 顺序延迟\n\n");

    /* ========== 多通道内存带宽 ========== */
    printf("=== 4通道内存带宽 (多线程) ===\n\n");

    size_t bw_sizes[] = {16 * MB, 64 * MB, 256 * MB};
    const char *bw_names[] = {"16MB", "64MB", "256MB"};
    int num_bw = sizeof(bw_sizes) / sizeof(bw_sizes[0]);

    printf("%-10s | %-14s | %-14s | %-14s\n",
           "Size", "Read(MB/s)", "Write(MB/s)", "Copy(MB/s)");
    printf("%-10s | %-14s | %-14s | %-14s\n",
           "--------", "--------------", "--------------", "--------------");

    for (int i = 0; i < num_bw; i++) {
        size_t size = bw_sizes[i];
        void *ptr = malloc(size);
        void *ptr2 = malloc(size);
        if (!ptr || !ptr2) {
            free(ptr);
            free(ptr2);
            continue;
        }
        memset(ptr, 0, size);
        memset(ptr2, 0, size);

        double read_bw = multi_bandwidth(ptr, size, threads, 100, multi_seq_read);
        double write_bw = multi_bandwidth(ptr, size, threads, 100, multi_seq_write);
        double copy_bw = multi_copy_bandwidth(ptr, ptr2, size, threads, 100);

        printf("%-10s | %-14.2f | %-14.2f | %-14.2f\n",
               bw_names[i], read_bw, write_bw, copy_bw);
        fflush(stdout);

        free(ptr);
        free(ptr2);
    }

    printf("\n理论峰值: 192 GB/s (4通道 x 6000MT/s x 8bytes)\n\n");

    /* ========== 生成报告 ========== */
    ReportContext *report = report_init("cache_hierarchy", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);

        report_section(report, "Cache Hierarchy Scan");
        report_write(report, "| Size | Lat(ns) | BW(MB/s) | Expected | Analysis |\n");
        report_write(report, "|------|----------|-----------|----------|----------|\n");

        srand(42);
        prev_lat = 0;
        for (int t = 0; t < num_tests; t++) {
            size_t size = tests[t].size;
            const char *name = tests[t].name;
            void *ptr = malloc(size);
            if (!ptr) continue;
            memset(ptr, 0, size);

            int lat_iter = (size < 1 * MB) ? 5000 : (size < 16 * MB) ? 2000 : 500;
            double lat = measure_latency(ptr, size, lat_iter);

            int bw_iter = (size < 1 * MB) ? 1000 : (size < 16 * MB) ? 500 : 100;
            double bw;
            if (size <= 4 * MB) {
                bw = single_bandwidth(ptr, size, bw_iter);
            } else {
                bw = multi_bandwidth(ptr, size, threads, bw_iter, multi_seq_read);
            }

            const char *analysis = "";
            if (t == 0) {
                analysis = "baseline";
            } else {
                double ratio = lat / prev_lat;
                if (ratio > 2.0) analysis = "LEVEL UP";
                else if (ratio > 1.3) analysis = "transition";
                else analysis = "stable";
            }
            prev_lat = lat;

            report_write(report, "| %s | %.2f | %.2f | %s | %s |\n",
                        name, lat, bw, tests[t].expected, analysis);
            free(ptr);
        }

        report_section(report, "Multi-channel Memory Bandwidth");
        report_write(report, "| Size | Read(MB/s) | Write(MB/s) | Copy(MB/s) |\n");
        report_write(report, "|------|-------------|---------------|------------|\n");

        for (int i = 0; i < num_bw; i++) {
            size_t size = bw_sizes[i];
            void *ptr = malloc(size);
            void *ptr2 = malloc(size);
            if (!ptr || !ptr2) {
                free(ptr);
                free(ptr2);
                continue;
            }
            memset(ptr, 0, size);
            memset(ptr2, 0, size);

            double read_bw = multi_bandwidth(ptr, size, threads, 100, multi_seq_read);
            double write_bw = multi_bandwidth(ptr, size, threads, 100, multi_seq_write);
            double copy_bw = multi_copy_bandwidth(ptr, ptr2, size, threads, 100);

            report_write(report, "| %s | %.2f | %.2f | %.2f |\n",
                        bw_names[i], read_bw, write_bw, copy_bw);
            free(ptr);
            free(ptr2);
        }

        report_section(report, "Notes");
        report_write(report, "- Cache bandwidth: single-threaded\n");
        report_write(report, "- Memory bandwidth: multi-threaded (%d threads)\n", threads);
        report_write(report, "- Theoretical peak: 192 GB/s\n\n");

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }
}

int main(int argc, char *argv[]) {
    initialize_cache_config();
    print_system_info();
    run_cache_hierarchy_test();
    return 0;
}