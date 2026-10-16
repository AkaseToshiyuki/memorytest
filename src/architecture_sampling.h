#ifndef ARCHITECTURE_SAMPLING_H
#define ARCHITECTURE_SAMPLING_H
/* One whole-batch retry at four times the duration. Never select individual
 * fast samples or retry according to the inferred capacity. Keep the initial
 * batch in the raw report; readers check cross-batch drift independently. */
typedef struct {
    uint64_t blocks, elapsed[BENCH_SAMPLES];
    ArchCounts counts[BENCH_SAMPLES];
} ArchSampleBatch;
static void arch_batch_capture(ArchSampleBatch *batch, const BenchMeasurement *result,
                               unsigned calls, const uint64_t *inner, const ArchCounts *counts) {
    batch->blocks = result->repetitions;
    for (unsigned i = 0; i < BENCH_SAMPLES; ++i) {
        unsigned slot = (calls+i)%BENCH_SAMPLES;
        batch->elapsed[i] = inner ? inner[slot] : result->samples[i];
        batch->counts[i] = counts ? counts[slot] : (ArchCounts){0};
    }
}
static int arch_values_stable(const double *values) {
    SampleStatistics stats;
    return !sample_statistics(values, BENCH_SAMPLES, &stats) && !stats.unstable &&
        stats.maximum-stats.minimum <= .15*stats.raw_median;
}
static int arch_batch_stable(const ArchSampleBatch *batch, int counters) {
    double times[BENCH_SAMPLES], cycles[BENCH_SAMPLES];
    for (unsigned i = 0; i < BENCH_SAMPLES; ++i) {
        times[i] = (double)batch->elapsed[i];
        cycles[i] = (double)batch->counts[i].cycles;
        if (counters && (!batch->counts[i].cycles || !batch->counts[i].instructions)) return 0;
    }
    return arch_values_stable(times) && (!counters || arch_values_stable(cycles));
}
static int arch_pair_stable(const ArchSampleBatch *a, const ArchSampleBatch *b, int counters) {
    if (!arch_batch_stable(a, counters) || !arch_batch_stable(b, counters)) return 0;
    double ratios[BENCH_SAMPLES];
    for (unsigned i = 0; i < BENCH_SAMPLES; ++i) ratios[i] = (double)a->elapsed[i]/b->elapsed[i];
    return arch_values_stable(ratios);
}
static void arch_batch_json(FILE *json, const ArchSampleBatch *batch, int counters, int mite) {
    fprintf(json, "{\"blocks\":%llu,\"elapsed_ns\":[", (unsigned long long)batch->blocks);
    for (unsigned i = 0; i < BENCH_SAMPLES; ++i)
        fprintf(json, "%s%llu", i ? "," : "", (unsigned long long)batch->elapsed[i]);
    if (counters) {
        const char *fields[] = {"cycles", "instructions", "mite_uops"};
        for (unsigned f = 0; f < (mite ? 3u : 2u); ++f) {
            fprintf(json, "],\"%s\":[", fields[f]);
            for (unsigned i = 0; i < BENCH_SAMPLES; ++i) {
                const ArchCounts *c = &batch->counts[i];
                fprintf(json, "%s%llu", i ? "," : "", (unsigned long long)(f == 0 ? c->cycles : f == 1 ? c->instructions : c->mite));
            }
        }
    }
    fputs("]}", json);
}
static void arch_previous_json(FILE *json, const ArchSampleBatch *previous, int retried, int counters, int mite) {
    fputs(",\"previous_batches\":[", json);
    if (retried) arch_batch_json(json, previous, counters, mite);
    fputc(']', json);
}
#endif
