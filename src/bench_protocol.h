#ifndef BENCH_PROTOCOL_H
#define BENCH_PROTOCOL_H

/* Diagnostic timing only. ComputeMark's versioned protocol does not use this.
 * Calibration and warmup are discarded; equal-work samples use a median. */
#include "common.h"
#ifndef BENCH_TARGET_NS
#define BENCH_TARGET_NS UINT64_C(150000000)
#endif
#ifndef BENCH_WARMUP_NS
#define BENCH_WARMUP_NS UINT64_C(50000000)
#endif
#ifndef BENCH_SAMPLES
#define BENCH_SAMPLES 5
#endif
#if BENCH_SAMPLES < 3 || BENCH_SAMPLES > 15 || BENCH_SAMPLES % 2 != 1
#error BENCH_SAMPLES must be odd and between 3 and 15
#endif
typedef int (*BenchKernel)(void *, uint64_t);
typedef struct {
    uint64_t repetitions, elapsed_ns, min_ns, max_ns;
} BenchMeasurement;

static int bench_once(BenchKernel kernel, void *context, uint64_t repetitions, uint64_t *elapsed) {
    uint64_t start = get_time_ns();
    if (kernel(context, repetitions)) return -1;
    uint64_t end = get_time_ns();
    if (end < start) return -1;
    *elapsed = end - start;
    return 0;
}

static int bench_measure(BenchKernel kernel, void *context, BenchMeasurement *result) {
    uint64_t repetitions = 1, elapsed = 0, warmed = 0;
    for (;;) {
        if (bench_once(kernel, context, repetitions, &elapsed)) return -1;
        warmed += elapsed;
        if (elapsed >= BENCH_TARGET_NS) break;
        uint64_t scale = elapsed ? BENCH_TARGET_NS / elapsed + 1 : 8;
        if (scale > 8) scale = 8;
        if (scale < 2) scale = 2;
        /* Reject a broken clock/no-op instead of manufacturing a result. */
        if (repetitions > UINT64_C(1073741824) / scale) return -1;
        repetitions *= scale;
    }
    while (warmed < BENCH_WARMUP_NS) {
        if (bench_once(kernel, context, repetitions, &elapsed) || !elapsed) return -1;
        warmed += elapsed;
    }
    uint64_t samples[BENCH_SAMPLES];
    for (unsigned i = 0; i < BENCH_SAMPLES; ++i)
        if (bench_once(kernel, context, repetitions, &samples[i]) || !samples[i]) return -1;
    qsort(samples, BENCH_SAMPLES, sizeof(*samples), compare_uint64);
    *result = (BenchMeasurement){repetitions, samples[BENCH_SAMPLES / 2], samples[0], samples[BENCH_SAMPLES - 1]};
    return 0;
}

static double bench_spread(const BenchMeasurement *result) {
    return 100.0 * (double)(result->max_ns - result->min_ns) / (double)result->elapsed_ns;
}

static void bench_report_conditions(ReportContext *report) {
    report_write(report, "- Protocol: diagnostic-v2\n- Samples: %d\n- Statistic: median\n", BENCH_SAMPLES);
    report_write(report, "- Warmup minimum: %g ms\n- Sample target: %g ms\n\n",
                 (double)BENCH_WARMUP_NS / 1e6, (double)BENCH_TARGET_NS / 1e6);
}
#endif
