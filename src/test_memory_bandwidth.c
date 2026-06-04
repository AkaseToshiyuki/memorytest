/**
 * Memory Bandwidth Test - Multi-threaded Version
 *
 * 多通道内存带宽测试
 * 使用多线程并行访问内存,充分调动所有通道
 *
 * 理论带宽取决于检测到的内存通道数和内存速度
 */

#include "common.h"
#include "util.h"
#include <string.h>
#include <pthread.h>
#include <sys/mman.h>

/* MAX_THREADS: the per-stack-array limit for pthread_t, args, results.
 * Chosen large enough to cover current and foreseeable server CPUs
 * (256 cores covers Ampere Altra / EPYC 9004 / future Apple silicon).
 * The actual number of threads used is clamped to this in main() to
 * prevent stack buffer overflows on machines with more cores. */
#define MAX_THREADS 256

/* 线程参数 */
typedef struct {
    int thread_id;
    int num_threads;
    void *ptr;
    size_t size;
    int iterations;
    double *bandwidth;  /* 输出: 带宽 MB/s */
    double latency;    /* 输出: 延迟 ns */
} ThreadArgs;

/* 线程本地数据 */
static __thread double thread_result;
static __thread uint64_t thread_sum;

/* 顺序读线程函数 - stride=8 (64 bytes = 1 cache line) */
static void *read_thread_func(void *arg) {
    ThreadArgs *args = (ThreadArgs *)arg;
    size_t per_thread_size = args->size / args->num_threads;
    volatile uint64_t *p = (volatile uint64_t *)((char *)args->ptr + args->thread_id * per_thread_size);
    size_t words = per_thread_size / sizeof(uint64_t);

    uint64_t start = get_time_ns();
    thread_sum = 0;

    for (int iter = 0; iter < args->iterations; iter++) {
        for (size_t i = 0; i < words; i += 8) {
            thread_sum += p[i];
        }
    }

    uint64_t end = get_time_ns();
    uint64_t total_bytes = (uint64_t)args->iterations * (words / 8) * 64;
    *args->bandwidth = ((double)total_bytes / (1024 * 1024)) / ((double)(end - start) / NS_PER_SEC);
    (void)thread_sum;
    return NULL;
}

/* 顺序写线程函数 - stride=8 (64 bytes = 1 cache line) */
static void *write_thread_func(void *arg) {
    ThreadArgs *args = (ThreadArgs *)arg;
    size_t per_thread_size = args->size / args->num_threads;
    volatile uint64_t *p = (volatile uint64_t *)((char *)args->ptr + args->thread_id * per_thread_size);
    size_t words = per_thread_size / sizeof(uint64_t);

    uint64_t start = get_time_ns();

    for (int iter = 0; iter < args->iterations; iter++) {
        for (size_t i = 0; i < words; i += 8) {
            p[i] = iter + i;
        }
    }

    uint64_t end = get_time_ns();
    uint64_t total_bytes = (uint64_t)args->iterations * (words / 8) * 64;
    *args->bandwidth = ((double)total_bytes / (1024 * 1024)) / ((double)(end - start) / NS_PER_SEC);
    return NULL;
}

/* 复制线程函数 - stride=1 (full cache line) */
static void *copy_thread_func(void *arg) {
    ThreadArgs *args = (ThreadArgs *)arg;
    size_t per_thread_size = args->size / args->num_threads;
    volatile uint64_t *src = (volatile uint64_t *)((char *)args->ptr + args->thread_id * per_thread_size);
    volatile uint64_t *dst = (volatile uint64_t *)((char *)args->ptr + args->size + args->thread_id * per_thread_size);
    size_t words = per_thread_size / sizeof(uint64_t);

    uint64_t start = get_time_ns();
    thread_sum = 0;

    for (int iter = 0; iter < args->iterations; iter++) {
        for (size_t i = 0; i < words; i++) {
            dst[i] = src[i];
        }
    }

    uint64_t end = get_time_ns();
    uint64_t total_bytes = (uint64_t)args->iterations * words * 16;  /* read+write = 2x */
    *args->bandwidth = ((double)total_bytes / (1024 * 1024)) / ((double)(end - start) / NS_PER_SEC);
    (void)thread_sum;
    return NULL;
}

