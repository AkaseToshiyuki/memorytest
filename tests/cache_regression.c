/* Native end-to-end cache/report regression with <=128 KiB working sets. */
#include "../src/common.h"
#include <errno.h>
static int fail_alloc;
static size_t available = 128 * MB;
static void *test_malloc(size_t bytes) { return fail_alloc ? NULL : malloc(bytes); }
static size_t test_available(void) { return available; }
#define malloc test_malloc
#define detect_available_memory test_available
#define main cache_program_main
#include "../src/test_cache_hierarchy.c"
#undef main
#undef malloc
#undef detect_available_memory

int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "transitions")) {
        CacheTestResult rows[5];
        double values[] = {2, 2, 4, 4.2, 4.1};
        for (unsigned i = 0; i < 5; ++i) {
            rows[i] = (CacheTestResult){.size = (8 * KB) << i, .ns = values[i],
                .latency = {1, (uint64_t)(values[i] * 256), (uint64_t)(values[i] * 250), (uint64_t)(values[i] * 262)}};
        }
        if (!cache_transition(rows, 5, 2) || cache_transition(rows, 5, 1) || cache_transition(rows, 5, 4)) return 1;
        rows[3].ns = 2; /* single-point spike must not become a cache boundary */
        if (cache_transition(rows, 5, 2)) return 2;
        rows[3].ns = 4.2;
        rows[1].latency.max_ns = 1200; /* overlapping noisy samples */
        if (cache_transition(rows, 5, 2)) return 3;
        for (unsigned i = 0; i < 5; ++i) {
            rows[i].latency.statistics.count = 5;
            rows[i].latency.statistics.q1 = rows[i].ns * 250;
            rows[i].latency.statistics.q3 = rows[i].ns * 262;
        }
        /* A single extreme raw maximum must not suppress separated IQR bands. */
        if (!cache_transition(rows, 5, 2)) return 4;
        rows[2].latency.statistics.q1 = rows[1].latency.statistics.q3;
        if (cache_transition(rows, 5, 2)) return 5;
        rows[2].latency.statistics.q1 = rows[2].ns * 250;
        rows[3].latency.statistics.unstable = 1;
        if (cache_transition(rows, 5, 2)) return 6;
        return 0;
    }
    global_system_config.detected = 1;
    global_system_config.cpu_cores = 1;
    global_system_config.cpu_cores_physical = 1;
    global_cache_config.detected = 1;
    global_cache_config.l1d_size = 16 * KB;
    global_cache_config.l2_size = 64 * KB;
    global_cache_config.l3_size = 0;
    cache_max_bytes = 128 * KB;
    cache_thread_limit = 1;
    cache_quick = 1;
    if (argc > 1 && !strcmp(argv[1], "sparse")) cache_max_bytes = 24 * KB;
    if (argc > 1 && !strcmp(argv[1], "missing")) {
        global_cache_config.l1d_size = global_cache_config.l2_size = 0;
    }
    if (argc > 1 && !strcmp(argv[1], "allocation")) fail_alloc = 1;
    if (argc > 1 && !strcmp(argv[1], "memory")) available = 0;
    if (argc > 1 && !strcmp(argv[1], "bandwidth_only")) cache_mode = 1;
    if (argc > 1 && !strcmp(argv[1], "latency_only")) cache_mode = 2;
    if (argc > 1 && !strcmp(argv[1], "detection_only")) cache_mode = 3;
    return run_cache_hierarchy_test();
}
