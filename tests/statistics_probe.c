#include "../src/sample_statistics.h"
int main(void) {
    unsigned count;
    double values[SAMPLE_STATS_MAX];
    if (scanf("%u", &count) != 1 || count > SAMPLE_STATS_MAX) return 2;
    for (unsigned i = 0; i < count; ++i)
        if (scanf("%lf", &values[i]) != 1) return 2;
    SampleStatistics stats;
    if (sample_statistics(values, count, &stats)) return 2;
    sample_statistics_json(stdout, &stats);
    return 0;
}
