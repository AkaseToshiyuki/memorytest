#ifndef SAMPLE_STATISTICS_H
#define SAMPLE_STATISTICS_H

/* Original shared implementation. Method reference: Criterion.rs documents
 * Tukey's outer (3*IQR) fences, but keeps outliers in its regression. Our
 * explicitly different policy reports a filtered median and always keeps raw
 * samples. Drift or excessive rejection disables filtering, not the evidence. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SAMPLE_STATS_MAX 200
#define SAMPLE_STATS_POLICY "outer-iqr3-median-drift10-cap10-v1"
typedef struct {
    unsigned count, kept, candidates, rejected, unstable;
    double raw_median, median, q1, q3, minimum, maximum, drift_pct;
    unsigned char excluded[SAMPLE_STATS_MAX];
} SampleStatistics;

static int sample_compare(const void *left, const void *right) {
    double a = *(const double *)left, b = *(const double *)right;
    return (a > b) - (a < b);
}

/* Type-7 interpolated quantiles, including the midpoint for an even median. */
static double sample_quantile(const double *sorted, unsigned count, double probability) {
    double position = (count - 1) * probability;
    unsigned lower = (unsigned)position;
    if (lower + 1 >= count) return sorted[count - 1];
    return sorted[lower] + (sorted[lower + 1] - sorted[lower]) * (position - lower);
}

static int sample_statistics(const double *values, unsigned count, SampleStatistics *out) {
    if (!values || !out || count < 3 || count > SAMPLE_STATS_MAX) return -1;
    double sorted[SAMPLE_STATS_MAX], half[SAMPLE_STATS_MAX], kept[SAMPLE_STATS_MAX];
    for (unsigned i = 0; i < count; ++i) {
        if (!isfinite(values[i]) || values[i] <= 0) return -1;
        sorted[i] = values[i];
    }
    qsort(sorted, count, sizeof(double), sample_compare);
    *out = (SampleStatistics){.count = count, .kept = count,
        .raw_median = sample_quantile(sorted, count, .5),
        .q1 = sample_quantile(sorted, count, .25), .q3 = sample_quantile(sorted, count, .75),
        .minimum = sorted[0], .maximum = sorted[count - 1]};
    unsigned half_count = count / 2;
    memcpy(half, values, half_count * sizeof(double));
    qsort(half, half_count, sizeof(double), sample_compare);
    double first = sample_quantile(half, half_count, .5);
    memcpy(half, values + count - half_count, half_count * sizeof(double));
    qsort(half, half_count, sizeof(double), sample_compare);
    double last = sample_quantile(half, half_count, .5);
    out->drift_pct = 100 * (last - first) / out->raw_median;
    out->unstable = fabs(out->drift_pct) > 10;
    double iqr = out->q3 - out->q1;
    if (count >= 10 && iqr > 0) {
        double low = out->q1 - 3 * iqr, high = out->q3 + 3 * iqr;
        for (unsigned i = 0; i < count; ++i) {
            out->excluded[i] = values[i] < low || values[i] > high;
            out->candidates += out->excluded[i];
        }
    }
    if (out->candidates > count / 10) out->unstable = 1;
    /* A phase change or broad contamination is not a few isolated outliers. */
    if (out->unstable) memset(out->excluded, 0, sizeof(out->excluded));
    unsigned n = 0;
    for (unsigned i = 0; i < count; ++i) {
        if (out->excluded[i]) ++out->rejected;
        else kept[n++] = values[i];
    }
    out->kept = n;
    qsort(kept, n, sizeof(double), sample_compare);
    out->median = sample_quantile(kept, n, .5);
    return 0;
}

static void sample_statistics_json(FILE *file, const SampleStatistics *stats) {
    fprintf(file, "{\"policy\":\"" SAMPLE_STATS_POLICY "\",\"count\":%u,\"kept\":%u,"
        "\"outlier_candidates\":%u,\"rejected\":%u,\"unstable\":%s,"
        "\"raw_median\":%.17g,\"median\":%.17g,\"q1\":%.17g,\"q3\":%.17g,"
        "\"raw_min\":%.17g,\"raw_max\":%.17g,\"drift_pct\":%.17g,\"excluded_indices\":[",
        stats->count, stats->kept, stats->candidates, stats->rejected, stats->unstable ? "true" : "false",
        stats->raw_median, stats->median, stats->q1, stats->q3, stats->minimum, stats->maximum, stats->drift_pct);
    unsigned written = 0;
    for (unsigned i = 0; i < stats->count; ++i)
        if (stats->excluded[i]) fprintf(file, "%s%u", written++ ? "," : "", i);
    fputs("]}", file);
}
#endif
