/* SPDX-License-Identifier: MIT
 * asm_helpers.h - Inline asm primitives for low-overhead benchmarking
 *
 * The benchmark suite measures latencies in the 1-100ns range, so the
 * overhead of clock_gettime(CLOCK_MONOTONIC) (~30-50ns/syscall) and the
 * serializing effect of `volatile` pointers can dominate the signal.
 *
 * This header provides:
 *   - rdtsc_ns() / cntvct_ns() - read the cycle counter in user space
 *   - memory_fence() - full memory barrier (dmb ish on ARM64, mfence on x86)
 *   - prefetch_l1() - L1 cache prefetch hint
 *   - compiler_barrier() - prevent compiler reordering only (no hw fence)
 *
 * All functions are inline static, with portable fallbacks via __builtin_*.
 */

#ifndef ASM_HELPERS_H
#define ASM_HELPERS_H

#include <stdint.h>
#include "common.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ============================================================
 * Cycle counter (low-overhead timestamp)
 *
 *   ARM64: cntvct_el0 - Virtual Cycle Count. ~20ns read.
 *          Frequency is per-CPU and can vary; we calibrate against
 *          get_time_ns() once on first use.
 *   x86_64: rdtsc - ~12ns read. Invariant TSC (constant_tsc) is required
 *            for cycles to map to wall time across cores; most server CPUs
 *            since Nehalem (2008) have it.
 *   Other: fall back to get_time_ns() from common.h.
 * ============================================================ */

static inline uint64_t rdtsc_cycles(void) {
#if defined(__aarch64__)
    uint64_t v;
    __asm__ __volatile__("mrs %0, cntvct_el0" : "=r"(v));
    return v;
#elif defined(__x86_64__) || defined(__i386__)
    uint32_t lo, hi;
    __asm__ __volatile__("rdtsc" : "=a"(lo), "=d"(hi));
    return ((uint64_t)hi << 32) | lo;
#else
    /* Portable fallback: use get_time_ns() (1ns resolution is enough for our
     * fallback path; the loop will be slow but still correct). */
    return get_time_ns();
#endif
}

/* Calibrated: convert cycles to nanoseconds. Lazily initialized.
 *
 * IMPORTANT: on ARM64, cntvct_el0 ticks at the *counter frequency* (cntfrq_el0),
 * NOT the CPU core frequency. On this dev host (Kunpeng 920) cntfrq_el0=100MHz
 * even though the CPU runs at 3GHz. Using get_cpu_freq_mhz() here would
 * underestimate time by 30x. We read cntfrq_el0 directly for ARM64.
 * On x86 with invariant TSC, rdtsc ticks at the CPU frequency. */
static inline uint64_t rdtsc_ns(void) {
    static double cycles_per_ns = 0.0;
    if (__builtin_expect(cycles_per_ns == 0.0, 0)) {
#if defined(__aarch64__)
        uint64_t cntfrq;
        __asm__ __volatile__("mrs %0, cntfrq_el0" : "=r"(cntfrq));
        cycles_per_ns = (double)cntfrq / 1e9;  /* Hz -> cycles/ns */
#elif defined(__x86_64__) || defined(__i386__)
        int freq_mhz = get_cpu_freq_mhz();
        if (freq_mhz <= 0) freq_mhz = 3000;
        cycles_per_ns = (double)freq_mhz / 1000.0;
#else
        cycles_per_ns = 1.0;  /* fallback path already returns ns */
#endif
    }
    return (uint64_t)((double)rdtsc_cycles() / cycles_per_ns);
}

/* ============================================================
 * Memory barriers
 * ============================================================ */

static inline void memory_fence(void) {
#if defined(__aarch64__)
    __asm__ __volatile__("dmb ish" ::: "memory");
#elif defined(__x86_64__) || defined(__i386__)
    __asm__ __volatile__("mfence" ::: "memory");
#else
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
#endif
}

/* Compiler-only barrier: prevents the compiler from reordering memory
 * accesses, but does NOT emit a hardware fence. Use between a load and a
 * cycle-counter read to ensure the load completes first. */
static inline void compiler_barrier(void) {
    __asm__ __volatile__("" ::: "memory");
}

/* ============================================================
 * Prefetch hints
 * ============================================================ */

static inline void prefetch_l1(const void *p) {
#if defined(__aarch64__)
    /* prfm pldl1strm - prefetch for load, L1, streaming (low temporal locality).
     * In a pointer-chase loop the next access is the *next* node, so we use
     * pldl1keep (keep in cache) since each line is touched once and then
     * re-used if it fits in L1. */
    __asm__ __volatile__("prfm pldl1keep, [%0]" :: "r"(p) : "memory");
#elif defined(__x86_64__) || defined(__i386__)
    __builtin_prefetch(p, 0 /* read */, 0 /* non-temporal locality */);
#else
    __builtin_prefetch(p, 0, 0);
#endif
}

/* Prefetch a `volatile` pointer (e.g. read-only benchmark buffers cast to
 * volatile uint64_t*). C lacks a "volatile const" type qualifier combination,
 * so we provide a separate overload that takes volatile. */
static inline void prefetch_l1_volatile(const volatile void *p) {
    prefetch_l1((const void *)p);
}

static inline void prefetch_l2(const void *p) {
#if defined(__aarch64__)
    __asm__ __volatile__("prfm pldl2keep, [%0]" :: "r"(p) : "memory");
#elif defined(__x86_64__) || defined(__i386__)
    __builtin_prefetch(p, 0, 1);
#else
    __builtin_prefetch(p, 0, 1);
#endif
}

/* ============================================================
 * Cache line flush (for forcing a miss on next access)
 * ============================================================ */

static inline void clflush(const void *p) {
#if defined(__x86_64__) || defined(__i386__)
    __asm__ __volatile__("clflush [%0]" :: "r"(p) : "memory");
#elif defined(__aarch64__)
    /* DC CIVAC - Data Cache line Clean and Invalidate by VA to Point of Coherence.
     * On ARM64 this both flushes and invalidates, more aggressive than x86 clflush. */
    __asm__ __volatile__("dc civac, %0" :: "r"(p) : "memory");
#else
    /* No portable equivalent. Use a memory_fence() as a weak fallback. */
    memory_fence();
#endif
}

#ifdef __cplusplus
}
#endif

#endif /* ASM_HELPERS_H */
