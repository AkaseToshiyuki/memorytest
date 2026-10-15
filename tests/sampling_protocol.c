/* Independent scripted clock: no hardware timing or production clock code. */
#define BENCH_TARGET_NS 100
#define BENCH_WARMUP_NS 50
#ifndef BENCH_SAMPLES
#define BENCH_SAMPLES 5
#endif
#include "../src/common.h"
static uint64_t scripted_time(void);
#define get_time_ns scripted_time
#include "../src/bench_protocol.h"
#undef get_time_ns
#include <stdio.h>
#include <string.h>

static uint64_t clock_value = 10000;
static unsigned calls;
static const char *mode;
static uint64_t scripted_time(void) { return clock_value; }
static int kernel(void *unused, uint64_t count) {
    (void)unused;
    ++calls;
    if (!strcmp(mode, "error")) return -1;
    if (!strcmp(mode, "backward")) { --clock_value; return 0; }
    if (!strcmp(mode, "stalled")) return 0;
    if (!strcmp(mode, "even")) {
        clock_value += calls == 1 ? 50 : calls == 2 ? 100 : 100 + calls - 3;
        return 0;
    }
    /* Cold start plus one calibration descheduling spike. The measured work
     * has an exact independent cost of two ticks per repetition. */
    clock_value += calls <= 2 ? 1000 : count * 2;
    return 0;
}
int main(int argc, char **argv) {
    mode = argc > 1 ? argv[1] : "jitter";
    BenchMeasurement result = {0};
    int status = bench_measure(kernel, NULL, &result);
    printf("%d %u %llu %.17g %.17g %.17g\n", status, calls,
        (unsigned long long)result.repetitions, result.elapsed_ns, result.min_ns, result.max_ns);
    return 0;
}