/* 随机读延迟线程 */
static void *random_read_latency_thread(void *arg) {
    ThreadArgs *args = (ThreadArgs *)arg;
    size_t per_thread_size = args->size / args->num_threads;
    volatile uint64_t *p = (volatile uint64_t *)((char *)args->ptr + args->thread_id * per_thread_size);
    size_t words = per_thread_size / sizeof(uint64_t);

    uint64_t total_lat = 0;
    int count = 0;

    for (int iter = 0; iter < args->iterations; iter++) {
        for (size_t i = 0; i < words; i += 16) {
            uint64_t start = get_time_ns();
            volatile uint64_t val = p[i];
            uint64_t end = get_time_ns();
            uint64_t lat = end - start;
            if (lat < 5000 && lat > 0) {
                total_lat += lat;
                count++;
            }
            (void)val;
        }
    }

    thread_result = (count > 0) ? (double)total_lat / count : 0;
    args->latency = thread_result;

    return NULL;
}

/* 随机写延迟线程 */
static void *random_write_latency_thread(void *arg) {
    ThreadArgs *args = (ThreadArgs *)arg;
    size_t per_thread_size = args->size / args->num_threads;
    volatile uint64_t *p = (volatile uint64_t *)((char *)args->ptr + args->thread_id * per_thread_size);
    size_t words = per_thread_size / sizeof(uint64_t);

    uint64_t total_lat = 0;
    int count = 0;

    for (int iter = 0; iter < args->iterations; iter++) {
        for (size_t i = 0; i < words; i += 16) {
            uint64_t start = get_time_ns();
            p[i] = iter + i;
            uint64_t end = get_time_ns();
            uint64_t lat = end - start;
            if (lat < 5000 && lat > 0) {
                total_lat += lat;
                count++;
            }
        }
    }

    thread_result = (count > 0) ? (double)total_lat / count : 0;
    args->latency = thread_result;

    return NULL;
}

/* 多线程测量带宽 */
static double measure_parallel_bandwidth(void *ptr, size_t size, int threads, int iterations, void *(*thread_func)(void *)) {
    pthread_t tids[MAX_THREADS];
    ThreadArgs args[MAX_THREADS];
    double bandwidths[MAX_THREADS];

    /* 分配线程参数 */
    for (int i = 0; i < threads; i++) {
        args[i].thread_id = i;
        args[i].num_threads = threads;
        args[i].ptr = ptr;
        args[i].size = size;
        args[i].iterations = iterations;
        args[i].bandwidth = &bandwidths[i];
    }

    /* 创建线程 */
    for (int i = 0; i < threads; i++) {
        pthread_create(&tids[i], NULL, thread_func, &args[i]);
    }

    /* 等待完成 */
    for (int i = 0; i < threads; i++) {
        pthread_join(tids[i], NULL);
    }

    /* 汇总带宽 */
    double total_bw = 0;
    for (int i = 0; i < threads; i++) {
        total_bw += bandwidths[i];
    }

    return total_bw;
}

/* 多线程测量延迟 */
static double measure_parallel_latency(void *ptr, size_t size, int threads, int iterations) {
    pthread_t tids[MAX_THREADS];
    ThreadArgs args[MAX_THREADS];
    double latencies[MAX_THREADS];

    for (int i = 0; i < threads; i++) {
        args[i].thread_id = i;
        args[i].num_threads = threads;
        args[i].ptr = ptr;
        args[i].size = size;
        args[i].iterations = iterations;
        args[i].latency = 0;
    }

    for (int i = 0; i < threads; i++) {
        pthread_create(&tids[i], NULL, random_read_latency_thread, &args[i]);
    }

    for (int i = 0; i < threads; i++) {
        pthread_join(tids[i], NULL);
        latencies[i] = args[i].latency;
    }

    /* 返回平均延迟 */
    double total_lat = 0;
    for (int i = 0; i < threads; i++) {
        total_lat += latencies[i];
    }
    return total_lat / threads;
}

