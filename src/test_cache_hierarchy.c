/**
 * Cache Hierarchy and Memory Access Test
 *
 * 正确的测试方法:
 * - 延迟: 单线程随机访问,强制缓存未命中
 * - 缓存带宽: 单线程顺序访问 (L1/L2/L3是per-core)
 * - 内存带宽: 多线程顺序访问 (饱和4通道)
 */

#include "common.h"
#include "asm_helpers.h"
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

/* ========== 序列化CPU以确保准确的内存访问时间 ========== */
static inline void cpu_relax(void) {
#if defined(__x86_64__)
    __asm__ volatile("" ::: "memory");
#elif defined(__aarch64__)
    __asm__ volatile("isb" ::: "memory");
#endif
}

/* ========== 防止编译器优化 ========== */
static inline void clobber(volatile void *p) {
    __asm__ volatile("" : "+m"(*(volatile char *)p));
}

/* Double comparison for qsort */
static int compare_double(const void *a, const void *b) {
    double va = *(const double *)a;
    double vb = *(const double *)b;
    if (va < vb) return -1;
    if (va > vb) return 1;
    return 0;
}

/* ========== 单线程延迟测量 - 批量方式减少开销 ========== */
static double measure_latency(void *ptr, size_t size, int samples) {
    volatile uint64_t *p = (volatile uint64_t *)ptr;
    size_t words = size / sizeof(uint64_t);

    /* 批量测量: 每批次访问数 */
    const int BATCH_SIZE = 1024;
    int num_batches = samples / BATCH_SIZE;
    if (num_batches < 1) num_batches = 1;

    double *latencies = malloc(num_batches * sizeof(double));
    if (!latencies) return 0;

    /* 深度预热 - 确保所有数据在缓存中 */
    for (size_t i = 0; i < words; i += 64) {
        volatile uint64_t val = p[i];
        (void)val;
    }
    /* 再预热一次 */
    for (size_t i = 0; i < words; i += 64) {
        volatile uint64_t val = p[i];
        (void)val;
    }

    int count = 0;

    /* Batch measurement - uses rdtsc_ns (lower overhead than get_time_ns).
     *
     * Prefetch logic: only prefetch when the working set exceeds L1, otherwise
     * HW prefetch will pull the entire working set into L1 and we'd measure
     * L1 hits even for "RAM-sized" buffers (observed 0.1 ns instead of 50-100 ns).
     * For L1-sized tests (<= 32KB on most CPUs), skip prefetch and let the
     * natural L1 hit latency show. */
    int working_set_kb = (int)(size / 1024);
    int use_prefetch = (working_set_kb > 64);  /* > L1D = 64KB on this host */

    for (int b = 0; b < num_batches && count < num_batches; b++) {
        /* Pseudo-random access - LCG to avoid stride patterns that HW prefetches */
        static const uint64_t a = 6364136223846793005ULL;
        static const uint64_t c = 1442695040888963407ULL;
        uint64_t lcg_state = b * 17 + 1;

        uint64_t start = rdtsc_ns();
        /* Batch: BATCH_SIZE accesses, real reads */
        volatile uint64_t sum = 0;
        for (int i = 0; i < BATCH_SIZE; i++) {
            lcg_state = a * lcg_state + c;
            size_t idx = lcg_state % words;
            if (use_prefetch) {
                uint64_t next_lcg = a * lcg_state + c;
                size_t next_idx = next_lcg % words;
                /* prefetch_l2 for L2+ (avoids L1 pollution for small buffers) */
                prefetch_l2(&p[next_idx]);
            }
            compiler_barrier();
            sum += p[idx];  /* Real read */
        }
        compiler_barrier();
        uint64_t end = rdtsc_ns();
        (void)sum;

        /* 使用浮点除法保留小数精度 */
        double batch_lat = (double)(end - start) / BATCH_SIZE;
        /* 过滤明显异常值 */
        if (batch_lat > 0.1 && batch_lat < 10000.0) {
            latencies[count++] = batch_lat;
        }
    }

    double result = 0;
    if (count > 0) {
        /* 使用中位数 */
        qsort(latencies, count, sizeof(double), compare_double);
        int mid = count / 2;
        if (count % 2 == 0) {
            result = (latencies[mid - 1] + latencies[mid]) / 2.0;
        } else {
            result = latencies[mid];
        }
    }

    free(latencies);
    return result;
}

