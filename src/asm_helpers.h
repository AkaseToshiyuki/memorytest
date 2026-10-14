/* SPDX-License-Identifier: MIT
 * asm_helpers.h - Inline asm primitives for low-overhead benchmarking
 *
 * The benchmark suite measures latencies in the 1-100ns range, so the
 * overhead of clock_gettime(CLOCK_MONOTONIC) (~30-50ns/syscall) and the
 * serializing effect of `volatile` pointers can dominate the signal.
 *
 * This header provides:
 *   - rdtsc_ns() - portable CLOCK_MONOTONIC timestamp (legacy function name)
 *   - memory_fence() - full memory barrier (dmb ish on ARM64, mfence on x86)
 *   - prefetch_l1() - L1 cache prefetch hint
 *   - compiler_barrier() - prevent compiler reordering only (no hw fence)
 *
 * All functions are inline static, with portable fallbacks via __builtin_*.
 */

#ifndef ASM_HELPERS_H
#define ASM_HELPERS_H

#include <stdatomic.h>
#include <stdint.h>
#include "common.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ============================================================
 * Legacy raw counters: not used by the portable elapsed-time path or scoring.
 * Callers must establish access, ordering and frequency themselves. There is
 * no SIGILL recovery here, and the PPC split read is not rollover-safe.
 *
 *   ARM64:     cntvct_el0   - Virtual Cycle Count. ~20ns read.
 *   x86_64:    rdtsc        - ~12ns read. Invariant TSC (constant_tsc) is required
 *                            for cycles to map to wall time across cores; most server
 *                            CPUs since Nehalem (2008) have it.
 *   RISC-V:    rdcycle      - User-mode cycle counter. Requires the Zicntr extension
 *                            (ratified in RV64G/RV32G). Reads CSR 0xC00. ~10-30ns.
 *                            Caveat: not all RISC-V cores expose it to U-mode; the
 *                            kernel may trap (SIGILL); no automatic fallback exists.
 *   PowerPC:   mftb         - Time Base register. 64-bit, ticks at bus/4 frequency.
 *                            Reads SPR 268. Requires -mno-ppc-relative-workaround.
 *   Other:     fall back to get_time_ns() from common.h.
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
#elif defined(__riscv) && (__riscv_xlen == 64)
    /* RISC-V 64-bit: rdcycle reads CSR 0xC00 (cycle). Requires Zicntr. */
    uint64_t v;
    __asm__ __volatile__("rdcycle %0" : "=r"(v));
    return v;
#elif defined(__powerpc__) || defined(__powerpc64__)
    /* PowerPC: mftb reads the Time Base lower 32 bits (SPR 268).
     * For 64-bit time, we'd need mfspr r, SPR_TB; some cores split into
     * TBL/TBU. Using 64-bit mfspr via register pair: */
    uint32_t lo, hi;
    __asm__ __volatile__("mfspr %0, 268" : "=r"(lo));
    __asm__ __volatile__("mfspr %0, 269" : "=r"(hi));
    return ((uint64_t)hi << 32) | lo;
#else
    /* Portable fallback: use get_time_ns() (1ns resolution is enough for our
     * fallback path; the loop will be slow but still correct). */
    return get_time_ns();
#endif
}

/* Portable elapsed-time clock. Raw cycle counters do not share a portable
 * frequency or access policy across ISAs. Time batches of dependent operations
 * with CLOCK_MONOTONIC instead; amortize its overhead, never divide by CPU MHz.
 * The legacy name remains for source compatibility; values are nanoseconds.
 */
static inline uint64_t rdtsc_ns(void) {
    atomic_signal_fence(memory_order_seq_cst);
    uint64_t ns = get_time_ns();
    atomic_signal_fence(memory_order_seq_cst);
    return ns;
}

/* ============================================================
 * Memory barriers
 * ============================================================ */

static inline void memory_fence(void) {
#if defined(__aarch64__)
    __asm__ __volatile__("dmb ish" ::: "memory");
#elif defined(__x86_64__) || defined(__i386__)
    __asm__ __volatile__("mfence" ::: "memory");
#elif defined(__riscv)
    /* RISC-V: 'fence iorw, iorw' = full fence on all modes (instruction + device
     * + memory, both read and write). The iorw is the strictest ordering. */
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
#elif defined(__powerpc__) || defined(__powerpc64__)
    __asm__ __volatile__("sync" ::: "memory");
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
    __asm__ __volatile__("clflush (%0)" :: "r"(p) : "memory");
#elif (defined(__APPLE__) || defined(_WIN32)) && defined(__aarch64__)
    /* No userspace per-line invalidation contract. Cache tests must use
     * the explicitly labelled capacity-eviction path, NOT this fence. */
    (void)p;
    memory_fence();
#elif defined(__aarch64__)
    /* DC CIVAC - Data Cache line Clean and Invalidate by VA to Point of Coherence.
     * On ARM64 this both flushes and invalidates, more aggressive than x86 clflush. */
    __asm__ __volatile__("dc civac, %0" :: "r"(p) : "memory");
#elif defined(__riscv)
    /* RISC-V: cflush.d.l1 cleans+invalidates L1 data cache line by VA.
     * Requires Zicbom extension (cache-block management). Fall back to
     * fence + compiler_barrier on cores without it. */
#if defined(__riscv_zicbom)
    __asm__ __volatile__(".word 0xfc00002e" :: "r"(p) : "memory");  /* cflush.d.l1 */
#else
    memory_fence();
    (void)p;
#endif
#elif defined(__powerpc__) || defined(__powerpc64__)
    /* PowerPC: dcbf (Data Cache Block Flush) invalidates a cache line.
     * Defined by the Power ISA as: "Discard the cache line containing EA
     * from all caches". */
    __asm__ __volatile__("dcbf %0, %1" :: "r"(p), "r"(0) : "memory");
#else
    /* No portable equivalent. Use a memory_fence() as a weak fallback. */
    memory_fence();
#endif
}

#ifdef __cplusplus
}
#endif

#endif /* ASM_HELPERS_H */
