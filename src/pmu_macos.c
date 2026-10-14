/* No supported public perf_event equivalent. Never fake PMU measurements. */
#include "common.h"
#include <errno.h>

PMUCacheCounters pmu_cache_counters = {-1, -1, -1, -1, -1, -1, -1};
int pmu_available = 0;
int pmu_init_cache_counters(void) { return -1; }
int perf_init_counters(void) { return -1; }
void pmu_reset_counters(void) {}
void pmu_disable_counters(void) {}
uint64_t pmu_read_counter(int fd) { (void)fd; return 0; }
uint64_t pmu_measure_cache_latency(void *ptr, int level) { (void)ptr; (void)level; return 0; }
long sys_perf_event_open(struct perf_event_attr *attr, pid_t pid, int cpu, int group, unsigned long flags) {
    (void)attr; (void)pid; (void)cpu; (void)group; (void)flags;
    errno = ENOTSUP;
    return -1;
}
int perf_measure(void (*func)(void *), void *arg, PerfResult *result) {
    (void)func; (void)arg;
    if (result) memset(result, 0, sizeof(*result));
    return -1; /* callers run their own wall-time fallback */
}
void perf_print_result(const PerfResult *result, int show_pmu) {
    (void)result; (void)show_pmu;
    puts("  PMU: unavailable on this OS backend (no retired-instruction IPC)");
}
