#ifndef MEMORY_KERNELS_H
#define MEMORY_KERNELS_H
#include "common.h"
#include <math.h>
#include <float.h>

enum { STREAM_READ, STREAM_WRITE, STREAM_COPY, STREAM_SCALE, STREAM_ADD,
       STREAM_TRIAD, STREAM_STRIDE64, STREAM_STRIDE256, STREAM_OPERATIONS };
static const char *const stream_names[STREAM_OPERATIONS] = {
    "Read", "Write", "Copy", "Scale", "Add", "Triad", "Stride 64B", "Stride 256B"
};

/* Bytes explicitly loaded/stored by the kernel, not inferred bus traffic. */
static uint64_t stream_bytes(unsigned operation, size_t words) {
    if (operation == STREAM_STRIDE64) return ((words + 7) / 8) * 8;
    if (operation == STREAM_STRIDE256) return ((words + 31) / 32) * 8;
    unsigned arrays = operation <= STREAM_WRITE ? 1 : operation <= STREAM_SCALE ? 2 : 3;
    return (uint64_t)words * sizeof(double) * arrays;
}

static void stream_initialize(double *a, double *b, double *c, size_t words) {
    for (size_t i = 0; i < words; ++i) {
        a[i] = 1 + (i % 8) * 0.125;
        b[i] = 2 + (i % 4) * 0.25;
        c[i] = 0;
    }
}

static double stream_kernel(const double *restrict a, const double *restrict b,
                            double *restrict c, size_t words, unsigned operation, uint64_t passes) {
    double sum0 = 0, sum1 = 0, sum2 = 0, sum3 = 0;
    for (uint64_t pass = 0; pass < passes; ++pass) {
        /* Each pass must access memory. This also prevents collapsing repeated
         * writes/copies or hoisting a repeated read into a single traversal. */
        __asm__ volatile("" : : "r"(a), "r"(b), "r"(c) : "memory");
        switch (operation) {
        case STREAM_READ: {
            size_t i = 0;
            for (; i + 3 < words; i += 4) {
                sum0 += a[i]; sum1 += a[i + 1]; sum2 += a[i + 2]; sum3 += a[i + 3];
            }
            for (; i < words; ++i) sum0 += a[i];
            break;
        }
        case STREAM_WRITE: for (size_t i = 0; i < words; ++i) c[i] = 3.0; break;
        case STREAM_COPY: memcpy(c, a, words * sizeof(double)); break;
        case STREAM_SCALE: for (size_t i = 0; i < words; ++i) c[i] = 3.0 * a[i]; break;
        case STREAM_ADD: for (size_t i = 0; i < words; ++i) c[i] = a[i] + b[i]; break;
        case STREAM_TRIAD: for (size_t i = 0; i < words; ++i) c[i] = a[i] + 3.0 * b[i]; break;
        default: {
            size_t stride = operation == STREAM_STRIDE64 ? 8 : 32;
            for (size_t i = 0; i < words; i += stride) sum0 += a[i];
            break;
        }
        }
        __asm__ volatile("" : : "r"(c) : "memory");
    }
    return operation == STREAM_READ || operation >= STREAM_STRIDE64
            ? sum0 + sum1 + sum2 + sum3 : c[words - 1];
}

/* Untimed validation against the input definition, not a second invocation of
 * the measured kernel. Check every written word (including middle corruption),
 * and derive read/stride reductions from the eight-element input period. */
static int stream_validate(const double *c, size_t words, unsigned operation,
                           uint64_t passes, double result) {
    if (!words || !passes || operation >= STREAM_OPERATIONS || !isfinite(result)) return -1;
    if (operation == STREAM_READ || operation >= STREAM_STRIDE64) {
        long double expected;
        if (operation == STREAM_READ) {
            expected = (long double)(words / 8) * 11.5L;
            for (size_t i = 0; i < words % 8; ++i) expected += 1.0L + i * .125L;
        } else {
            size_t stride = operation == STREAM_STRIDE64 ? 8 : 32;
            expected = (words + stride - 1) / stride;
        }
        expected *= passes;
        return fabsl((long double)result - expected) <= expected * 64 * DBL_EPSILON ? 0 : -1;
    }
    for (size_t i = 0; i < words; ++i) {
        double a = 1.0 + (i % 8) * .125, b = 2.0 + (i % 4) * .25, expected;
        switch (operation) {
        case STREAM_WRITE: expected = 3; break;
        case STREAM_COPY: expected = a; break;
        case STREAM_SCALE: expected = 3 * a; break;
        case STREAM_ADD: expected = a + b; break;
        default: expected = a + 3 * b; break;
        }
        /* Inputs and all outputs here are exactly representable in binary64. */
        if (c[i] != expected) return -1;
    }
    return 0;
}

static uint64_t chain_random(uint64_t *state) {
    uint64_t x = *state;
    x ^= x >> 12; x ^= x << 25; x ^= x >> 27;
    *state = x;
    return x * UINT64_C(2685821657736338717);
}

/* Insert each new cache-line node into one existing cycle. Every node belongs
 * to the same permutation cycle; no auxiliary allocation or short subcycles.
 * Fixed 64-byte node spacing on all platforms; ordinary OS pages, no flush. */
static void chain_build(void *buffer, size_t bytes) {
    unsigned char *base = buffer;
    size_t nodes = bytes / 64;
    uint64_t random = UINT64_C(0x243f6a8885a308d3);
    *(uintptr_t *)base = (uintptr_t)base;
    for (size_t i = 1; i < nodes; ++i) {
        uintptr_t *insert = (uintptr_t *)(base + i * 64);
        uintptr_t *prior = (uintptr_t *)(base + (chain_random(&random) % i) * 64);
        *insert = *prior;
        *prior = (uintptr_t)insert;
    }
}

typedef struct { void *buffer; size_t bytes; uintptr_t cursor; } ChainWork;
static volatile uintptr_t chain_sink;
enum { CHAIN_STEPS = 256 };
static int chain_work(void *context, uint64_t blocks) {
    ChainWork *work = context;
    uintptr_t cursor = work->cursor;
    for (uint64_t i = 0; i < blocks; ++i)
        for (unsigned j = 0; j < CHAIN_STEPS / 8; ++j) {
#define CHASE cursor = *(volatile uintptr_t *)cursor
            CHASE; CHASE; CHASE; CHASE; CHASE; CHASE; CHASE; CHASE;
#undef CHASE
        }
    chain_sink = cursor;
    work->cursor = cursor;
    uintptr_t start = (uintptr_t)work->buffer;
    return cursor < start || cursor >= start + work->bytes || (cursor - start) % 64 ? -1 : 0;
}
#endif
