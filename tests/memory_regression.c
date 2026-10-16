#include "../src/common.h"
static int fail_allocation, fail_creation, creations, allocations;
static void *test_malloc(size_t bytes) { ++allocations; return fail_allocation ? NULL : malloc(bytes); }
static int test_create(pthread_t *thread, const void *attributes, void *(*entry)(void *), void *argument) {
    if (++creations == 2 && fail_creation) return 1;
    return pthread_create(thread, attributes, entry, argument);
}
#define malloc test_malloc
#define pthread_create test_create
#define main memory_program_main
#include "../src/test_memory_bandwidth.c"
#undef main
#undef pthread_create
#undef malloc

static int latency_reference(void) {
    uintptr_t source[5 * 64 / sizeof(uintptr_t)], destination[5 * 64 / sizeof(uintptr_t)];
    /* Hand-authored cycle independent of chain_build: 0 -> 3 -> 1 -> 4 -> 2 -> 0. */
    const unsigned next[] = {3, 4, 0, 1, 2};
    const size_t words = 64 / sizeof(uintptr_t);
    for (unsigned op = 0; op < MEMORY_LAT_OPERATIONS; ++op) {
        MemoryLatency work = {{source, sizeof(source), (uintptr_t)source}, destination, op};
        memory_latency_initialize(&work);
        for (unsigned node = 0; node < 5; ++node)
            source[node * words] = (uintptr_t)(source + next[node] * words);
        if (memory_latency_work(&work, 3) || memory_latency_validate(&work)) return 20;
        printf("%u %zu %zu", op, words, (size_t)(work.chain.cursor - (uintptr_t)source) / 64);
        for (unsigned node = 0; node < 5; ++node) {
            printf(" %zu", (size_t)(source[node * words] - (uintptr_t)source) / 64);
            for (size_t word = 1; word < words; ++word)
                printf(" %llu", (unsigned long long)source[node * words + word]);
        }
        putchar('\n');
        source[2 * words + 1] ^= 1;
        if (!memory_latency_validate(&work)) return 21;
        source[2 * words + 1] ^= 1;
        if (op == MEMORY_LAT_COPY) {
            destination[3 * words + 2] ^= 1;
            if (!memory_latency_validate(&work)) return 22;
        }
    }
    return 0;
}

int main(int argc, char **argv) {
    global_system_config.detected = global_cache_config.detected = 1;
    global_system_config.cpu_cores = global_system_config.cpu_cores_physical = 2;
    if (argc > 1) {
        if (!strcmp(argv[1], "latency_reference")) return latency_reference();
        fail_allocation = !strcmp(argv[1], "allocation");
        fail_creation = !strcmp(argv[1], "creation");
        if (!strcmp(argv[1], "latency_only")) {
            memory_mode = 2;
            int status = run_memory_bandwidth_test(128 * KB, 2);
            if (allocations != 2 || creations != 0) return 15;
            return status;
        }
        if (!strcmp(argv[1], "bandwidth_only")) memory_mode = 1;
        return run_memory_bandwidth_test(128 * KB, 2);
    }
    double a[65], b[65], c[65];
    stream_initialize(a, b, c, 65);
    for (unsigned operation = 0; operation < STREAM_OPERATIONS; ++operation) {
        double result = stream_kernel(a, b, c, 65, operation, 3);
        if (stream_validate(c, 65, operation, 3, result)) return 11;
        printf("%u %llu %.17g", operation, (unsigned long long)stream_bytes(operation, 65), result);
        if (operation >= STREAM_WRITE && operation <= STREAM_TRIAD)
            for (unsigned i = 0; i < 65; ++i) printf(" %.17g", c[i]);
        putchar('\n');
        if (operation >= STREAM_WRITE && operation <= STREAM_TRIAD) {
            double saved = c[32];
            c[32] += 1;
            if (!stream_validate(c, 65, operation, 3, result)) return 12;
            c[32] = saved;
        } else if (!stream_validate(c, 65, operation, 3, result + 1)) return 13;
    }
    void *buffer = malloc(65 * 64);
    if (!buffer) return 1;
    chain_build(buffer, 65 * 64);
    uintptr_t cursor = (uintptr_t)buffer;
    for (unsigned i = 0; i < 65; ++i) {
        printf("%s%llu", i ? " " : "", (unsigned long long)((cursor - (uintptr_t)buffer) / 64));
        cursor = *(uintptr_t *)cursor;
    }
    putchar('\n');
    if (cursor != (uintptr_t)buffer) return 1;
    ChainWork work = {buffer, 65 * 64, (uintptr_t)buffer};
    if (chain_work(&work, 3)) return 1;
    printf("%llu\n", (unsigned long long)((work.cursor - (uintptr_t)buffer) / 64));
    free(buffer);
    return 0;
}
