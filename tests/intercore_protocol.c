/* Two workers only; completion/count correctness, not a topology benchmark. */
#define NUM_SAMPLES 3
#define ROUND_TRIPS_PER_SAMPLE 64
#define main intercore_program_main
#include "../src/test_inter_core.c"
#undef main
int main(void) {
    if (worker_pool_init(2)) return 1;
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
    puts("completion-counts-ok");
    return 0;
}
