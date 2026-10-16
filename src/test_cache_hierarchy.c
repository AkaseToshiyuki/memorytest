/* Cache bandwidth, dependent-load latency and hierarchy transitions.
 * The same fixed working-set grid and warmed full-cycle chain are used on all
 * platforms. OS cache capacities are metadata, never inferred measurements. */
#include "common.h"
#include "util.h"
#define BENCH_DEFAULT_SAMPLES 5
#include "bench_protocol.h"
#include "memory_kernels.h"

static size_t cache_max_bytes;
static int cache_thread_limit; /* compatibility with bounded regression callers */
static int cache_quick;
/* 0: legacy combined; 4: one latency scan, two independently named reports. */
static int cache_mode;
enum { MAX_CACHE_TEST_SIZES = 64 };
typedef struct {
    size_t size;
    BenchMeasurement bandwidth[3], latency;
    double ns;
} CacheTestResult;
typedef struct { double *a, *c; size_t bytes; unsigned operation; double result; } CacheBandwidth;

static int cache_bandwidth_work(void *context, uint64_t passes) {
    CacheBandwidth *work = context;
    /* Read/write touch the full working set. Copy splits it equally between
     * source and destination, so every curve has the same total footprint. */
    size_t words = work->bytes / (work->operation == STREAM_COPY ? 16 : 8);
    double result = stream_kernel(work->a, work->a, work->c, words, work->operation, passes);
    work->result = result;
    return isfinite(result) && result > 0 ? 0 : -1;
}

static size_t cache_budget(void) {
    size_t budget = detect_available_memory() / 4;
    if (budget > (size_t)GB) budget = (size_t)GB;
    if (cache_max_bytes && budget > cache_max_bytes) budget = cache_max_bytes;
    return budget - budget % 256;
}

static int cache_grid(CacheTestResult *results, size_t limit) {
    size_t size = 8 * KB;
    int count = 0;
    while (size <= limit && count < MAX_CACHE_TEST_SIZES) {
        results[count++] = (CacheTestResult){.size = size};
        if (size == limit) break;
        size_t next = cache_quick ? size * 2 : (size_t)((double)size * 1.4142135623730951);
        next = (next + 255) & ~(size_t)255;
        size = next < limit ? next : limit;
    }
    return count;
}

/* No cache-size prior enters detection. Require a persistent median jump and
 * non-overlapping interquartile bands. Return an interval, not a fabricated exact
 * capacity or an assumed sequence of L1/L2/L3 levels. */
static int cache_transition(const CacheTestResult *rows, int count, int index) {
    if (index < 1 || index + 1 >= count) return 0;
    const CacheTestResult *before = &rows[index - 1], *after = &rows[index];
    if (before->ns <= 0 || after->ns / before->ns < 1.35 || rows[index + 1].ns / before->ns < 1.25) return 0;
    if (before->latency.statistics.unstable || after->latency.statistics.unstable ||
        rows[index + 1].latency.statistics.unstable) return 0;
    double before_max = (before->latency.statistics.count ? before->latency.statistics.q3 : before->latency.max_ns)
                        / (before->latency.repetitions * (double)CHAIN_STEPS);
    double after_min = (after->latency.statistics.count ? after->latency.statistics.q1 : after->latency.min_ns)
                        / (after->latency.repetitions * (double)CHAIN_STEPS);
    return after_min > before_max * 1.10;
}