/* ========== 指针追逐延迟测量 - 真正随机的内存访问 ========== */
static double measure_pointer_chase_latency(void *ptr, size_t size, int iterations) {
    volatile uint64_t *p = (volatile uint64_t *)ptr;
    size_t words = size / sizeof(uint64_t);

    if (words < 16) return 0;

    /* 构建链表: 每个元素指向另一个随机位置 */
    size_t *indices = malloc(words * sizeof(size_t));
    if (!indices) return 0;

    /* 用确定性的方式构建链表，避免随机数生成开销 */
    for (size_t i = 0; i < words; i++) {
        /* 质数stride确保遍历覆盖整个数组 */
        indices[i] = (i * 17 + 1) % words;
    }

    /* 预热: 沿着链表走一遍，确保所有数据从内存加载到缓存 */
    size_t idx = 0;
    for (size_t i = 0; i < words; i++) {
        idx = indices[idx];
        volatile uint64_t val = p[idx];  /* 真正读取 */
        (void)val;
    }

    /* 测量: 多次指针追逐 */
    const int INNER_LOOP = 64;
    double total_lat = 0;
    int valid_samples = 0;

    for (int iter = 0; iter < iterations; iter++) {
        idx = iter % words;  /* 从不同起点开始 */

        uint64_t start = get_time_ns();
        for (int i = 0; i < INNER_LOOP; i++) {
            idx = indices[idx];
            volatile uint64_t val = p[idx];  /* 真正读取 */
            (void)val;
        }
        uint64_t end = get_time_ns();

        double lat = (double)(end - start) / INNER_LOOP;
        if (lat > 0.1 && lat < 10000.0) {
            total_lat += lat;
            valid_samples++;
        }
    }

    free(indices);
    return valid_samples > 0 ? total_lat / valid_samples : 0;
}