/* 多线程测量写延迟 */
static double measure_parallel_write_latency(void *ptr, size_t size, int threads, int iterations) {
    pthread_t tids[MAX_THREADS];
    ThreadArgs args[MAX_THREADS];
    double latencies[MAX_THREADS];

    for (int i = 0; i < threads; i++) {
        args[i].thread_id = i;
        args[i].num_threads = threads;
        args[i].ptr = ptr;
        args[i].size = size;
        args[i].iterations = iterations;
        args[i].latency = 0;
    }

    for (int i = 0; i < threads; i++) {
        pthread_create(&tids[i], NULL, random_write_latency_thread, &args[i]);
    }

    for (int i = 0; i < threads; i++) {
        pthread_join(tids[i], NULL);
        latencies[i] = args[i].latency;
    }

    /* 返回平均延迟 */
    double total_lat = 0;
    for (int i = 0; i < threads; i++) {
        total_lat += latencies[i];
    }
    return total_lat / threads;
}

void run_memory_bandwidth_test(size_t size, int threads) {
    print_header("MEMORY BANDWIDTH TEST (Multi-threaded)");
    printf("Memory Size: %.2f MB, Threads: %d\n\n", size / (double)MB, threads);

    /* 获取CPU核心数和内存通道数 */
    long num_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    int channels = get_memory_channels();
    printf("CPU Cores: %ld\n", num_cpus);
    printf("Memory Channels: %d\n\n", channels);

    /* 使用 mmap 分配大内存,确保物理对齐 */
    size_t total_size = size * 2;  /* 复制需要两倍空间 */
    void *ptr = mmap(NULL, total_size, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB, -1, 0);
    int using_mmap = (ptr != MAP_FAILED);
    if (!using_mmap) {
        /* fallback to malloc */
        ptr = malloc(total_size);
        if (!ptr) {
            printf("Allocation failed\n");
            return;
        }
        memset(ptr, 0, total_size);
    }

    void *src = ptr;
    void *dst = (char *)ptr + size;

    /* 初始化内存 */
    memset(src, 0xAB, size);
    memset(dst, 0, size);

    /* Warmup */
    printf("Warming up...\n");
    measure_parallel_bandwidth(src, size, threads, 2, read_thread_func);
    measure_parallel_bandwidth(src, size, threads, 2, write_thread_func);

    /* 测量 */
    printf("Measuring...\n\n");

    int iterations = 20;
    double read_bw = measure_parallel_bandwidth(src, size, threads, iterations, read_thread_func);
    double write_bw = measure_parallel_bandwidth(src, size, threads, iterations, write_thread_func);

    /* 复制测试需要不同的内存布局 */
    double copy_bw = measure_parallel_bandwidth(src, size, threads, iterations, copy_thread_func);

    /* 延迟测量 */
    double read_latency = measure_parallel_latency(src, size, threads, iterations * 10);
    double write_latency = measure_parallel_write_latency(src, size, threads, iterations * 10);

    printf("=== BANDWIDTH RESULTS ===\n");
    printf("  Thread Count: %d\n", threads);
    int bw_channels = get_memory_channels();
    double theoretical_bw = bw_channels * 4800.0 * 8 / 1024;  /* channels * MT/s * bytes / 1024 = MB/s */
    printf("  Read:    %8.2f MB/s  (%.1f%% of theoretical %.0f GB/s)\n",
           read_bw, read_bw / theoretical_bw * 100, theoretical_bw / 1000);
    printf("  Write:   %8.2f MB/s\n", write_bw);
    printf("  Copy:    %8.2f MB/s\n\n", copy_bw);

    printf("=== LATENCY ===\n");
    printf("  Random Read Latency:  %.2f ns\n", read_latency);
    printf("  Random Write Latency: %.2f ns\n\n", write_latency);

    printf("=== ANALYSIS ===\n");
    printf("  Read/Write ratio: %.2f\n", read_bw / (write_bw > 0 ? write_bw : 1));
    printf("  Copy/Read ratio:  %.2f\n\n", copy_bw / (read_bw > 0 ? read_bw : 1));

    /* 生成报告 */
    ReportContext *report = report_init("memory_bandwidth", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "Memory Bandwidth Test Results (Multi-threaded)");

        report_write(report, "**Test Parameters**\n");
        report_write(report, "- Memory Size: %.2f MB\n", size / (double)MB);
        report_write(report, "- Thread Count: %d\n", threads);
        int rep_channels = get_memory_channels();
        double rep_theoretical = rep_channels * 4800.0 * 8 / 1024;  /* MB/s */
        report_write(report, "- Memory Channels: %d\n", rep_channels);
        report_write(report, "- Theoretical Bandwidth: ~%.0f GB/s\n\n", rep_theoretical / 1000);

        report_write(report, "## Bandwidth Results\n\n");
        report_write(report, "| Operation | Bandwidth (MB/s) |\n");
        report_write(report, "|-----------|--------------------|\n");
        report_write(report, "| Read      | %.2f           |\n", read_bw);
        report_write(report, "| Write     | %.2f           |\n", write_bw);
        report_write(report, "| Copy      | %.2f           |\n\n", copy_bw);

        report_write(report, "## Latency\n\n");
        report_write(report, "- Random Read Latency:  %.2f ns\n", read_latency);
        report_write(report, "- Random Write Latency: %.2f ns\n\n", write_latency);

        report_write(report, "## Analysis\n\n");
        report_write(report, "- Read/Write ratio: %.2f\n", read_bw / (write_bw > 0 ? write_bw : 1));
        report_write(report, "- Copy/Read ratio: %.2f\n\n", copy_bw / (read_bw > 0 ? read_bw : 1));

        report_write(report, "## Notes\n");
        int note_channels = get_memory_channels();
        report_write(report, "- Multi-threaded test uses %d threads to saturate %d-channel memory\n", threads, note_channels);
        double note_theoretical = note_channels * 4800.0 * 8 / 1024;
        report_write(report, "- Theoretical peak: ~%.0f GB/s\n", note_theoretical / 1000);
        report_write(report, "- Actual bandwidth depends on memory controller efficiency and core count\n\n");

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }

    /* 清理 */
    if (using_mmap) {
        munmap(ptr, total_size);
    } else {
        free(ptr);
    }
}

