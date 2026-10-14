#include "../src/common.h"
static int fail_allocation, fail_creation, creations;
static void *test_malloc(size_t bytes) { return fail_allocation ? NULL : malloc(bytes); }
static int test_create(pthread_t *thread, const void *attributes, void *(*entry)(void *), void *argument) {
    if (fail_creation && creations++ == 1) return 1;
    return pthread_create(thread, attributes, entry, argument);
}
#define malloc test_malloc
#define pthread_create test_create
#define main memory_program_main
#include "../src/test_memory_bandwidth.c"
#undef main
#undef pthread_create
#undef malloc

int main(int argc, char **argv) {
    global_system_config.detected = global_cache_config.detected = 1;
    global_system_config.cpu_cores = global_system_config.cpu_cores_physical = 2;
    if (argc > 1) {
        fail_allocation = !strcmp(argv[1], "allocation");
        fail_creation = !strcmp(argv[1], "creation");
        return run_memory_bandwidth_test(128 * KB, 2);
    }
    double a[65], b[65], c[65];
    stream_initialize(a, b, c, 65);
    for (unsigned operation = 0; operation < STREAM_OPERATIONS; ++operation) {
        double result = stream_kernel(a, b, c, 65, operation, 3);
        printf("%u %llu %.17g", operation, (unsigned long long)stream_bytes(operation, 65), result);
        if (operation >= STREAM_WRITE && operation <= STREAM_TRIAD)
            for (unsigned i = 0; i < 65; ++i) printf(" %.17g", c[i]);
        putchar('\n');
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
