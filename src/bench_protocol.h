#ifndef BENCH_PROTOCOL_H
#define BENCH_PROTOCOL_H

/* Diagnostic timing only. ComputeMark's versioned protocol does not use this.
 * Calibration and warmup are discarded; equal-work samples use a median. */
#include "common.h"
#include "sample_statistics.h"
#ifndef BENCH_TARGET_NS
#define BENCH_TARGET_NS UINT64_C(150000000)
#endif
#ifndef BENCH_WARMUP_NS
#define BENCH_WARMUP_NS UINT64_C(50000000)
#endif
#ifndef BENCH_SAMPLES
#ifdef BENCH_DEFAULT_SAMPLES
#define BENCH_SAMPLES BENCH_DEFAULT_SAMPLES
#else
#define BENCH_SAMPLES 20
#endif
#endif
#if BENCH_SAMPLES < 3 || BENCH_SAMPLES > SAMPLE_STATS_MAX
#error BENCH_SAMPLES must be between 3 and 200
#endif
typedef int (*BenchKernel)(void *, uint64_t);
typedef struct {
    uint64_t repetitions;
    double elapsed_ns, min_ns, max_ns;
    uint64_t samples[BENCH_SAMPLES];
    SampleStatistics statistics;
} BenchMeasurement;

static int bench_once(BenchKernel kernel, void *context, uint64_t repetitions, uint64_t *elapsed) {
    uint64_t start = get_time_ns();
    if (kernel(context, repetitions)) return -1;
    uint64_t end = get_time_ns();
    if (end < start) return -1;
    *elapsed = end - start;
    return 0;
}

static int bench_grow(uint64_t *repetitions, uint64_t elapsed, uint64_t target) {
    uint64_t scale = elapsed ? target / elapsed + 1 : 8;
    if (scale > 8) scale = 8;
    if (scale < 2) scale = 2;
    if (*repetitions > UINT64_C(1073741824) / scale) return -1;
    *repetitions *= scale;
    return 0;
}

static int bench_measure(BenchKernel kernel, void *context, BenchMeasurement *result) {
    uint64_t repetitions = 1, elapsed = 0, warmed = 0;
    /* Warmup has its own discarded interval; cold page faults/scheduling must
     * not determine the measured iteration count. Google Benchmark and
     * nanobench provide the methodological reference, not shared source. */
    while (warmed < BENCH_WARMUP_NS) {
        if (bench_once(kernel, context, repetitions, &elapsed)) return -1;
        if (UINT64_MAX - warmed < elapsed) return -1;
        warmed += elapsed;
        if (warmed < BENCH_WARMUP_NS && bench_grow(&repetitions, elapsed, BENCH_WARMUP_NS)) return -1;
    }
    repetitions = 1;
    for (;;) {
        if (bench_once(kernel, context, repetitions, &elapsed)) return -1;
        if (elapsed < BENCH_TARGET_NS) {
            if (bench_grow(&repetitions, elapsed, BENCH_TARGET_NS)) return -1;
            continue;
        }
        uint64_t samples[BENCH_SAMPLES];
        for (unsigned i = 0; i < BENCH_SAMPLES; ++i)
            if (bench_once(kernel, context, repetitions, &samples[i]) || !samples[i]) return -1;
        double values[BENCH_SAMPLES];
        for (unsigned i = 0; i < BENCH_SAMPLES; ++i) values[i] = (double)samples[i];
        SampleStatistics statistics;
        if (sample_statistics(values, BENCH_SAMPLES, &statistics)) return -1;
        /* A descheduled calibration pass can appear long while real work is
         * tiny. Discard this whole sample batch and recalibrate; never mix
         * differing work counts into one median or hide slow sample outliers. */
        if (statistics.minimum < BENCH_TARGET_NS / 2) {
            if (bench_grow(&repetitions, (uint64_t)statistics.minimum, BENCH_TARGET_NS)) return -1;
            continue;
        }
        *result = (BenchMeasurement){.repetitions = repetitions, .elapsed_ns = statistics.median,
            .min_ns = statistics.minimum, .max_ns = statistics.maximum, .statistics = statistics};
        memcpy(result->samples, samples, sizeof(samples));
        return 0;
    }
}

static double bench_spread(const BenchMeasurement *result) {
    double median = result->statistics.count ? result->statistics.raw_median : result->elapsed_ns;
    return 100.0 * (result->max_ns - result->min_ns) / median;
}

static void bench_report_conditions(ReportContext *report) {
    report_write(report, "- Protocol: diagnostic-v3\n- Samples: %d\n- Statistic: outer-IQR-filtered median\n", BENCH_SAMPLES);
    report_write(report, "- Warmup minimum: %g ms\n- Sample target: %g ms\n\n",
                 (double)BENCH_WARMUP_NS / 1e6, (double)BENCH_TARGET_NS / 1e6);
    report_write(report, "- Policy: " SAMPLE_STATS_POLICY "\n"
        "- Outer fences: Q1 - 3 IQR / Q3 + 3 IQR; no filtering when IQR is zero or N < 10.\n"
        "- More than 10%% candidate outliers or >10%% first/last-half median drift disables filtering.\n"
        "- Spread always describes ALL samples. Quantiles are distribution summaries, not confidence intervals.\n\n");
}

/* The summary is human-readable; the sidecar preserves timing order and every
 * sample, including excluded values. Failure to save evidence fails the module. */
static FILE *bench_audit_begin(ReportContext *report) {
    char path[256];
    snprintf(path, sizeof(path), "%s/%s_samples.json", REPORTS_DIR, report->test_name);
    FILE *file = fopen(path, "w");
    if (!file) return NULL;
    fputs("{\"schema_version\":1,\"protocol\":\"diagnostic-v3\",\"records\":[\n", file);
    report_section(report, "Sampling Statistics");
    report_write(report, "Raw timing order and excluded indices: %s_samples.json\n\n", report->test_name);
    report_write(report, "| Measurement | Samples | Kept | Rejected | Raw median(ms) | Used median(ms) | Drift(%%) | Status |\n"
                         "|---|---:|---:|---:|---:|---:|---:|---|\n");
    return file;
}

static void bench_audit_row(ReportContext *report, FILE *file, const char *label,
                            const BenchMeasurement *result, unsigned index) {
    const SampleStatistics *stats = &result->statistics;
    report_write(report, "| %s | %u | %u | %u | %.9f | %.9f | %.3f | %s |\n", label,
        stats->count, stats->kept, stats->rejected, stats->raw_median / 1e6, stats->median / 1e6,
        stats->drift_pct, stats->unstable ? "unstable; unfiltered" : "measured");
    /* Labels come from fixed module identifiers and formatted numeric sizes. */
    fprintf(file, "%s{\"measurement\":\"%s\",\"repetitions\":%llu,\"elapsed_ns\":[", index ? ",\n" : "", label,
            (unsigned long long)result->repetitions);
    for (unsigned i = 0; i < BENCH_SAMPLES; ++i)
        fprintf(file, "%s%llu", i ? "," : "", (unsigned long long)result->samples[i]);
    fputs("],\"statistics\":", file);
    sample_statistics_json(file, stats);
    fputc('}', file);
}

static int bench_audit_finish(FILE *file) {
    fputs("\n]}\n", file);
    int failed = ferror(file);
    if (fclose(file)) failed = 1;
    return failed ? -1 : 0;
}
#endif
