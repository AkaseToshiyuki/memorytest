/* SPDX-License-Identifier: MIT
 * latency.c - Cache latency measurement (timing-based fallback + unified dispatcher)
 *
 * measure_cache_latency() tries PMU first (pmu_measure_cache_latency in pmu.c),
 * then falls back to measure_latency_timing().
 * Split from monolithic common.c (2026-06-02).
 */
#include "common.h"
#include "asm_helpers.h"
#include <stddef.h>
#include <stdarg.h>
#include <string.h>
#include <signal.h>
#include <setjmp.h>
#include <sys/wait.h>
#include <linux/types.h>

static uint64_t measure_latency_timing(void *ptr, size_t size, int sequential) {
    /* asm_helpers.h: rdtsc_ns() replaces clock_gettime (~30-50ns → ~12-20ns).
     * The pointer cast stays volatile to prevent the compiler from
     * hoisting the load out of the timing window, but we add a
     * compiler_barrier() before the cycle counter read to guarantee
     * the load completes first.
     *
     * Prefetch is conditional: only use it when the working set is larger
     * than L1D (otherwise HW prefetcher would pull the whole buffer into
     * L1 and we'd measure artificially low latencies even for "RAM" tests).
     * For small (L1-fit) buffers, we skip prefetch and let the natural
     * L1 hit latency show. */
    volatile uint64_t *p = (volatile uint64_t *)ptr;
    size_t words = size / sizeof(uint64_t);
    if (words < 8) words = 8;

    int working_set_kb = (int)(size / 1024);

    /* Total on-chip cache: L1D + L2 + total L3 (all CCDs).  Prefetching
     * only helps when the working set fits within the aggregate cache; for
     * DRAM-sized buffers the prefetch chain hides the real memory latency
     * (every access after the first hits L2 from the previous prefetch,
     * averaging ~6ns instead of ~80ns). */
    size_t total_cache_kb = (global_cache_config.l1d_size
                           + global_cache_config.l2_size
                           + global_cache_config.l3_total_size) / 1024;

    /* Prefetch only when the working set fits entirely within on-chip cache.
     * For DRAM-sized buffers (> total cache) we must NOT prefetch — we
     * need uncached misses to measure real DRAM latency. */
    int use_prefetch = (working_set_kb > 0 && working_set_kb <= (int)total_cache_kb);

    /* Pre-warm: only for cache-fitting buffers.  For DRAM-sized buffers,
     * pre-warming would fill L3 with a tail chunk and artificially lower
     * the measured latency via L3 hits. */
    if (use_prefetch) {
        for (size_t i = 0; i < words; i++) {
            volatile uint64_t val = p[i];
            (void)val;
        }
    }

    /* Measurement array - use median of multiple measurements */
    uint64_t measurements[64];
    int num_measure = 64;

    for (int m = 0; m < num_measure; m++) {
        uint64_t start, end;
        uint64_t idx;

        if (sequential) {
            /* Sequential access: stride-1. Prefetch only for L2+ working sets.
             * Use modulo instead of bitwise AND — words may not be a power of 2
             * for arbitrary malloc'ed buffer sizes. */
            start = rdtsc_ns();
            for (size_t i = 0; i < 64; i++) {
                idx = i % words;
                if (use_prefetch) prefetch_l2(&p[(i + 8) % words]);
                compiler_barrier();
                volatile uint64_t val = p[idx];
                (void)val;
            }
            compiler_barrier();
            end = rdtsc_ns();
        } else {
            /* Random access: use LCG pseudo-random sequence to defeat
             * hardware prefetchers. The old (i*17+1) stride was only 136
             * bytes apart — easily detected by modern stride prefetchers
             * (which track strides up to 2KB). An LCG with large multipliers
             * produces indices that look uniformly random, forcing real
             * cache misses on large buffers. */
            static const uint64_t lcg_a = 6364136223846793005ULL;
            static const uint64_t lcg_c = 1442695040888963407ULL;
            uint64_t lcg_state = 1 + (uint64_t)m * 17;
            start = rdtsc_ns();
            for (size_t i = 0; i < 64; i++) {
                lcg_state = lcg_a * lcg_state + lcg_c;
                idx = lcg_state % words;
                if (use_prefetch) {
                    uint64_t next_lcg = lcg_a * lcg_state + lcg_c;
                    size_t next_idx = next_lcg % words;
                    prefetch_l2(&p[next_idx]);
                }
                compiler_barrier();
                volatile uint64_t val = p[idx];
                (void)val;
            }
            compiler_barrier();
            end = rdtsc_ns();
        }

        measurements[m] = (end - start) / 64;  /* ns per access */
    }

    /* Sort and get median */
    qsort(measurements, num_measure, sizeof(uint64_t), compare_uint64);
    return measurements[num_measure / 2];  /* Median */
}

/* ========== Unified Cache Latency Measurement ========== */
/**
 * Main cache latency measurement function
 * Uses PMU if available, falls back to timing-based
 */
static uint64_t measure_cache_latency(void *ptr, size_t size, int level) {
    /* Try PMU first */
    uint64_t pmu_latency = pmu_measure_cache_latency(ptr, level);
    if (pmu_latency > 0) {
        return pmu_latency;
    }

    /* Fallback: timing-based with sequential access (cache hit) */
    return measure_latency_timing(ptr, size, 1);
}

/* ========== Cache Efficiency Measurement ========== */
/**
 * Measure cache efficiency: sequential vs random access ratio
 * Higher ratio = more cache misses (expected for larger sizes)
 */
static double measure_cache_efficiency(void *ptr, size_t size) {
    /* Sequential access latency (cached) */
    uint64_t seq_lat = measure_latency_timing(ptr, size, 1);

    /* Random access latency (may miss) */
    uint64_t rnd_lat = measure_latency_timing(ptr, size, 0);

    if (seq_lat > 0) {
        return (double)rnd_lat / seq_lat;
    }
    return 1.0;
}
