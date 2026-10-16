#define _GNU_SOURCE
#include "../src/common.h"
#include <stdatomic.h>
#include <errno.h>
#include <time.h>
static int mode, creations;
static _Thread_local unsigned cpu, reads;
static unsigned obs_logical_cpus(void) { return 2; }
static int obs_probe(void) { return mode == 5 ? -1 : 0; }
static int obs_set_qos(unsigned index) { cpu = 101 + index * 3; return mode == 6 ? -1 : 0; }
static uint64_t obs_cpu_token(void) {
    if (mode == 1) return 101;
    if (mode == 2) return cpu + (++reads % 2);
    if (mode == 3) { struct timespec t = {0, 150000000}; nanosleep(&t, NULL); }
    return cpu;
}
static void obs_platform_info(ReportContext *report) { report_write(report, "- Test fixture: not hardware CPU IDs\n"); }
static int test_create(pthread_t *thread, const void *attributes, void *(*entry)(void *), void *argument) {
    if (++creations == 2 && mode == 4) return 1;
    return pthread_create(thread, attributes, entry, argument);
}
#define pthread_create test_create
#include "../src/intercore_observed.h"
#undef pthread_create
int main(int argc, char **argv) {
    mode = argc > 1 ? atoi(argv[1]) : 0;
    global_system_config.detected = global_cache_config.detected = 1;
    char *args[] = {argv[0], "--budget-ms", "800", "--workers", "2"};
    return observed_main(5, args);
}