static void print_usage(const char *prog) {
    printf("Usage: %s [options]\n", prog);
    printf("Options:\n");
    printf("  -s, --size SIZE       Memory size (e.g., 1M, 100M, 1G) [default: 256M]\n");
    printf("  -t, --threads N        Number of threads [auto-detected if not specified]\n");
    printf("  -h, --help            Show this help\n");
}

int main(int argc, char *argv[]) {
    init_platform_layer();
    size_t size = 256 * MB;
    long num_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    int threads = (int)num_cpus;  /* Default: use all available cores */
    if (threads < 1) threads = 1;
    if (threads > MAX_THREADS) {
        fprintf(stderr, "[mem] %d cores detected, but MAX_THREADS=%d. "
                        "Clamping to %d threads (use -t N to override).\n",
                threads, MAX_THREADS, MAX_THREADS);
        threads = MAX_THREADS;
    }

    /* 用户可以通过 -t 参数覆盖 */

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            print_usage(argv[0]);
            return 0;
        } else if (strcmp(argv[i], "-s") == 0 || strcmp(argv[i], "--size") == 0) {
            size = parse_size(argv[++i]);
        } else if (strcmp(argv[i], "-t") == 0 || strcmp(argv[i], "--threads") == 0) {
            threads = atoi(argv[++i]);
        }
    }

    initialize_cache_config();
    initialize_system_config();
    pmu_init_cache_counters();
    print_system_info();
    run_memory_bandwidth_test(size, threads);
    return 0;
}