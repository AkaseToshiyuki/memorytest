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
    return run_cache_hierarchy_test();
}
