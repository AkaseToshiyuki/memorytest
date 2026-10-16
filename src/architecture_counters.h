#ifndef ARCHITECTURE_COUNTERS_H
#define ARCHITECTURE_COUNTERS_H
/* Raw hardware counts only. Never substitute TSC, CNTVCT, advertised MHz,
 * or QueryThreadCycleTime for actual core cycles. No system-wide PMU writes. */
#ifdef __linux__
#include <sys/syscall.h>
#elif defined(__APPLE__)
#include <libproc.h>
#include <sys/resource.h>
#endif
typedef struct { uint64_t cycles, instructions, enabled, running; } ArchCounts;
typedef struct { int cycles, instructions; const char *source; } ArchCounter;
static int arch_counter_read(const ArchCounter *counter, ArchCounts *out) {
    memset(out, 0, sizeof(*out));
#ifdef __linux__
    /* PERF_FORMAT_GROUP: nr, time_enabled, time_running, value[2]. */
    uint64_t raw[5];
    if (counter->cycles < 0 || read(counter->cycles, raw, sizeof(raw)) != sizeof(raw) || raw[0] != 2) return -1;
    *out = (ArchCounts){raw[3], raw[4], raw[1], raw[2]};
    return 0;
#elif defined(__APPLE__) && defined(RUSAGE_INFO_V4)
    struct rusage_info_v4 usage;
    if (proc_pid_rusage(getpid(), RUSAGE_INFO_V4, (rusage_info_t *)&usage)) return -1;
    *out = (ArchCounts){.cycles = usage.ri_cycles, .instructions = usage.ri_instructions};
    return 0;
#else
    (void)counter;
    return -1;
#endif
}
static void arch_counter_close(ArchCounter *counter) {
#ifdef __linux__
    if (counter->instructions >= 0) close(counter->instructions);
    if (counter->cycles >= 0) close(counter->cycles);
#endif
    counter->cycles = counter->instructions = -1;
}
static ArchCounter arch_counter_open(void) {
    ArchCounter counter = {-1, -1, "unavailable"};
#ifdef __linux__
    struct perf_event_attr attr = {.size = sizeof(attr), .type = PERF_TYPE_HARDWARE,
        .config = PERF_COUNT_HW_CPU_CYCLES, .exclude_kernel = 1, .exclude_hv = 1,
        .read_format = PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING};
    counter.cycles = (int)syscall(__NR_perf_event_open, &attr, 0, -1, -1, PERF_FLAG_FD_CLOEXEC);
    if (counter.cycles >= 0) {
        attr.config = PERF_COUNT_HW_INSTRUCTIONS;
        counter.instructions = (int)syscall(__NR_perf_event_open, &attr, 0, -1, counter.cycles, PERF_FLAG_FD_CLOEXEC);
        if (counter.instructions >= 0) counter.source = "linux-perf-thread-user";
        else arch_counter_close(&counter);
    }
#elif defined(__APPLE__) && defined(RUSAGE_INFO_V4)
    ArchCounts value;
    if (!arch_counter_read(&counter, &value) && value.cycles && value.instructions)
        counter.source = "macos-rusage-process-user-and-kernel";
#endif
    return counter;
}
static int arch_counter_delta(const ArchCounts *a, const ArchCounts *b, ArchCounts *delta) {
    if (b->cycles <= a->cycles || b->instructions <= a->instructions ||
        b->enabled < a->enabled || b->running < a->running) return -1;
    *delta = (ArchCounts){b->cycles-a->cycles, b->instructions-a->instructions,
        b->enabled-a->enabled, b->running-a->running};
    /* Multiplexed samples are unusable for width estimates; do not scale. */
    if (delta->enabled != delta->running) return -1;
    return 0;
}
#endif
