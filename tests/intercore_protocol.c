/* Two workers only; completion/count correctness, not a topology benchmark. */
#define _GNU_SOURCE
#include <sched.h>
#include <pthread.h>
static int fail_bind, bind_calls, fail_create, create_calls;
static int test_bind(pid_t pid, size_t bytes, const cpu_set_t *set) {
    if (fail_bind && ++bind_calls == 2) return -1;
    return sched_setaffinity(pid, bytes, set);
}
static int test_create(pthread_t *thread, const pthread_attr_t *attr, void *(*entry)(void *), void *arg) {
    if (fail_create && ++create_calls == 2) return 1;
    return pthread_create(thread, attr, entry, arg);
}
#define sched_setaffinity test_bind
#define pthread_create test_create
#define NUM_SAMPLES 3
#define BW_SAMPLES 3
#define ROUND_TRIPS_PER_SAMPLE 64
#define main intercore_program_main
#include "../src/test_inter_core.c"
#undef main
#undef sched_setaffinity
#undef pthread_create
int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "matrix")) {
        global_system_config.detected = global_cache_config.detected = 1;
        global_system_config.cpu_cores = global_system_config.cpu_cores_physical = 2;
        g_bw_iterations = 800000; /* fixed upper bound; only two allowed CPUs */
        return run_inter_core_latency_test();
    }
    if (argc > 1) {
        fail_bind = !strcmp(argv[1], "binding");
        fail_create = !strcmp(argv[1], "creation");
        if (!worker_pool_init(2) || g_workers || g_created_workers) return 8;
        puts("failure-cleanup-ok"); return 0;
    }
    if (worker_pool_init(2)) return 1;
    if (!g_workers[0].affinity_ok || !g_workers[1].affinity_ok) return 6;
    printf("cpu-ids %d %d\n", g_workers[0].core_id, g_workers[1].core_id);
    g_bw_iterations = 128;
    warm_pair_bandwidth(0, 1);
    if (g_bw_iterations != 128) return 2;
    for (unsigned i = 0; i < 80; ++i) {
        dispatch_pair(0, 1, WORKER_BUSY_BANDWIDTH);
        if (g_workers[0].cas_ops != 128 || g_workers[1].cas_ops != 128) return 3;
        if (atomic_load(&g_workers[0].ready_to_run) || atomic_load(&g_workers[1].ready_to_run)) return 4;
    }
    dispatch_pair(0, 1, WORKER_BUSY_LATENCY);
    if (g_workers[0].n_samples != 3 || g_workers[1].n_samples != 3) return 5;
    worker_pool_destroy();
    puts("completion-counts-ok");
    return 0;
}