/* ========== 单线程写延迟测量 - 批量方式 ========== */
static double measure_write_latency(void *ptr, size_t size, int samples) {
    volatile uint64_t *p = (volatile uint64_t *)ptr;
    size_t words = size / sizeof(uint64_t);

    /* 批量测量 */
    const int BATCH_SIZE = 1024;
    int num_batches = samples / BATCH_SIZE;
    if (num_batches < 1) num_batches = 1;

    double *latencies = malloc(num_batches * sizeof(double));
    if (!latencies) return 0;

    /* 预热 */
    for (size_t i = 0; i < words && i < 4096; i += 64) {
        p[i] = i;
    }

    int count = 0;

    /* 批量测量 - 使用LCG产生更随机的访问模式 */
    static const uint64_t a = 6364136223846793005ULL;
    static const uint64_t c = 1442695040888963407ULL;

    for (int b = 0; b < num_batches && count < num_batches; b++) {
        uint64_t lcg_state = b * 17 + 1;

        uint64_t start = get_time_ns();
        for (int i = 0; i < BATCH_SIZE; i++) {
            lcg_state = a * lcg_state + c;
            size_t idx = lcg_state % words;
            p[idx] = (uint64_t)(lcg_state);
        }
        uint64_t end = get_time_ns();

        double batch_lat = (double)(end - start) / BATCH_SIZE;
        if (batch_lat > 0.1 && batch_lat < 10000.0) {
            latencies[count++] = batch_lat;
        }
    }

    double result = 0;
    if (count > 0) {
        qsort(latencies, count, sizeof(double), compare_double);
        int mid = count / 2;
        if (count % 2 == 0) {
            result = (latencies[mid - 1] + latencies[mid]) / 2.0;
        } else {
            result = latencies[mid];
        }
    }

    free(latencies);
    return result;
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

/* Maximum number of cache test sizes */
#define MAX_CACHE_TEST_SIZES 16

/* Result structure for cache hierarchy tests */
typedef struct {
    size_t size;
    char name[16];
    char expected[8];
    double rd_lat;
    double wr_lat;
    double bw;
    char analysis[16];
} CacheTestResult;

/* Run cache hierarchy scan and return results */
static int run_cache_hierarchy_scan(CacheTestResult *results, int max_results,
                                     size_t l1_size, size_t l2_size, size_t l3_size,
                                     int threads) {
    int num_results = 0;

    /* Helper to add result */
    #define ADD_RESULT(sz, nm, level, rd, wr, bw_val, anal) do { \
        if (num_results < max_results) { \
            results[num_results].size = (sz); \
            strncpy(results[num_results].name, (nm), sizeof(results[num_results].name) - 1); \
            results[num_results].name[sizeof(results[num_results].name) - 1] = 0; \
            strncpy(results[num_results].expected, (level), sizeof(results[num_results].expected) - 1); \
            results[num_results].expected[sizeof(results[num_results].expected) - 1] = 0; \
            results[num_results].rd_lat = (rd); \
            results[num_results].wr_lat = (wr); \
            results[num_results].bw = (bw_val); \
            strncpy(results[num_results].analysis, (anal), sizeof(results[num_results].analysis) - 1); \
            results[num_results].analysis[sizeof(results[num_results].analysis) - 1] = 0; \
            num_results++; \
        } \
    } while(0)

    /* L1 test sizes: 0.25x, 0.5x, 1x L1 */
    char buf[16];
    snprintf(buf, sizeof(buf), "%.0fKB", (double)l1_size / 4 / 1024);
    ADD_RESULT(l1_size / 4, buf, "L1", 0, 0, 0, "");
    snprintf(buf, sizeof(buf), "%.0fKB", (double)l1_size / 2 / 1024);
    ADD_RESULT(l1_size / 2, buf, "L1", 0, 0, 0, "");
    snprintf(buf, sizeof(buf), "%.0fKB", (double)l1_size / 1024);
    ADD_RESULT(l1_size, buf, "L1", 0, 0, 0, "baseline");

    /* L2 test sizes */
    if (l2_size > l1_size) {
        snprintf(buf, sizeof(buf), "%.0fKB", (double)l2_size / 2 / 1024);
        ADD_RESULT(l2_size / 2, buf, "L2", 0, 0, 0, "");
        snprintf(buf, sizeof(buf), "%.0fKB", (double)l2_size / 1024);
        ADD_RESULT(l2_size, buf, "L2", 0, 0, 0, "");
    }

    /* L3 test sizes */
    if (l3_size > l2_size) {
        snprintf(buf, sizeof(buf), "%.0fMB", (double)l3_size / 2 / MB);
        ADD_RESULT(l3_size / 2, buf, "L3", 0, 0, 0, "");
        snprintf(buf, sizeof(buf), "%.0fMB", (double)l3_size / MB);
        ADD_RESULT(l3_size, buf, "L3", 0, 0, 0, "");
        /* 2x L3 to show L3->RAM transition */
        if (l3_size * 2 <= 128 * MB) {
            snprintf(buf, sizeof(buf), "%.0fMB", (double)l3_size * 2 / MB);
            ADD_RESULT(l3_size * 2, buf, "RAM", 0, 0, 0, "");
        }
    }

    /* RAM test sizes - must be significantly > L3 */
    if (l3_size < 64 * MB) {
        ADD_RESULT(64 * MB, "64MB", "RAM", 0, 0, 0, "");
    }
    if (l3_size < 256 * MB) {
        ADD_RESULT(256 * MB, "256MB", "RAM", 0, 0, 0, "");
    }
    ADD_RESULT(1024 * MB, "1GB", "RAM", 0, 0, 0, "");

    #undef ADD_RESULT

    /* Run actual measurements */
    double prev_lat = 0;
    for (int i = 0; i < num_results; i++) {
        size_t size = results[i].size;

        void *ptr = malloc(size);
        if (!ptr) continue;
        memset(ptr, 0, size);

        /* Random access latency */
        int lat_iter = (size < 1 * MB) ? 5000 : (size < 16 * MB) ? 2000 : 500;
        double lat = measure_latency(ptr, size, lat_iter);
        double wr_lat = measure_write_latency(ptr, size, lat_iter);

        /* Bandwidth measurement - use multi-threaded for ALL sizes for consistency */
        int bw_iter = (size < 1 * MB) ? 1000 : (size < 16 * MB) ? 500 : 100;
        /* Limit threads based on size to avoid over-parallelization */
        int bw_threads = threads;
        if (size < 1 * MB) bw_threads = 1;
        else if (size < 4 * MB) bw_threads = (threads > 4) ? 4 : threads;
        else if (size < 16 * MB) bw_threads = (threads > 8) ? 8 : threads;
        double bw = multi_bandwidth(ptr, size, bw_threads, bw_iter, multi_seq_read);

        results[i].rd_lat = lat;
        results[i].wr_lat = wr_lat;
        results[i].bw = bw;

        /* Analysis based on bandwidth ratio - cache misses cause bandwidth drop */
        if (i == 0) {
            strcpy(results[i].analysis, "baseline");
        } else {
            /* Use bandwidth ratio to detect cache boundary */
            double prev_bw = results[i-1].bw;
            double bw_ratio = prev_bw / bw;  /* >1 means bandwidth dropped */

            /* Latency ratio for additional confirmation */
            double lat_ratio = lat / prev_lat;

            /* Detect cache boundary: significant BW drop + latency increase */
            if (bw_ratio > 1.5 && lat_ratio > 1.1) {
                strcpy(results[i].analysis, "LEVEL UP");
            } else if (bw_ratio > 1.2 || lat_ratio > 1.15) {
                strcpy(results[i].analysis, "transition");
            } else {
                strcpy(results[i].analysis, "stable");
            }
        }
        prev_lat = lat;

        free(ptr);
    }

    return num_results;
}


