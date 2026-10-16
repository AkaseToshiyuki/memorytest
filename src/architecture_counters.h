#ifndef ARCHITECTURE_COUNTERS_H
#define ARCHITECTURE_COUNTERS_H
#include <errno.h>
/* Raw hardware counts only. Never substitute TSC, CNTVCT, advertised MHz,
 * or QueryThreadCycleTime for actual core cycles. No system-wide PMU writes. */
#ifdef __linux__
#include <sys/syscall.h>
#if defined(__x86_64__)
#include <cpuid.h>
#endif
#elif defined(__APPLE__)
#include <libproc.h>
#include <sys/resource.h>
#endif
typedef struct { uint64_t cycles, instructions, enabled, running, mite; } ArchCounts;
typedef struct {
    int cycles, instructions;
    const char *source;
    int mite;
    const char *decode_source;
    const char *status;
    int error;
} ArchCounter;
static int arch_counter_read(const ArchCounter *counter, ArchCounts *out) {
    memset(out, 0, sizeof(*out));
#ifdef __linux__
    /* PERF_FORMAT_GROUP: nr, time_enabled, time_running, value[2 or 3]. */
    uint64_t raw[6];
    size_t count = counter->mite >= 0 ? 3 : 2, bytes = (count + 3) * sizeof(uint64_t);
    if (counter->cycles < 0 || read(counter->cycles, raw, bytes) != (ssize_t)bytes || raw[0] != count) return -1;
    *out = (ArchCounts){raw[3], raw[4], raw[1], raw[2], count == 3 ? raw[5] : 0};
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
    if (counter->mite >= 0) close(counter->mite);
    if (counter->instructions >= 0) close(counter->instructions);
    if (counter->cycles >= 0) close(counter->cycles);
#endif
    counter->cycles = counter->instructions = counter->mite = -1;
}
static int arch_has_skl_mite_event(void) {
#if defined(__linux__) && defined(__x86_64__)
    unsigned a, b, c, d;
    if (!__get_cpuid(0, &a, &b, &c, &d) || b != 0x756e6547 || d != 0x49656e69 || c != 0x6c65746e) return 0;
    if (!__get_cpuid(1, &a, &b, &c, &d) || (c & (1u << 31)) || ((a >> 8) & 15) != 6) return 0;
    unsigned model = ((a >> 4) & 15) | ((a >> 12) & 0xf0);
    /* Intel perfmon SKL/KBL: IDQ.MITE_UOPS, event 79H, umask 04H.
     * No guessed raw events on other models or virtual PMUs. */
    return model == 0x4e || model == 0x5e || model == 0x8e || model == 0x9e;
#else
    return 0;
#endif
}
static ArchCounter arch_counter_open(void) {
    ArchCounter counter = {-1, -1, "unavailable", -1, "unavailable", "platform_unsupported", 0};
#ifdef __linux__
    struct perf_event_attr attr = {.size = sizeof(attr), .type = PERF_TYPE_HARDWARE,
        .config = PERF_COUNT_HW_CPU_CYCLES, .exclude_kernel = 1, .exclude_hv = 1,
        .read_format = PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING};
    counter.cycles = (int)syscall(__NR_perf_event_open, &attr, 0, -1, -1, PERF_FLAG_FD_CLOEXEC);
    if (counter.cycles < 0) counter.error = errno;
    if (counter.cycles >= 0) {
        attr.config = PERF_COUNT_HW_INSTRUCTIONS;
        counter.instructions = (int)syscall(__NR_perf_event_open, &attr, 0, -1, counter.cycles, PERF_FLAG_FD_CLOEXEC);
        if (counter.instructions >= 0) { counter.source = "linux-perf-thread-user"; counter.status = "available"; }
        else { counter.error = errno; arch_counter_close(&counter); }
        if (counter.instructions >= 0 && arch_has_skl_mite_event()) {
            attr.type = PERF_TYPE_RAW; attr.config = 0x0479;
            counter.mite = (int)syscall(__NR_perf_event_open, &attr, 0, -1, counter.cycles, PERF_FLAG_FD_CLOEXEC);
            if (counter.mite >= 0) counter.decode_source = "intel-skl-kbl-idq-mite-uops";
        }
    }
    if (counter.error) counter.status = counter.error == EACCES || counter.error == EPERM ? "permission_denied" : "event_unavailable";
#elif defined(__APPLE__) && defined(RUSAGE_INFO_V4)
    ArchCounts value;
    if (arch_counter_read(&counter, &value)) { counter.status = "read_failed"; counter.error = errno; }
    else if (value.cycles && value.instructions) { counter.source = "macos-rusage-process-user-and-kernel"; counter.status = "available"; }
    else counter.status = "zero_counts";
#endif
    return counter;
}
static int arch_counter_delta(const ArchCounts *a, const ArchCounts *b, ArchCounts *delta) {
    if (b->cycles <= a->cycles || b->instructions <= a->instructions ||
        b->enabled < a->enabled || b->running < a->running || b->mite < a->mite) return -1;
    *delta = (ArchCounts){b->cycles-a->cycles, b->instructions-a->instructions,
        b->enabled-a->enabled, b->running-a->running, b->mite-a->mite};
    /* Multiplexed samples are unusable for width estimates; do not scale. */
    if (delta->enabled != delta->running) return -1;
    return 0;
}
#endif