int run_cache_hierarchy_test(void) {
    (void)cache_thread_limit;
    size_t limit = cache_budget();
    if (limit < 8 * KB) { fputs("Cannot determine a safe cache-test budget\n", stderr); return 1; }
    /* A fixed grid; metadata only adjusts the far end if larger caches exist. */
    size_t desired = 256 * MB;
    size_t last = global_cache_config.l3_size > global_cache_config.l2_size
                    ? global_cache_config.l3_size : global_cache_config.l2_size;
    if (last > desired / 4 && last <= SIZE_MAX / 4) desired = last * 4;
    if (limit > desired) limit = desired;
    CacheTestResult results[MAX_CACHE_TEST_SIZES];
    int count = cache_grid(results, limit);
    if (!count) return 1;
    int do_bandwidth = cache_mode == 0 || cache_mode == 1;
    int do_latency = cache_mode != 1;
    for (int i = 0; i < count; ++i) {
        size_t bytes = results[i].size;
        double *a = do_bandwidth ? malloc(bytes) : NULL, *c = malloc(bytes);
        if ((do_bandwidth && !a) || !c) { free(a); free(c); return 1; }
        if (do_bandwidth)
            for (size_t j = 0; j < bytes / 8; ++j) { a[j] = 1 + (j % 8) * 0.125; c[j] = 0; }
        printf("Warming and sampling cache working set: %zu bytes\n", bytes); fflush(stdout);
        for (unsigned operation = STREAM_READ; do_bandwidth && operation <= STREAM_COPY; ++operation) {
            CacheBandwidth work = {a, c, bytes, operation};
            if (bench_measure(cache_bandwidth_work, &work, &results[i].bandwidth[operation]) ||
                stream_validate(c, bytes / (operation == STREAM_COPY ? 16 : 8), operation,
                                results[i].bandwidth[operation].repetitions, work.result)) {
                free(a); free(c); return 1;
            }
        }
        if (do_latency) {
            chain_build(c, bytes);
            ChainWork work = {c, bytes, (uintptr_t)c};
            /* A complete first traversal faults in and warms every chain node. */
            if (chain_work(&work, (bytes / 64 + CHAIN_STEPS - 1) / CHAIN_STEPS) ||
                bench_measure(chain_work, &work, &results[i].latency)) {
                free(a); free(c); return 1;
            }
            results[i].ns = (double)results[i].latency.elapsed_ns /
                            ((double)results[i].latency.repetitions * CHAIN_STEPS);
        }
        free(a); free(c);
    }
    const char *names[] = {"cache_hierarchy", "cache_bandwidth_test", "cache_latency_test", "cache_detection"};
    for (int view = 0; view < (cache_mode == 4 ? 2 : 1); ++view) {
        int report_mode = cache_mode == 4 ? 2 + view : cache_mode;
        int do_detection = report_mode == 0 || report_mode == 3;
        ReportContext *report = report_init(names[report_mode], REPORT_FORMAT_MARKDOWN);
        if (!report) return 1;
        report_write_system_info(report);
        report_section(report, "Cache Test Conditions");
        bench_report_conditions(report);
        report_write(report, "- Thread Count: 1\n- Node spacing: 64 B\n- Page size: %ld B\n\n", sysconf(_SC_PAGESIZE));
        if (cache_mode == 4) report_write(report, "- Sampling source: shared-cache-latency-scan-v1\n"
            "- Cache latency and hierarchy detection share the SAME ordered raw samples in this invocation.\n\n");
        if (do_bandwidth) {
            report_section(report, "Cache Bandwidth Test");
            report_write(report, "| Size | Operation | Bandwidth (MB/s) | Time(ms) | Bytes | Spread(%%) |\n");
            report_write(report, "|------|-----------|------------------|----------|-------|-----------|\n");
            for (int i = 0; i < count; ++i) {
                for (unsigned operation = STREAM_READ; operation <= STREAM_COPY; ++operation) {
                    BenchMeasurement *sample = &results[i].bandwidth[operation];
                    double payload = (double)results[i].size * sample->repetitions;
                    report_write(report, "| %.6f KiB | %s | %.6f | %.9f | %.0f | %.3f |\n",
                                 (double)results[i].size / KB, stream_names[operation],
                                 payload * 1000 / sample->elapsed_ns, (double)sample->elapsed_ns / 1e6,
                                 payload, bench_spread(sample));
                }
            }
        }
        if (do_latency) {
            report_section(report, "Cache Latency Test");
            report_write(report, "| Size | RdLat(ns) | Time(ms) | Operations | Spread(%%) |\n");
            report_write(report, "|------|-----------|----------|------------|-----------|\n");
            for (int i = 0; i < count; ++i) {
                BenchMeasurement *sample = &results[i].latency;
                report_write(report, "| %.6f KiB | %.6f | %.9f | %.0f | %.3f |\n", (double)results[i].size / KB,
                             results[i].ns, (double)sample->elapsed_ns / 1e6,
                             (double)sample->repetitions * CHAIN_STEPS, bench_spread(sample));
            }
        }
        if (do_detection) {
            report_section(report, "Cache Hierarchy Detection");
            report_write(report, "| Transition | Lower(KiB) | Upper(KiB) | Before(ns) | After(ns) | Ratio |\n");
            report_write(report, "|------------|------------|------------|------------|-----------|-------|\n");
            unsigned hits = 0;
            for (int i = 1; i + 1 < count; ++i) {
                if (!cache_transition(results, count, i)) continue;
                report_write(report, "| %u | %.6f | %.6f | %.6f | %.6f | %.3f |\n", ++hits,
                             (double)results[i - 1].size / KB, (double)results[i].size / KB,
                             results[i - 1].ns, results[i].ns, results[i].ns / results[i - 1].ns);
                ++i; /* do not count adjacent parts of the same rise twice */
            }
            report_write(report, "\n- Detected transitions: %u\n\n", hits);
            report_section(report, "Reported Cache Hierarchy");
            report_write(report, "| Level | Reported(KiB) |\n|-------|---------------|\n");
            size_t sizes[] = {global_cache_config.l1d_size, global_cache_config.l2_size, global_cache_config.l3_size};
            const char *levels[] = {"L1D", "L2", "L3"};
            for (unsigned i = 0; i < 3; ++i)
                if (sizes[i]) report_write(report, "| %s | %.6f |\n", levels[i], (double)sizes[i] / KB);
        }
        report_section(report, "Notes");
        report_write(report, "Single-thread, ordinary OS pages; OS-selected CPU placement. No privileged flush or guessed cache level.\n"
                     "Bandwidth is useful decimal MB/s. Copy uses two half-sized arrays for the listed total working set.\n"
                     "Latency is a warmed dependent permutation cycle with one node per 64 bytes across the full working set.\n"
                     "Transition intervals require sustained changes and non-overlapping raw interquartile bands, without flagged drift.\n"
                     "These distribution bands are not confidence intervals or reported capacities.\n"
                     "TLB, page, migration and frequency effects can also produce transitions; intervals are not exact capacities.\n"
                     "Reported capacities describe the OS view and may be shared by multiple cores.\n");
        FILE *audit = bench_audit_begin(report);
        if (!audit) { report_free(report); return 1; }
        unsigned audit_index = 0;
        for (int i = 0; i < count; ++i) {
            char name[96];
            for (unsigned operation = STREAM_READ; do_bandwidth && operation <= STREAM_COPY; ++operation) {
                snprintf(name, sizeof(name), "%zuB %s", results[i].size, stream_names[operation]);
                bench_audit_row(report, audit, name, &results[i].bandwidth[operation], audit_index++);
            }
            if (do_latency) {
                snprintf(name, sizeof(name), "%zuB dependent load", results[i].size);
                bench_audit_row(report, audit, name, &results[i].latency, audit_index++);
            }
        }
        if (bench_audit_finish(audit)) { report_free(report); return 1; }
        report_finalize_json(report);
        if (report_finish(report)) return 1;
    }
    return 0;
}

int main(int argc, char **argv) {
    init_platform_layer();
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--mode") && i + 1 < argc) {
            const char *mode = argv[++i];
            cache_mode = !strcmp(mode, "bandwidth") ? 1 : !strcmp(mode, "latency") ? 2 :
                         !strcmp(mode, "detection") ? 3 : !strcmp(mode, "latency-detection") ? 4 :
                         !strcmp(mode, "all") ? 0 : -1;
            if (cache_mode < 0) return 2;
        } else if ((!strcmp(argv[i], "--max-size") || !strcmp(argv[i], "-s")) && i + 1 < argc) {
            cache_max_bytes = parse_size(argv[++i]);
            if (cache_max_bytes < 8 * KB) return 2;
        } else if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
            printf("Usage: %s [--max-size SIZE] [--mode all|bandwidth|latency|detection|latency-detection]\n", argv[0]); return 0;
        } else return 2;
    }
    print_system_info();
    return run_cache_hierarchy_test();
}