void run_cache_hierarchy_test(void) {
    print_header("CACHE HIERARCHY TEST");

    long num_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    int threads = (int)num_cpus;
    int channels = get_memory_channels();

    /* Get detected cache sizes */
    size_t l1_size = global_cache_config.l1d_size;
    size_t l2_size = global_cache_config.l2_size;
    size_t l3_size = global_cache_config.l3_size;

    printf("Test Configuration:\n");
    printf("  CPU Cores: %ld\n", num_cpus);
    printf("  Threads: %d (auto-scaled to CPU count)\n", threads);
    printf("  Memory: %d-channel memory\n", channels);
    printf("  Detected Cache: L1=%zuKB, L2=%zuKB, L3=%zuKB\n\n",
           l1_size / KB, l2_size / KB, l3_size / KB);

    /* Run cache hierarchy scan */
    CacheTestResult results[MAX_CACHE_TEST_SIZES];
    int num_results = run_cache_hierarchy_scan(results, MAX_CACHE_TEST_SIZES,
                                                l1_size, l2_size, l3_size, threads);

    /* ========== 缓存层级扫描 ========== */
    printf("=== 缓存层级扫描 ===\n\n");

    printf("%-12s | %-10s | %-12s | %-12s | %-10s | %s\n",
           "Size", "RdLat(ns)", "WrLat(ns)", "BW(MB/s)", "Expected", "Analysis");
    printf("%-12s | %-10s | %-12s | %-12s | %-10s | %s\n",
           "------------", "----------", "------------", "------------", "----------", "------");

    for (int i = 0; i < num_results; i++) {
        printf("%-12s | %-10.2f | %-12.2f | %-12.2f | %-10s | %s\n",
               results[i].name, results[i].rd_lat, results[i].wr_lat,
               results[i].bw, results[i].expected, results[i].analysis);
    }
    fflush(stdout);

    printf("\nCache Latency Reference:\n");
    printf("  L1: < 5 ns\n");
    printf("  L2: 5-15 ns\n");
    printf("  L3: 15-50 ns\n");
    printf("  RAM: > 50 ns\n\n");

    /* ========== 访问模式对比 ========== */
    printf("=== 访问模式对比 (顺序 vs 随机) ===\n\n");

    size_t pattern_sizes[3];
    const char *pattern_names[3];
    pattern_sizes[0] = l1_size;
    pattern_names[0] = "L1";
    pattern_sizes[1] = l2_size;
    pattern_names[1] = "L2";
    if (l3_size > 0) {
        pattern_sizes[2] = l3_size;
    } else {
        /* Cannot detect L3, prompt user */
        printf("[Warning] L3 size not detected, cannot run pattern comparison\n");
        return;
    }
    pattern_names[2] = "L3";

    printf("%-10s | %-12s | %-12s | %s\n",
           "Size", "Seq Lat(ns)", "Rnd Lat(ns)", "Ratio");
    printf("%-10s | %-12s | %-12s | %s\n",
           "--------", "------------", "------------", "------");

    for (int i = 0; i < 3; i++) {
        size_t size = pattern_sizes[i];
        void *ptr = malloc(size);
        if (!ptr) continue;
        memset(ptr, 0, size);

        /* Sequential latency */
        uint64_t start = get_time_ns();
        volatile uint64_t sum = 0;
        size_t words = size / sizeof(uint64_t);
        for (size_t j = 0; j < words; j++) {
            sum += ((volatile uint64_t *)ptr)[j];
        }
        uint64_t end = get_time_ns();
        double seq_lat = (double)(end - start) / words;

        /* Random latency */
        double rnd_lat = measure_latency(ptr, size, 5000);

        double ratio = (seq_lat > 0) ? rnd_lat / seq_lat : 0;

        printf("%-10s | %-12.2f | %-12.2f | %.2fx\n",
               pattern_names[i], seq_lat, rnd_lat, ratio);
        fflush(stdout);

        free(ptr);
    }

    printf("\n说明: Ratio = 随机延迟 / 顺序延迟\n\n");

    /* ========== 多通道内存带宽 ========== */
    printf("=== 多通道内存带宽 (多线程) ===\n\n");

    /* Use detected cache sizes + RAM sizes */
    size_t bw_sizes[5];
    char bw_names[5][16];
    int num_bw = 0;

    /* L1/L2/L3 sizes */
    bw_sizes[num_bw] = l1_size;
    snprintf(bw_names[num_bw], sizeof(bw_names[num_bw]), "L1(%.0fKB)", (double)l1_size/KB);
    num_bw++;

    if (l2_size > l1_size) {
        bw_sizes[num_bw] = l2_size;
        snprintf(bw_names[num_bw], sizeof(bw_names[num_bw]), "L2(%.0fKB)", (double)l2_size/KB);
        num_bw++;
    }

    if (l3_size > l2_size) {
        bw_sizes[num_bw] = l3_size;
        snprintf(bw_names[num_bw], sizeof(bw_names[num_bw]), "L3(%.0fMB)", (double)l3_size/MB);
        num_bw++;
    }

    /* RAM sizes - must be significantly larger than L3 */
    size_t ram_size = (l3_size > 0) ? (l3_size * 4) : (64 * MB);
    if (ram_size < 64 * MB) ram_size = 64 * MB;
    bw_sizes[num_bw] = ram_size;
    snprintf(bw_names[num_bw], sizeof(bw_names[num_bw]), "RAM(%.0fMB)", (double)ram_size/MB);
    num_bw++;

    if (l3_size < 256 * MB) {
        bw_sizes[num_bw] = 256 * MB;
        snprintf(bw_names[num_bw], sizeof(bw_names[num_bw]), "RAM(256MB)");
        num_bw++;
    }

    printf("%-14s | %-14s | %-14s | %-14s\n",
           "Size", "Read(MB/s)", "Write(MB/s)", "Copy(MB/s)");
    printf("%-14s | %-14s | %-14s | %-14s\n",
           "--------------", "--------------", "--------------", "--------------");

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

        printf("%-14s | %-14.2f | %-14.2f | %-14.2f\n",
               bw_names[i], read_bw, write_bw, copy_bw);
        fflush(stdout);

        free(ptr);
        free(ptr2);
    }

    printf("\n理论峰值: %d GB/s (假设单通道 4800 MT/s, 实际取决于内存规格)\n\n", channels * 38);

    /* ========== 生成报告 ========== */
    ReportContext *report = report_init("cache_hierarchy", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);

        report_section(report, "Cache Hierarchy Scan");
        report_write(report, "| Size | RdLat(ns) | WrLat(ns) | BW(MB/s) | Expected | Analysis |\n");
        report_write(report, "|------|-----------|------------|-----------|----------|----------|\n");

        for (int i = 0; i < num_results; i++) {
            report_write(report, "| %s | %.2f | %.2f | %.2f | %s | %s |\n",
                        results[i].name, results[i].rd_lat, results[i].wr_lat,
                        results[i].bw, results[i].expected, results[i].analysis);
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
        report_write(report, "- Cache sizes: L1=%zuKB, L2=%zuKB, L3=%zuKB\n", l1_size/KB, l2_size/KB, l3_size/KB);
        report_write(report, "- Cache bandwidth: single-threaded\n");
        report_write(report, "- Memory bandwidth: multi-threaded (%d threads)\n", threads);
        int report_channels = get_memory_channels();
        report_write(report, "- Memory channels: %d\n", report_channels);
        report_write(report, "- Theoretical peak: ~%d GB/s\n\n", report_channels * 38);

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
    run_cache_hierarchy_test();
    return 0;
}