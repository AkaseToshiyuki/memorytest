/* SPDX-License-Identifier: MIT
 * latency.c - Cache latency measurement (timing-based fallback + unified dispatcher)
 *
 * measure_cache_latency() tries PMU first (pmu_measure_cache_latency in pmu.c),
 * then falls back to measure_latency_timing().
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

static uint64_t measure_latency_timing(void *ptr, size_t size, int sequential) {
    volatile uint64_t *p = (volatile uint64_t *)ptr;
    size_t words = size / sizeof(uint64_t);
    if (words < 8) words = 8;

    /* Pre-warm: access all words to ensure in cache */
    for (size_t i = 0; i < words; i++) {
        volatile uint64_t val = p[i];
        (void)val;
    }

    /* Measurement array - use median of multiple measurements */
    uint64_t measurements[64];
    int num_measure = 64;

    for (int m = 0; m < num_measure; m++) {
        uint64_t start, end;
        uint64_t idx;

        if (sequential) {
            /* Sequential access: stride-1 */
            start = get_time_ns();
            for (size_t i = 0; i < 64; i++) {
                idx = i & (words - 1);
                volatile uint64_t val = p[idx];
                (void)val;
            }
            end = get_time_ns();
        } else {
            /* Random access: use prime stride to avoid predictability */
            start = get_time_ns();
            for (size_t i = 0; i < 64; i++) {
                idx = ((i * 17) + 1) & (words - 1);
                volatile uint64_t val = p[idx];
                (void)val;
            }
            end = get_time_ns();
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
