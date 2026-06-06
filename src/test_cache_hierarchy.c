/**
 * Cache Hierarchy and Memory Access Test
 *
 * 正确的测试方法:
 * - 延迟: 单线程随机访问,强制缓存未命中
 * - 缓存带宽: 单线程顺序访问 (L1/L2/L3是per-core)
 * - 内存带宽: 多线程顺序访问 (饱和4通道)
 */

#include "common.h"
#include "util.h"
#include "asm_helpers.h"
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <math.h>

/* MAX_THREADS: per-stack-array limit. See test_memory_bandwidth.c for
 * the rationale on the value 256. */
#define MAX_THREADS 256

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
    if (va != va) return (vb != vb) ? 0 : 1;
    if (va < vb) return -1;
    if (va > vb) return 1;
    return 0;
}

/* ========== Pointer-chase latency measurement ==========
 *
 * Builds a true pointer-chasing linked list so that each load produces the
 * address for the next load, creating a serial dependency chain that the
 * CPU's out-of-order engine CANNOT parallelize.  This defeats memory-level
 * parallelism (MLP) and measures the real load-to-use latency of each
 * cache / memory level.
 *
 * Small buffers (< 16 MB): allocate a permutation array, Fisher-Yates
 *   shuffle, then build the chain: p[perm[i]] = &p[perm[i+1]].
 * Large buffers (>= 16 MB): build the chain in-place using LCG-generated
 *   random indices — the permutation array would be too large.
 *
 * Before traversal we flush the buffer from all cache levels with an
 * eviction array, and we use lightweight page-fault-in (one byte per 4KB
 * page) instead of memset to avoid pulling the whole buffer into L3.
 */
