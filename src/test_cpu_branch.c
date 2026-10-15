/* Identical, non-if-convertible data branch for every input pattern.
 * Pattern sensitivity, not predictor accuracy or BTB capacity. Timing includes
 * the byte load, loop branch and taken-path work. No PMU accuracy is inferred. */
#include "common.h"
#include "util.h"
#include "bench_protocol.h"

#ifndef BRANCH_PATTERN_SIZE
#define BRANCH_PATTERN_SIZE (1024 * 1024)
#endif
static volatile uint64_t branch_sink;

__attribute__((noinline)) static uint64_t branch_count(const unsigned char *data, size_t count) {
    uint64_t ones = 0;
    if (!count) return 0;
#if defined(__x86_64__)
    __asm__ volatile(
        "1: cmpb $0, (%[p])\n\tje 2f\n\taddq $1, %[s]\n\t"
        "2: addq $1, %[p]\n\tsubq $1, %[n]\n\tjne 1b\n\t"
        : [p] "+&r" (data), [n] "+&r" (count), [s] "+&r" (ones)
        : : "cc", "memory");
#elif defined(__aarch64__)
    unsigned value;
    __asm__ volatile(
        "1: ldrb %w[v], [%[p]], #1\n\tcbz %w[v], 2f\n\tadd %[s], %[s], #1\n\t"
        "2: subs %[n], %[n], #1\n\tb.ne 1b\n\t"
        : [p] "+&r" (data), [n] "+&r" (count), [s] "+&r" (ones), [v] "=&r" (value)
        : : "cc", "memory");
#else
    (void)data; (void)count;
#endif
    return ones;
}

static uint64_t pattern_random(uint64_t *state) {
    uint64_t x = *state;
    x ^= x >> 12; x ^= x << 25; x ^= x >> 27;
    *state = x;
    return x * UINT64_C(2685821657736338717);
}

enum { ALWAYS, NEVER, ALTERNATING, PAIRS, BLOCK256, BLOCK1024, RANDOM50, PATTERNS };
static const char *pattern_names[PATTERNS] = {
    "Always Taken", "Never Taken", "Alternating", "TTNN", "Pattern 256", "Pattern 1K", "Random 50%"
};

static uint64_t make_pattern(unsigned char *data, size_t count, unsigned pattern) {
    for (size_t i = 0; i < count; ++i) {
        switch (pattern) {
        case ALWAYS: data[i] = 0; break; /* cbz/je is taken for zero */
        case NEVER: data[i] = 1; break;
        case ALTERNATING: data[i] = i % 2; break;
        case PAIRS: data[i] = (i / 2) % 2; break;
        case BLOCK256: data[i] = (i / 256) % 2; break;
        case BLOCK1024: data[i] = (i / 1024) % 2; break;
        default: data[i] = i % 2; break;
        }
    }
    if (pattern == RANDOM50) {
        uint64_t seed = UINT64_C(0x243f6a8885a308d3);
        for (size_t i = count; i > 1; --i) {
            size_t j = (size_t)(pattern_random(&seed) % i);
            unsigned char temporary = data[i - 1]; data[i - 1] = data[j]; data[j] = temporary;
        }
    }
    uint64_t ones = 0;
    for (size_t i = 0; i < count; ++i) ones += data[i] != 0;
    return ones;
}

typedef struct { const unsigned char *data; size_t count; uint64_t expected; } BranchWork;
static int branch_work(void *context, uint64_t repetitions) {
    BranchWork *work = context;
    uint64_t sum = 0;
    for (uint64_t i = 0; i < repetitions; ++i) sum += branch_count(work->data, work->count);
    branch_sink = sum;
    return sum == work->expected * repetitions ? 0 : -1;
}

int run_cpu_branch_test(void) {
#if !defined(__x86_64__) && !defined(__aarch64__)
    fputs("Native conditional-branch kernel unavailable for this architecture\n", stderr);
    return 77;
#endif
    unsigned char *data = malloc(BRANCH_PATTERN_SIZE);
    if (!data) return 1;
    BenchMeasurement results[PATTERNS];
    for (unsigned pattern = 0; pattern < PATTERNS; ++pattern) {
        BranchWork work = {data, BRANCH_PATTERN_SIZE, make_pattern(data, BRANCH_PATTERN_SIZE, pattern)};
        printf("Warming and sampling: %s\n", pattern_names[pattern]); fflush(stdout);
        if (bench_measure(branch_work, &work, &results[pattern])) { free(data); return 1; }
    }
    free(data);
    ReportContext *report = report_init("cpu_branch", REPORT_FORMAT_MARKDOWN);
    if (!report) return 1;
    report_write_system_info(report);
    report_section(report, "Branch Prediction Test Results");
    bench_report_conditions(report);
    report_write(report, "- Pattern bytes: %u\n\n", (unsigned)BRANCH_PATTERN_SIZE);
    report_write(report, "| Pattern | Time(ms) | ns/branch | Branches | Spread(%%) | Data Source |\n");
    report_write(report, "|---------|----------|-----------|----------|-----------|-------------|\n");
    for (unsigned i = 0; i < PATTERNS; ++i) {
        double count = (double)results[i].repetitions * BRANCH_PATTERN_SIZE;
        double ns = (double)results[i].elapsed_ns / count;
        printf("%s: %.6f ns/branch (%d samples)\n", pattern_names[i], ns, BENCH_SAMPLES);
        report_write(report, "| %s | %.9f | %.6f | %.0f | %.3f | Measured |\n", pattern_names[i],
                     (double)results[i].elapsed_ns / 1e6, ns, count, bench_spread(&results[i]));
    }
    report_section(report, "Notes");
    report_write(report, "One data-dependent conditional branch per input byte, plus loop/control branches.\n"
                 "Balanced patterns use identical input size, kernel and taken-path work.\n"
                 "Elapsed time includes loads and loop overhead; it is not a misprediction percentage.\n"
                 "Branch accuracy requires supported hardware counters and is not estimated here.\n");
    FILE *audit = bench_audit_begin(report);
    if (!audit) { report_free(report); return 1; }
    for (unsigned i = 0; i < PATTERNS; ++i) bench_audit_row(report, audit, pattern_names[i], &results[i], i);
    if (bench_audit_finish(audit)) { report_free(report); return 1; }
    report_finalize_json(report);
    return report_finish(report) ? 1 : 0;
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    init_platform_layer();
    print_system_info();
    return run_cpu_branch_test();
}
