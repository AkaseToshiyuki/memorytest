/* Bounded macOS API/eviction check; no full memory scan or stress run. */
#define main cache_program_main
#include "../src/test_cache_hierarchy.c"
#undef main
#include "../src/platform.h"
#include "../src/darwin_helpers.h"
#include <errno.h>
#include <inttypes.h>

int main(void) {
    platform_set_non_interactive(true);
    init_platform_layer();
    if (set_cpu_affinity(0) != ENOTSUP || perf_init_counters() != -1) return 1;
    if (platform_pmu_probe()->available) return 2;
#if defined(__aarch64__)
    if (!platform_has_neon()) return 3;
#endif
    size_t total = detect_total_memory(), available = detect_available_memory();
    if (!total || !available || available > total) return 4;
    printf("MEMORY_BYTES=%zu\n", total);
    printf("CACHE_L2=%zu\n", global_cache_config.l2_size);
    printf("CACHE_L3=%zu\n", global_cache_config.l3_size);
    printf("CACHE_L2_API=%" PRIu64 "\n", darwin_uint("hw.l2cachesize"));
    printf("CACHE_L3_API=%" PRIu64 "\n", darwin_uint("hw.l3cachesize"));
    DarwinPerfLevel levels[DARWIN_MAX_PERF_LEVELS];
    size_t count = darwin_perflevels(levels, DARWIN_MAX_PERF_LEVELS);
    uint64_t reported = darwin_uint("hw.nperflevels");
    if (count != (reported < DARWIN_MAX_PERF_LEVELS ? reported : DARWIN_MAX_PERF_LEVELS)) return 7;
    for (size_t i = 0; i < count; ++i) {
        char key[96];
        snprintf(key, sizeof(key), "hw.perflevel%zu.physicalcpu", i);
        if (levels[i].physical != darwin_uint(key) || !levels[i].name[0]) return 8;
    }
    void *buffer = malloc(16 * KB);
    if (!buffer) return 5;
    double latency = measure_latency(buffer, 16 * KB, 32768);
    free(buffer);
    if (!isfinite(latency) || latency <= 0) return 6;
    /* Exercises partial creation/abort separately in test_compute. */
    return 0;
}