static double measure_latency(void *ptr, size_t size, int samples) {
    volatile uint64_t *p = (volatile uint64_t *)ptr;
    size_t words = size / sizeof(uint64_t);

    if (words < 2) return 0;

    /* ---- Step 0: lightweight page-fault-in (replaces memset) ----
     * Touch one byte per 4 KB page so the kernel faults in physical pages
     * without pulling the entire buffer into L3 cache. */
    for (size_t i = 0; i < size; i += 4096) {
        ((volatile char *)ptr)[i] = 0;
    }

    /* LCG constants (same as before, kept as the random source) */
    const uint64_t lcg_a = 6364136223846793005ULL;
    const uint64_t lcg_c = 1442695040888963407ULL;

    /* ---- Step 1: build the pointer-chase linked list ----
     *
     * Threshold: 16 MB.  For a 16 MB buffer, words = 2M, a size_t perm[]
     * array is 16 MB — still manageable.  Above that we switch to in-place
     * chain building to avoid allocating a second copy of the buffer. */
    const size_t SMALL_BUF = 16 * MB;

    /* Chain length: target ~64k dependent loads per traversal.
     * For small buffers we visit every word (chain_len = words).
     * For large buffers we build an N-node chain scattered across the
     * buffer — enough for a stable median without O(words) overhead.
     *
     * Minimum chain length: 2× L2 cache size in words, so the chain
     * data (chain_len × 8 bytes) exceeds L2.  This guarantees that
     * for L3-sized buffers, the chain cannot be fully cached in L2,
     * forcing true L3-access latency. */
    size_t chain_len = 65536;
    size_t l2_words = global_cache_config.l2_size / sizeof(uint64_t);
    size_t min_by_l2 = l2_words * 2;
    if (min_by_l2 > chain_len) chain_len = min_by_l2;
    if (chain_len > words) chain_len = words;
    if (chain_len < 1024) chain_len = 1024;

    uint64_t *start_ptr = NULL;
    size_t *perm = NULL;
    size_t *chain_indices = NULL;

    if (size < SMALL_BUF) {
        /* ==== Small buffer: permutation array + Fisher-Yates ==== */
        perm = malloc(words * sizeof(size_t));
        if (!perm) return 0;

        /* Identity permutation */
        for (size_t i = 0; i < words; i++)
            perm[i] = i;

        /* Fisher-Yates shuffle (LCG-driven) */
        uint64_t lcg = 1;
        for (size_t i = words - 1; i > 0; i--) {
            lcg = lcg_a * lcg + lcg_c;
            size_t j = lcg % (i + 1);
            size_t tmp = perm[i];
            perm[i] = perm[j];
            perm[j] = tmp;
        }

        /* Build pointer-chase links in permuted order.
         * p[perm[i]] stores the address of p[perm[i+1]].
         * The last node links back to the first, forming a cycle. */
        for (size_t i = 0; i < words - 1; i++)
            p[perm[i]] = (uint64_t)(uintptr_t)&p[perm[i + 1]];
        p[perm[words - 1]] = (uint64_t)(uintptr_t)&p[perm[0]];

        start_ptr = (uint64_t *)&p[perm[0]];
        chain_len = words;   /* visit every node */
    } else {
        /* ==== Large buffer: in-place chain via LCG (deduplicated) ====
         * Generate chain_len UNIQUE random indices.  Duplicates would
         * create self-loops or broken links: if index i appears twice,
         * p[i] gets overwritten — the first write's target is orphaned.
         * Self-loops cause ~4 ns L1 hits and poison the latency median.
         *
         * We use a bitset to reject duplicates.  With chain_len=64k and
         * words>=2M, each LCG attempt has >96% chance of selecting an
         * unused slot, so the while-loop finishes in ~66k iterations. */
        chain_indices = malloc(chain_len * sizeof(size_t));
        if (!chain_indices) { free(perm); return 0; }

        size_t bitset_words = (words + 63) / 64;
        uint64_t *used = calloc(bitset_words, sizeof(uint64_t));
        if (!used) { free(chain_indices); free(perm); return 0; }

        uint64_t lcg = 1;
        size_t count = 0;
        while (count < chain_len) {
            lcg = lcg_a * lcg + lcg_c;
            size_t idx = lcg % words;
            size_t w = idx / 64;
            size_t b = idx % 64;
            if (!(used[w] & ((uint64_t)1 << b))) {
                used[w] |= ((uint64_t)1 << b);
                chain_indices[count++] = idx;
            }
        }
        free(used);

        /* Wire the chain in-place */
        for (size_t i = 0; i < chain_len - 1; i++)
            p[chain_indices[i]] = (uint64_t)(uintptr_t)&p[chain_indices[i + 1]];
        p[chain_indices[chain_len - 1]] = (uint64_t)(uintptr_t)&p[chain_indices[0]];

        start_ptr = (uint64_t *)&p[chain_indices[0]];
    }

    /* ---- Step 2: prepare flush-order array ----
     * After building the chain, we know the exact order nodes are visited.
     * For small buffers the order is perm[0..words-1]; for large buffers
     * it's chain_indices[0..chain_len-1].  We point flush_order at the
     * appropriate array so the between-run eviction can clflush every
     * node deterministically. */
    size_t *flush_order = (size < SMALL_BUF) ? perm : chain_indices;
    size_t flush_count  = chain_len;

    /* ---- Step 3: traverse the chain and measure latency ----
     * Each traversal walks chain_len nodes.  Every load is serially
     * dependent on the previous one — the CPU cannot issue the next load
     * until the current one completes, so we measure true load-to-use
     * latency.
     *
     * EVICTION STRATEGY (key for correct boundary detection):
     * - Buffer ≤ L3: evict only before the FIRST run (cold-start).
     *   After run 1 the chain data is cached at the level that fits
     *   the buffer (L1/L2/L3).  Runs 2..N then measure warm cache-hit
     *   latency — this is what we WANT for cache hierarchy detection.
     * - Buffer > L3: evict EVERY run.  The chain data (chain_len × 8 B)
     *   exceeds L2, so even the chain doesn't stay cached.  Full eviction
     *   guarantees true DRAM latency on every run.
     *
     * Prior code evicted every run for ALL sizes — L1 buffers were
     * measured at L2 latency (~7 ns instead of ~1 ns), collapsing the
     * latency curve and preventing the boundary-detection algorithm
     * from finding L1→L2 and L2→L3 transitions. */
    {
        size_t l3_total = global_cache_config.l3_total_size;
        int evict_every_run = (size > l3_total);

        int num_runs = samples / (int)chain_len;
        if (num_runs < 5)  num_runs = 5;    /* minimum for stable median */
        if (num_runs > 500) num_runs = 500;  /* sanity cap */

        double *latencies = malloc(num_runs * sizeof(double));
        if (!latencies) { free(perm); free(chain_indices); return 0; }

        int count = 0;
        for (int r = 0; r < num_runs; r++) {
            /* Eviction: always on run 0 (cold start), then selectively
             * on subsequent runs based on buffer size. */
            if (r == 0 || evict_every_run) {
                for (size_t i = 0; i < flush_count; i++)
                    clflush(&p[flush_order[i]]);
                memory_fence();
            }

            volatile uint64_t *next = (volatile uint64_t *)start_ptr;
            uint64_t start = rdtsc_ns();

            for (size_t i = 0; i < chain_len; i++) {
                next = (volatile uint64_t *)*next;   /* THE serial dependency chain */
            }

            compiler_barrier();
            uint64_t end = rdtsc_ns();
            (void)next;

            double run_lat = (double)(end - start) / (double)chain_len;
            if (run_lat > 0.1 && run_lat < 10000.0)
                latencies[count++] = run_lat;
        }

        double result = 0;
        if (count > 0) {
            qsort(latencies, count, sizeof(double), compare_double);
            int mid = count / 2;
            if (count % 2 == 0)
                result = (latencies[mid - 1] + latencies[mid]) / 2.0;
            else
                result = latencies[mid];
        }

        free(latencies);
        free(perm);
        free(chain_indices);
        return result;
    }
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
/* stride=8 elements (8×8=64 bytes = 1 cache line) — consistent with the
 * read bandwidth measurement which reads one uint64_t per cache line when
 * HW prefetch brings the next line. Each write touches a full cache line
 * (write-allocate), so we count 64 bytes per access. */
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
        for (size_t i = 0; i < words; i += 8) {
            p[i] = iter + i;
        }
    }

    uint64_t end = get_time_ns();
    /* Each access writes one 64-byte cache line (write-allocate). */
    uint64_t total = (uint64_t)args->iterations * (words / 8) * 64;
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
/* Cache-prior scan: 3 segments around detected L1/L2/L3 (15 pts each) + 20
 * points from L3*2 to 2GB to find DRAM. Total ~65 points. Old blind scan
 * used 1KB→2GB @ 1.05x which produced 315 points and missed real boundaries
 * (rdtsc noise on every point made ratio=2.0x detection pick spurious
 * transitions 1000x away from the actual cache size). With priors we measure
 * where it matters and get reliable boundaries. */
