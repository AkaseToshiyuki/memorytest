#ifndef MEMORY_LATENCY_H
#define MEMORY_LATENCY_H
#include "memory_kernels.h"
#include <stdatomic.h>

/* These are defined dependent transactions, NOT inverse bandwidth or isolated
 * DRAM store latency. A write reads the next address, writes one 64-B node and
 * orders it before the next transaction. Copy transfers 64 B to a separate
 * allocation, orders the stores, then obtains the next source address from
 * the destination. The full fence costs are intentionally included. */
enum { MEMORY_LAT_READ, MEMORY_LAT_WRITE, MEMORY_LAT_COPY, MEMORY_LAT_OPERATIONS };
static const char *const memory_latency_names[MEMORY_LAT_OPERATIONS] = {
    "Dependent random read", "Dependent random write 64B (ordered)",
    "Dependent random copy 64B (ordered)"
};
typedef struct {
    ChainWork chain;
    void *destination;
    unsigned operation;
} MemoryLatency;

static uintptr_t latency_payload(size_t node, size_t word, int written) {
    return ((uintptr_t)node * 17 + word) ^ (written ? UINT64_C(0x5a5aa5a5) : UINT64_C(0x13579bdf));
}

static void memory_latency_initialize(MemoryLatency *work) {
    chain_build(work->chain.buffer, work->chain.bytes);
    for (size_t node = 0; node < work->chain.bytes / 64; ++node) {
        uintptr_t *words = (uintptr_t *)((unsigned char *)work->chain.buffer + node * 64);
        for (size_t word = 1; word < 64 / sizeof(uintptr_t); ++word)
            words[word] = latency_payload(node, word, 0);
    }
    if (work->operation == MEMORY_LAT_COPY) memset(work->destination, 0, work->chain.bytes);
    work->chain.cursor = (uintptr_t)work->chain.buffer;
}

static int memory_latency_work(void *context, uint64_t blocks) {
    MemoryLatency *work = context;
    if (work->operation == MEMORY_LAT_READ) return chain_work(&work->chain, blocks);
    if (work->operation >= MEMORY_LAT_OPERATIONS ||
        (work->operation == MEMORY_LAT_COPY && !work->destination)) return -1;
    uintptr_t cursor = work->chain.cursor, base = (uintptr_t)work->chain.buffer;
    for (uint64_t block = 0; block < blocks; ++block) {
        for (unsigned step = 0; step < CHAIN_STEPS; ++step) {
            volatile uintptr_t *source = (volatile uintptr_t *)cursor;
            size_t offset = cursor - base;
            if (work->operation == MEMORY_LAT_WRITE) {
                uintptr_t next = source[0];
                source[0] = next;
                for (size_t word = 1; word < 64 / sizeof(uintptr_t); ++word)
                    source[word] = latency_payload(offset / 64, word, 1);
                atomic_thread_fence(memory_order_seq_cst);
                cursor = next;
            } else {
                volatile uintptr_t *destination = (volatile uintptr_t *)((unsigned char *)work->destination + offset);
                for (size_t word = 0; word < 64 / sizeof(uintptr_t); ++word)
                    destination[word] = source[word];
                atomic_thread_fence(memory_order_seq_cst);
                cursor = destination[0];
            }
        }
    }
    chain_sink = cursor;
    work->chain.cursor = cursor;
    return cursor < base || cursor >= base + work->chain.bytes || (cursor - base) % 64 ? -1 : 0;
}

/* Untimed: validate every payload word, the separate copied allocation and a
 * full cycle (no short subcycle). No second invocation of the timed kernel. */
static int memory_latency_validate(const MemoryLatency *work) {
    uintptr_t base = (uintptr_t)work->chain.buffer, cursor = base;
    size_t nodes = work->chain.bytes / 64;
    for (size_t node = 0; node < nodes; ++node) {
        const uintptr_t *source = (const uintptr_t *)(base + node * 64);
        for (size_t word = 1; word < 64 / sizeof(uintptr_t); ++word)
            if (source[word] != latency_payload(node, word, work->operation == MEMORY_LAT_WRITE)) return -1;
        if (work->operation == MEMORY_LAT_COPY &&
            memcmp(source, (const unsigned char *)work->destination + node * 64, 64)) return -1;
        if (cursor < base || cursor >= base + work->chain.bytes || (cursor - base) % 64) return -1;
        cursor = *(const uintptr_t *)cursor;
        if (cursor == base && node + 1 != nodes) return -1;
    }
    return cursor == base ? 0 : -1;
}
#endif