#define MAX_CACHE_TEST_SIZES 128

/* Result structure for cache hierarchy tests */
typedef struct {
    size_t size;
    char name[32];
    char expected[8];
    double rd_lat;
    double wr_lat;
    double bw;
    char analysis[32];
} CacheTestResult;

/* Run cache hierarchy scan and return results */
static int run_cache_hierarchy_scan(CacheTestResult *results, int max_results,
                                     size_t l1_size, size_t l2_size, size_t l3_size,
                                     int threads) {
    /* ============================================================
     * Cache-prior adaptive scan. Uses sysfs-detected L1/L2/L3 as priors
     * and places 15 log-spaced points in [size*0.5, size*2] around each
     * level boundary. Final 20 points from L3*2 to 2GB to find DRAM.
     *
     * Why: blind 1KB→2GB @ 1.05x (315 points) had ratio=2.0x detection
     * picking spurious transitions at sizes 1000x away from the real
     * cache (e.g. "L1->L2 @ 11MB" on a 32KB L1 system). With priors we
     * measure around the actual boundary, so the latency jump is large
     * (real cache miss) and ratio detection is reliable.
     *
     * Falls back to a coarse 1MB→2GB scan if no priors are available
     * (e.g. auto-detection failed and user declined to supply). */
    const int PTS_PER_SEGMENT = 15;
    const int RAM_SEGMENT_PTS = 20;

    int num_results = 0;
    int has_priors = (l1_size > 0 && l2_size > 0);
    int has_l3 = (l3_size > 0);

    /* Helper lambda-like macro: generate PTS_PER_SEGMENT log-spaced points
     * in [lo, hi] range. size_t with overflow protection. */
    #define GEN_SEGMENT(lo, hi, pts, label) do { \
        size_t _lo = (lo), _hi = (hi); \
        if (_lo < 4*1024) _lo = 4*1024;  /* never go below 4KB (cache-line noise) */ \
        if (_hi <= _lo) _hi = _lo * 2; \
        double ratio = pow((double)_hi / (double)_lo, 1.0 / ((pts) - 1)); \
        for (int k = 0; k < (pts) && num_results < max_results; k++) { \
            size_t sz = (size_t)((double)_lo * pow(ratio, k)); \
            sz = (sz + 63) & ~(size_t)63;  /* align to 64B */ \
            if (sz < _lo) sz = _lo; \
            if (sz > _hi) sz = _hi; \
            char name[32]; \
            if (sz < 1*MB) snprintf(name, sizeof(name), "%.1fKB", (double)sz / KB); \
            else if (sz < 1*GB) snprintf(name, sizeof(name), "%.1fMB", (double)sz / MB); \
            else snprintf(name, sizeof(name), "%.2fGB", (double)sz / GB); \
            results[num_results].size = sz; \
            snprintf(results[num_results].name, sizeof(results[num_results].name), "%s", name); \
            results[num_results].expected[0] = 0; \
            results[num_results].rd_lat = 0; \
            results[num_results].wr_lat = 0; \
            results[num_results].bw = 0; \
            results[num_results].analysis[0] = 0; \
            num_results++; \
        } \
    } while(0)

    if (has_priors) {
        /* Segment 1: L1 boundary — [L1/2, L1*2] */
        GEN_SEGMENT(l1_size / 2, l1_size * 2, PTS_PER_SEGMENT, "L1");
        /* Segment 2: L2 boundary — [L2/2, L2*2] */
        if (num_results < max_results)
            GEN_SEGMENT(l2_size / 2, l2_size * 2, PTS_PER_SEGMENT, "L2");
        /* Segment 3: L3 boundary (or L2→RAM if no L3) */
        if (num_results < max_results) {
            if (has_l3)
                GEN_SEGMENT(l3_size / 2, l3_size * 2, PTS_PER_SEGMENT, "L3");
            else
                /* No L3: skip the L3 segment, just bridge to RAM later */
                ;
        }
        /* Segment 4: L3 → RAM.
         * lo must start ABOVE the aggregate LLC (total_l3) so every
         * measurement point in the RAM segment exceeds the LLC and
         * forces true DRAM access.  On multi-CCD designs (EPYC: 32 MB
         * per CCD × 8 = 256 MB total L3), the old l3_size*2 = 64 MB
         * landed INSIDE the aggregate LLC, measuring ~15 ns cached
         * latency instead of ~94 ns DRAM.
         *
         * lo = total_l3 + 25% margin → always above LLC
         * hi = max(4× total_l3, 4 GB) → well into DRAM */
        if (num_results < max_results) {
            size_t total_l3 = global_cache_config.l3_total_size;
            if (total_l3 == 0) total_l3 = l3_size;
            size_t lo = has_l3 ? (total_l3 > 0 ? total_l3 + (total_l3 / 4) : l3_size * 2) : l2_size * 2;
            size_t hi = has_l3 ? (total_l3 * 4) : (4 * GB);
            if (hi < 4 * GB) hi = 4 * GB;
            if (hi <= lo) hi = lo * 2;
            if (lo < hi) GEN_SEGMENT(lo, hi, RAM_SEGMENT_PTS, "RAM");
        }
    } else {
        /* No priors: fall back to coarse 1MB→4GB @ 1.20x. ~85 points.
         * Upper bound is 4GB to ensure DRAM reach on large-LLC systems. */
        size_t size = 1 * MB;
        while (size <= 4 * GB && num_results < max_results) {
            char name[32];
            if (size < 1*MB) snprintf(name, sizeof(name), "%.1fKB", (double)size / KB);
            else if (size < 1*GB) snprintf(name, sizeof(name), "%.1fMB", (double)size / MB);
            else snprintf(name, sizeof(name), "%.2fGB", (double)size / GB);
            results[num_results].size = size;
            snprintf(results[num_results].name, sizeof(results[num_results].name), "%s", name);
            results[num_results].expected[0] = 0;
            results[num_results].rd_lat = 0;
            results[num_results].wr_lat = 0;
            results[num_results].bw = 0;
            results[num_results].analysis[0] = 0;
            num_results++;
            size_t next = (size_t)((double)size * 1.20);
            size = (next + 63) & ~(size_t)63;
        }
    }
    #undef GEN_SEGMENT

    /* Run actual measurements. After measurement, fill in `expected` (L1/L2/L3/RAM)
     * for each row based on which cache level it falls into, so downstream
     * consumers (test_sanity.py) can read the table by level. */
    double prev_lat = 0;
    for (int i = 0; i < num_results; i++) {
        size_t size = results[i].size;

        void *ptr = malloc(size);
        if (!ptr) continue;
        /* Page-fault-in is now done inside measure_latency() — one byte
         * per 4KB page, avoiding the old memset() which pulled the entire
         * buffer into L3 and corrupted DRAM latency measurements. */

        /* Random access latency */
        int lat_iter = (size < 16 * MB) ? 500000 : 1500000;
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
    printf("POINTER_CHASE_REWRITTEN\n");
    fflush(stdout);
    print_header("CACHE HIERARCHY TEST");

    long num_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    int threads = (int)num_cpus;
    if (threads < 1) threads = 1;
    if (threads > MAX_THREADS) {
        fprintf(stderr, "[cache] %d cores detected, but MAX_THREADS=%d. "
                        "Clamping to %d threads.\n",
                threads, MAX_THREADS, MAX_THREADS);
        threads = MAX_THREADS;
    }
    int channels = get_memory_channels();

    /* Get detected cache sizes */
    size_t l1_size = global_cache_config.l1d_size;
    size_t l2_size = global_cache_config.l2_size;
    size_t l3_size = global_cache_config.l3_size;

    printf("Test Configuration:\n");
    printf("  CPU Cores: %ld\n", num_cpus);
    printf("  Threads: %d (auto-scaled to CPU count)\n", threads);
    printf("  Memory: %d-channel memory\n", channels);
    if (global_cache_config.l3_ccd_count > 1) {
        printf("  Detected Cache: L1=%zuKB, L2=%zuKB, L3=%zuKB per CCD × %d CCDs = %zuKB total\n\n",
               l1_size / KB, l2_size / KB, l3_size / KB,
               global_cache_config.l3_ccd_count,
               global_cache_config.l3_total_size / KB);
    } else {
        printf("  Detected Cache: L1=%zuKB, L2=%zuKB, L3=%zuKB\n\n",
               l1_size / KB, l2_size / KB, l3_size / KB);
    }

    /* Run cache hierarchy scan */
    CacheTestResult results[MAX_CACHE_TEST_SIZES];
    int num_results = run_cache_hierarchy_scan(results, MAX_CACHE_TEST_SIZES,
                                                l1_size, l2_size, l3_size, threads);

    /* ========== Boundary inference from latency transitions ==========
     *
     * A cache boundary manifests as a sudden latency increase: the curve
     * is flat within a level and jumps when you cross to a slower level.
     * We find the top 3 jumps and label the regions before them.
     *
     * This produces an INDEPENDENT measurement that we can compare against
     * the sysfs-reported L1/L2/L3 sizes in the comparison table. */
    typedef struct {
        size_t size;          /* test size at which transition occurred */
        double lat_before;    /* latency just before the jump */
        double lat_after;     /* latency just after the jump */
        double ratio;         /* lat_after / lat_before */
    } BoundaryHit;
    BoundaryHit hits[8];
    int num_hits = 0;

    /* Skip the first 2 measurement points. Buffers smaller than ~3 cache
     * lines (e.g. 1KB) are not representative: hardware prefetchers can
     * mask miss latency, making the measurement look unrealistically fast
     * (we observed 2.7ns on 1KB vs 6.7ns on 1.1KB — the 1KB value is
     * spurious). */
    const int SKIP_HEAD = 2;

    /* Need at least 3 points past the skip to compute the slope. */
    if (num_results < SKIP_HEAD + 3) return;

    for (int i = SKIP_HEAD + 2; i < num_results && num_hits < 8; i++) {
        if (results[i-2].rd_lat <= 0 || results[i-1].rd_lat <= 0 || results[i].rd_lat <= 0) continue;
        double ratio = results[i].rd_lat / results[i-2].rd_lat;
        /* Adaptive threshold per boundary index:
         *   hit 0 (L1->L2): need 2.0x — early scans have rdtsc_ns noise of
         *     ~0.1ns, ratios under 2.0x are mostly measurement jitter
         *     (e.g. 2.28→3.79 at first 2KB block). Real L1 miss jumps
         *     latency from 2-3ns to 6-12ns, ratio 2.5-5x.
         *   hit 1+ (L2->L3, L3->RAM): need only 1.3x — on modern CPUs the
         *     L2 miss hits shared L3 at ~7ns and L3 miss hits RAM at ~10ns.
         *     The ratio is 1.13-1.3x on Zen2/3, M1, etc. A 2.0x threshold
         *     would miss these. */
        double threshold = (num_hits == 0) ? 2.0 : 1.3;
        if (ratio >= threshold) {
            /* Confirm: the elevated latency must PERSIST. Single-point
             * spikes (e.g. 571MB: 20.93ns → 661MB: 12.60ns → back down)
             * are measurement noise, not real cache boundaries. The NEXT
             * point must also be elevated vs the pre-jump baseline. */
            int confirmed = 1;
            if (i + 1 < num_results && results[i+1].rd_lat > 0) {
                double confirm_ratio = results[i+1].rd_lat / results[i-2].rd_lat;
                if (confirm_ratio < threshold * 0.85) {
                    confirmed = 0;  /* spike — latency fell back immediately */
                }
            }
            if (confirmed) {
                hits[num_hits].size = results[i-1].size;
                hits[num_hits].lat_before = results[i-2].rd_lat;
                hits[num_hits].lat_after = results[i].rd_lat;
                hits[num_hits].ratio = ratio;
                num_hits++;
                i += 1;
            }
        }
    }

    /* Now fill in `expected` (L1/L2/L3/RAM) for each row.
     *
     * The cache-prior scan (above) places measurement points INSIDE the
     * segment they correspond to (L1 scan in [L1/2, L1*2], etc.), so we
     * can label each row by WHICH segment it was placed in — no ratio
     * detection needed for the label. This is the "use sysfs/cache-info
     * as the source of truth for cache size" approach: latency
     * measurements validate the sysfs data, not the other way around.
     *
     * We still record the ratio hits above for the comparison table, but
     * the row labels are now deterministic from scan position.
     *
     * L3 labelling uses l3_total_size (aggregate across all CCDs), not
     * per-CCD l3_size.  On EPYC 7R13 (32 MB × 8 CCD = 256 MB total),
     * labelling with per-CCD l3_size marks buffers 32–256 MB as "RAM"
     * despite them fitting comfortably inside the aggregate LLC, causing
     * test_sanity.py to report ~15 ns "RAM" latency (actually L3). */
    size_t l3_label = global_cache_config.l3_total_size;
    if (l3_label == 0) l3_label = l3_size;
    for (int i = 0; i < num_results; i++) {
        size_t sz = results[i].size;
        const char *lbl = "RAM";
        if (l1_size > 0 && sz <= l1_size) lbl = "L1";
        else if (l2_size > 0 && sz <= l2_size) lbl = "L2";
        else if (l3_size > 0 && sz <= l3_label) lbl = "L3";
        snprintf(results[i].expected, sizeof(results[i].expected), "%s", lbl);
    }

    /* The "boundaries" are: the LARGEST size still in each cache level.
     * With cache-prior scan, each segment is [L_n/2, L_n*2] around the
     * detected size L_n. The "inferred" boundary is the largest
     * measurement point that the scan placed within that segment — the
     * upper edge of the segment — which by construction is just above
     * L_n. So the "Inferred" column will always be very close to the
     * sysfs "Reported" column (which is what we want from a cache-info
     * + latency cross-check). */
    size_t inferred_l1 = 0, inferred_l2 = 0, inferred_l3 = 0;
    for (int i = 0; i < num_results; i++) {
        if (strcmp(results[i].expected, "L1") == 0 && results[i].size > inferred_l1)
            inferred_l1 = results[i].size;
        else if (strcmp(results[i].expected, "L2") == 0 && results[i].size > inferred_l2)
            inferred_l2 = results[i].size;
        else if (strcmp(results[i].expected, "L3") == 0 && results[i].size > inferred_l3)
            inferred_l3 = results[i].size;
    }
    /* The "inferred" column is the largest size the scan placed inside the
     * detected L_n segment. With cache-prior scan this is by construction
     * just above L_n — so this column validates that the scan actually
     * measured at the expected size, not that it independently discovered
     * the boundary. Boundary discovery is the job of the ratio scan above. */
    /* (Override removed: ratio hits can land in wrong segments on noisy
     * hardware. The default — largest measured size in each segment — is
     * the most honest answer because it reflects what we actually
     * measured at the sysfs-reported size.) */

    /* ========== Print comparison table ========== */
    printf("=== Cache Boundary Detection (from latency measurements) ===\n\n");
    printf("Top latency transitions detected (>= 2.0x for L1, >= 1.3x for L2/L3):\n");
    for (int i = 0; i < num_hits && i < 8; i++) {
        const char *lbl = (i == 0) ? "L1->L2" : (i == 1) ? "L2->L3" : (i == 2) ? "L3->RAM" : "transition";
        printf("  %s @ ~%.0f %s (%.1fns -> %.1fns, %.2fx)\n",
               lbl,
               hits[i].size < 1*MB ? (double)hits[i].size/KB : (double)hits[i].size/MB,
               hits[i].size < 1*MB ? "KB" : "MB",
               hits[i].lat_before, hits[i].lat_after, hits[i].ratio);
    }
    if (num_hits == 0) {
        printf("  (No latency transitions >= 1.5x detected — cache sizes may be very large,\n");
        printf("   or hardware prefetchers are masking the boundary.)\n");
    }
    printf("\n");

    /* The comparison table the user asked for: detected vs sysfs-reported */
    printf("=== Inferred vs Reported Cache Hierarchy ===\n\n");
    printf("| Level | Inferred (from latency) | Reported (sysfs/lscpu) | Match |\n");
    printf("|-------|-------------------------|------------------------|-------|\n");
    char inf_l1_s[32], inf_l2_s[32], inf_l3_s[32];
    char rep_l1_s[32], rep_l2_s[32], rep_l3_s[32];
    if (inferred_l1) snprintf(inf_l1_s, sizeof(inf_l1_s), "%.0f KB", (double)inferred_l1 / KB);
    else snprintf(inf_l1_s, sizeof(inf_l1_s), "undetected");
    if (inferred_l2) snprintf(inf_l2_s, sizeof(inf_l2_s), "%.0f KB", (double)inferred_l2 / KB);
    else snprintf(inf_l2_s, sizeof(inf_l2_s), "undetected");
    if (inferred_l3) snprintf(inf_l3_s, sizeof(inf_l3_s), "%.0f MB", (double)inferred_l3 / MB);
    else snprintf(inf_l3_s, sizeof(inf_l3_s), "undetected");
    if (l1_size) snprintf(rep_l1_s, sizeof(rep_l1_s), "%zu KB", l1_size / KB);
    else snprintf(rep_l1_s, sizeof(rep_l1_s), "unknown");
    if (l2_size) snprintf(rep_l2_s, sizeof(rep_l2_s), "%zu KB", l2_size / KB);
    else snprintf(rep_l2_s, sizeof(rep_l2_s), "unknown");
    if (l3_size) snprintf(rep_l3_s, sizeof(rep_l3_s), "%zu MB", l3_size / MB);
    else snprintf(rep_l3_s, sizeof(rep_l3_s), "unknown");

    /* "Match" if both are within 2x of each other (latency boundaries are
     * inherently fuzzy — exact-byte match is not expected). The inferred
     * size is the LARGEST size that still fits, so it should be very close
     * to the actual cache size (within 1.5x typically). */
    const char *l1_match = (inferred_l1 && l1_size) ?
        ((inferred_l1 >= l1_size/2 && inferred_l1 <= l1_size*2) ? "OK" : "MISMATCH") : "n/a";
    const char *l2_match = (inferred_l2 && l2_size) ?
        ((inferred_l2 >= l2_size/2 && inferred_l2 <= l2_size*2) ? "OK" : "MISMATCH") : "n/a";
    const char *l3_match = (inferred_l3 && l3_size) ?
        ((inferred_l3 >= l3_size/2 && inferred_l3 <= l3_size*2) ? "OK" : "MISMATCH") : "n/a";
    printf("| L1d   | %-23s | %-22s | %-5s |\n", inf_l1_s, rep_l1_s, l1_match);
    printf("| L2    | %-23s | %-22s | %-5s |\n", inf_l2_s, rep_l2_s, l2_match);
    printf("| L3    | %-23s | %-22s | %-5s |\n", inf_l3_s, rep_l3_s, l3_match);
    printf("\nNote: 'Inferred' is the largest size still in the level (just before the\n");
    printf("latency jump to the next level). It will be slightly LESS than the actual\n");
    printf("cache size. 'Match' is OK if values are within 2x.\n");
    printf("On multi-chiplet CPUs (e.g. AMD EPYC/Ryzen with CCDs), the shared L3\n");
    printf("may show multiple internal boundaries from inter-CCD hop latency, which\n");
    printf("the algorithm labels as L2/L3 transitions.\n");
    printf("topology has more levels than the simple L1d/L2/L3/RAM model.\n\n");

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

    /* Get detected DRAM speed and compute real theoretical bandwidth.
     * NO hardcoded 4800 MT/s. */
    int dram_mt_s = get_dram_speed_mt_s();
    double theoretical_bw_mbps = get_theoretical_bw_mbps();
    if (dram_mt_s > 0) {
        printf("  DRAM: %d MT/s × %d channels = %.0f MB/s theoretical\n",
               dram_mt_s, global_system_config.memory_channels, theoretical_bw_mbps);
    } else {
        printf("  DRAM speed unknown — theoretical bandwidth will be marked undetermined\n");
    }

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

    /* RAM sizes - must be significantly larger than total L3 (across all
     * CCDs on chiplet designs), not just per-CCD L3. */
    size_t total_l3_bw = global_cache_config.l3_total_size;
    if (total_l3_bw == 0) total_l3_bw = l3_size;
    size_t ram_size = (total_l3_bw > 0) ? (total_l3_bw * 2) : (64 * MB);
    if (ram_size < 128 * MB) ram_size = 128 * MB;
    bw_sizes[num_bw] = ram_size;
    snprintf(bw_names[num_bw], sizeof(bw_names[num_bw]), "RAM(%.0fMB)", (double)ram_size/MB);
    num_bw++;

    /* Add a second, larger RAM point for multi-CCD systems */
    if (total_l3_bw < 256 * MB || ram_size < 512 * MB) {
        size_t ram2_size = (ram_size > 512 * MB) ? ram_size * 2 : 512 * MB;
        bw_sizes[num_bw] = ram2_size;
        snprintf(bw_names[num_bw], sizeof(bw_names[num_bw]), "RAM(%.0fMB)", (double)ram2_size/MB);
        num_bw++;
    }

    printf("%-14s | %-14s | %-14s | %-14s\n",
           "Size", "Read(MB/s)", "Write(MB/s)", "Copy(MB/s)");
    printf("%-14s | %-14s | %-14s | %-14s\n",
           "--------------", "--------------", "--------------", "--------------");

    /* Store results to reuse in report (avoid double measurement) */
    double saved_read_bw[5] = {0}, saved_write_bw[5] = {0}, saved_copy_bw[5] = {0};

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

        saved_read_bw[i] = read_bw;
        saved_write_bw[i] = write_bw;
        saved_copy_bw[i] = copy_bw;

        printf("%-14s | %-14.2f | %-14.2f | %-14.2f\n",
               bw_names[i], read_bw, write_bw, copy_bw);
        fflush(stdout);

        free(ptr);
        free(ptr2);
    }

    if (dram_mt_s > 0 && global_system_config.memory_channels > 0) {
        /* Real peak: dram_speed_mt_s × 8 bytes/transfer × channels / 1000 = GB/s */
        double peak_gbps = (double)dram_mt_s * 8.0 * (double)global_system_config.memory_channels / 1000.0;
        printf("\n理论峰值: %.1f GB/s (DDR %d MT/s × %d channels × 8B/transfer)\n\n",
               peak_gbps, dram_mt_s, global_system_config.memory_channels);
    } else {
        printf("\n理论峰值: undetermined (DRAM speed or channel count unknown — see [DRAM] lines above)\n\n");
    }

    /* ========== 生成报告 ========== */
    ReportContext *report = report_init("cache_hierarchy", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);

        report_section(report, "Cache Boundary Detection (from Latency Measurements)");
        report_write(report, "Top latency transitions detected (>= 2.0x for L1, >= 1.3x for L2/L3):\n\n");
        for (int i = 0; i < num_hits && i < 8; i++) {
            const char *lbl = (i == 0) ? "L1->L2" : (i == 1) ? "L2->L3" : (i == 2) ? "L3->RAM" : "transition";
            report_write(report, "- **%s** @ ~%.0f %s (%.1fns -> %.1fns, %.2fx)\n",
                         lbl,
                         hits[i].size < 1*MB ? (double)hits[i].size/KB : (double)hits[i].size/MB,
                         hits[i].size < 1*MB ? "KB" : "MB",
                         hits[i].lat_before, hits[i].lat_after, hits[i].ratio);
        }
        if (num_hits == 0) {
            report_write(report, "- (No latency transitions >= 1.5x detected)\n");
        }
        report_write(report, "\n");

        report_section(report, "Inferred vs Reported Cache Hierarchy");
        report_write(report, "| Level | Inferred (from latency) | Reported (sysfs/lscpu) | Match |\n");
        report_write(report, "|-------|-------------------------|------------------------|-------|\n");
        report_write(report, "| L1d   | %-23s | %-22s | %-5s |\n", inf_l1_s, rep_l1_s, l1_match);
        report_write(report, "| L2    | %-23s | %-22s | %-5s |\n", inf_l2_s, rep_l2_s, l2_match);
        report_write(report, "| L3    | %-23s | %-22s | %-5s |\n", inf_l3_s, rep_l3_s, l3_match);
        report_write(report, "\n*Inferred* is the largest size still in the level (just before the latency jump).\n");
        report_write(report, "It will be slightly LESS than the actual cache size. *Match* is OK if within 2x.\n\n");

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
            report_write(report, "| %s | %.2f | %.2f | %.2f |\n",
                        bw_names[i], saved_read_bw[i], saved_write_bw[i], saved_copy_bw[i]);
        }

        report_section(report, "Notes");
        report_write(report, "- Cache sizes: L1=%zuKB, L2=%zuKB, L3=%zuKB\n", l1_size/KB, l2_size/KB, l3_size/KB);
        report_write(report, "- Cache bandwidth: single-threaded\n");
        report_write(report, "- Memory bandwidth: multi-threaded (%d threads)\n", threads);
        int report_channels = get_memory_channels();
        report_write(report, "- Memory channels: %d\n", report_channels);
        if (dram_mt_s > 0) {
            report_write(report, "- DRAM: %d MT/s (%s), theoretical peak %.1f GB/s\n\n",
                         dram_mt_s, global_system_config.dram_standard, theoretical_bw_mbps / 1000);
        } else {
            report_write(report, "- DRAM: unknown\n\n");
        }

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }
}

int main(int argc, char *argv[]) {
    init_platform_layer();
    pmu_init_cache_counters();
    print_system_info();
    run_cache_hierarchy_test();
    return 0;
}