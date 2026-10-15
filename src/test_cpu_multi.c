/* ComputeMark v2: measured, fixed-workload single/available-CPU throughput.
 * This module intentionally does not link hardware guessing, PMU, SIMD dispatch
 * or the other microbenchmarks. See README: comparability contract and limits.
 */
#define _GNU_SOURCE
#ifndef _DARWIN_C_SOURCE
#define _DARWIN_C_SOURCE 1
#endif
#include "compute_kernel.h"
#include "sample_statistics.h"
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#ifdef _WIN32
#include "windows_compat.h"
#else
#include <pthread.h>
#include <sched.h>
#include <sys/utsname.h>
#endif
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include "darwin_helpers.h"
#include <pthread/qos.h>
#define CM_PLACEMENT "macos-qos-user-initiated"
#define CM_AFFINITY "false"
#elif defined(_WIN32)
#define CM_PLACEMENT "windows-pinned-process-group"
#define CM_AFFINITY "true"
#else
#define CM_PLACEMENT "linux-pinned"
#define CM_AFFINITY "true"
#endif

#ifdef _WIN32
#define CM_TIMER "QueryPerformanceCounter"
#else
#define CM_TIMER "CLOCK_MONOTONIC"
#endif

#ifndef CM_BUILD_PROFILE
#define CM_BUILD_PROFILE "custom-unverified"
#endif
#ifndef CM_SOURCE_HASH
#define CM_SOURCE_HASH "unknown"
#endif
#ifndef CM_BUILD_FLAGS
#define CM_BUILD_FLAGS "unknown"
#endif

typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t condition;
    int ready, done, go, abort, failed, threads;
    uint64_t end_ns;
} Gate;

typedef struct {
    Gate *gate;
    int cpu, kind;
    size_t cpu_slots;
    uint64_t iterations, checksum;
} Worker;

typedef struct {
    uint64_t elapsed_ns, operations, checksum;
} Sample;

typedef struct {
    int kind, threads;
    uint64_t iterations;
    Sample samples[CM_REPEATS];
    SampleStatistics statistics;
    double rate;
} Result;

static uint64_t now_ns(void) {
#ifdef _WIN32
    return windows_now_ns();
#else
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        perror("clock_gettime");
        exit(1);
    }
    return (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
#endif
}

/* Enumerate the actual allowed IDs (including holes). Never assume [0, N). */
static int allowed_cpus(int **ids, size_t *slots) {
#ifdef __linux__
    for (size_t n = 128; n <= 262144; n *= 2) {
        cpu_set_t *set = CPU_ALLOC(n);
        if (!set) return -1;
        size_t bytes = CPU_ALLOC_SIZE(n);
        CPU_ZERO_S(bytes, set);
        if (sched_getaffinity(0, bytes, set) == 0) {
            int count = CPU_COUNT_S(bytes, set);
            *ids = calloc((size_t)count, sizeof(**ids));
            if (!*ids || count <= 0) { CPU_FREE(set); return -1; }
            int j = 0;
            for (size_t i = 0; i < n; ++i)
                if (CPU_ISSET_S(i, bytes, set)) (*ids)[j++] = (int)i;
            CPU_FREE(set); *slots = n;
            return count;
        }
        int error = errno;
        CPU_FREE(set);
        if (error != EINVAL) return -1;
    }
#elif defined(_WIN32)
    return windows_allowed_cpus(ids, slots);
#elif defined(__APPLE__)
    uint64_t count = darwin_uint("hw.logicalcpu");
    if (!count || count > 262144) { errno = EINVAL; return -1; }
    *ids = calloc((size_t)count, sizeof(int));
    if (!*ids) return -1;
    /* Worker slots only, NOT physical/logical CPU IDs; never report as pinned. */
    for (uint64_t i = 0; i < count; ++i) (*ids)[i] = -1;
    *slots = (size_t)count;
    return (int)count;
#else
    (void)ids; (void)slots;
    errno = ENOTSUP;
#endif
    return -1;
}

static int bind_worker(int cpu, size_t slots) {
#ifdef __linux__
    cpu_set_t *set = CPU_ALLOC(slots);
    if (!set) return -1;
    size_t bytes = CPU_ALLOC_SIZE(slots);
    CPU_ZERO_S(bytes, set);
    CPU_SET_S((size_t)cpu, bytes, set);
    int rc = pthread_setaffinity_np(pthread_self(), bytes, set);
    if (rc == 0) {
        CPU_ZERO_S(bytes, set);
        rc = pthread_getaffinity_np(pthread_self(), bytes, set);
        if (rc == 0 && (CPU_COUNT_S(bytes, set) != 1 ||
                       !CPU_ISSET_S((size_t)cpu, bytes, set))) rc = EINVAL;
    }
    CPU_FREE(set);
    return rc;
#elif defined(_WIN32)
    (void)slots;
    return windows_bind_cpu(cpu);
#elif defined(__APPLE__)
    (void)cpu; (void)slots;
    int rc = pthread_set_qos_class_self_np(QOS_CLASS_USER_INITIATED, 0);
    qos_class_t qos = QOS_CLASS_UNSPECIFIED;
    int relative_priority = 0;
    if (!rc) rc = pthread_get_qos_class_np(pthread_self(), &qos, &relative_priority);
    if (!rc && (qos != QOS_CLASS_USER_INITIATED || relative_priority != 0)) rc = EINVAL;
    return rc; /* QoS is verified, CPU placement is still the OS's decision. */
#else
    (void)cpu; (void)slots;
    return ENOTSUP;
#endif
}

static void *worker_main(void *opaque) {
    Worker *w = opaque;
    Gate *g = w->gate;
    uint64_t ints[4]; double floats[4];
    int failed = bind_worker(w->cpu, w->cpu_slots) != 0 || !cm_selftest();
    cm_seed_integer(ints); cm_seed_float(floats);
    /* Warm up the same workload outside the measured common interval. */
    if (w->kind == 0) cm_integer(32768, ints);
    else cm_float(32768, floats);
    cm_seed_integer(ints); cm_seed_float(floats);
    pthread_mutex_lock(&g->mutex);
    if (failed) g->failed = 1;
    ++g->ready;
    pthread_cond_broadcast(&g->condition);
    while (!g->go && !g->abort) pthread_cond_wait(&g->condition, &g->mutex);
    int abort_run = g->abort;
    pthread_mutex_unlock(&g->mutex);
    if (abort_run) return NULL;
    if (w->kind == 0) {
        cm_integer(w->iterations, ints);
        w->checksum = ints[0] ^ ints[1] ^ ints[2] ^ ints[3];
        for (int i = 0; i < 4; ++i) if (!ints[i]) failed = 1;
    } else {
        cm_float(w->iterations, floats);
        w->checksum = 0;
        for (int i = 0; i < 4; ++i) {
            uint64_t bits;
            memcpy(&bits, &floats[i], sizeof(bits));
            w->checksum ^= bits;
            if (!isfinite(floats[i]) || floats[i] <= 0 || floats[i] >= 1) failed = 1;
        }
    }
    pthread_mutex_lock(&g->mutex);
    if (failed) g->failed = 1;
    if (++g->done == g->threads) g->end_ns = now_ns();
    pthread_cond_broadcast(&g->condition);
    pthread_mutex_unlock(&g->mutex);
    return NULL;
}

static int measure(int kind, int threads, const int *cpus, size_t slots,
                   uint64_t iterations, Sample *sample) {
    Gate gate = {.mutex = PTHREAD_MUTEX_INITIALIZER, .condition = PTHREAD_COND_INITIALIZER,
                 .threads = threads};
    pthread_t *tids = calloc((size_t)threads, sizeof(*tids));
    Worker *workers = calloc((size_t)threads, sizeof(*workers));
    int created = 0, error = 0;
    uint64_t start = 0;
    if (!tids || !workers) { error = ENOMEM; goto cleanup; }
    for (int i = 0; i < threads; ++i) {
        workers[i] = (Worker){.gate = &gate, .cpu = cpus[i], .kind = kind,
                              .cpu_slots = slots, .iterations = iterations};
        error = pthread_create(&tids[i], NULL, worker_main, &workers[i]);
        if (error) break;
        ++created;
    }
    pthread_mutex_lock(&gate.mutex);
    while (gate.ready < created) pthread_cond_wait(&gate.condition, &gate.mutex);
    if (error || gate.failed) gate.abort = 1;
    else { start = now_ns(); gate.go = 1; }
    pthread_cond_broadcast(&gate.condition);
    if (!gate.abort)
        while (gate.done < threads) pthread_cond_wait(&gate.condition, &gate.mutex);
    pthread_mutex_unlock(&gate.mutex);
    for (int i = 0; i < created; ++i) pthread_join(tids[i], NULL);
    if (!error && (gate.failed || gate.abort || gate.end_ns <= start)) error = EINVAL;
    if (!error) {
        sample->elapsed_ns = gate.end_ns - start;
        sample->operations = iterations * (uint64_t)threads * (kind ? CM_FP_OPS : CM_INT_OPS);
        sample->checksum = 0;
        for (int i = 0; i < threads; ++i)
            sample->checksum += workers[i].checksum ^ (uint64_t)i;
    }
cleanup:
    free(tids); free(workers);
    pthread_cond_destroy(&gate.condition);
    pthread_mutex_destroy(&gate.mutex);
    if (error) fprintf(stderr, "Measurement failed (no score): %s\n", strerror(error));
    return error;
}

static int compare_rates(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static int benchmark(Result *result, const int *cpus, size_t slots) {
    uint64_t iterations = 32768;
    /* Calibrate independently for each workload/thread count. Count actual work;
     * calibration time is never scored. All accepted repetitions share work size. */
    for (int attempt = 0; attempt < 32; ++attempt) {
        if (iterations > UINT64_MAX / ((uint64_t)result->threads * CM_INT_OPS)) return 1;
        Sample calibration;
        if (measure(result->kind, result->threads, cpus, slots, iterations, &calibration)) return 1;
        if (calibration.elapsed_ns < CM_MIN_NS) { iterations *= 2; continue; }
        int short_sample = 0;
        double rates[CM_REPEATS];
        for (int i = 0; i < CM_REPEATS; ++i) {
            if (measure(result->kind, result->threads, cpus, slots, iterations, &result->samples[i])) return 1;
            if (result->samples[i].elapsed_ns < CM_MIN_NS) { short_sample = 1; break; }
            rates[i] = (double)result->samples[i].operations / (double)result->samples[i].elapsed_ns;
        }
        if (short_sample) { iterations *= 2; continue; }
        double elapsed[CM_REPEATS], kept[CM_REPEATS];
        for (unsigned i = 0; i < CM_REPEATS; ++i) elapsed[i] = (double)result->samples[i].elapsed_ns;
        if (sample_statistics(elapsed, CM_REPEATS, &result->statistics)) return 1;
        unsigned n = 0;
        for (unsigned i = 0; i < CM_REPEATS; ++i)
            if (!result->statistics.excluded[i]) kept[n++] = rates[i];
        qsort(kept, n, sizeof(double), compare_rates);
        result->iterations = iterations;
        result->rate = sample_quantile(kept, n, .5);
        return 0;
    }
    fprintf(stderr, "Could not obtain sufficiently long samples; no score\n");
    return 1;
}

static void json_string(FILE *file, const char *s) {
    fputc('"', file);
    for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
        if (*p == '"' || *p == '\\') fprintf(file, "\\%c", *p);
        else if (*p < 32) fprintf(file, "\\u%04x", *p);
        else fputc(*p, file);
    }
    fputc('"', file);
}

static int write_reports(Result *results, int count, const int *cpus, int available, int threads) {
    if (mkdir("reports", 0755) != 0 && errno != EEXIST) return 1;
    FILE *md = fopen("reports/cpu_multi_core_report.md", "w");
    FILE *json = fopen("reports/compute_mark_v2.json", "w");
    if (!md || !json) {
        if (md) fclose(md);
        if (json) fclose(json);
        return 1;
    }
#ifdef _WIN32
#if defined(__aarch64__)
    struct { char machine[32], sysname[32], release[32]; } system = {"arm64", "Windows", ""};
#else
    struct { char machine[32], sysname[32], release[32]; } system = {"x86_64", "Windows", ""};
#endif
#else
    struct utsname system;
    if (uname(&system) != 0) memset(&system, 0, sizeof(system));
#endif
    char cpu_model[256] = "unknown";
#ifdef __APPLE__
    darwin_string("machdep.cpu.brand_string", cpu_model, sizeof(cpu_model));
    if (!cpu_model[0]) darwin_string("hw.model", cpu_model, sizeof(cpu_model));
    if (!cpu_model[0]) snprintf(cpu_model, sizeof(cpu_model), "unknown");
#elif defined(_WIN32)
    windows_cpu_model(cpu_model, sizeof(cpu_model));
#else
    FILE *cpuinfo = fopen("/proc/cpuinfo", "r");
    if (cpuinfo) {
        char line[512];
        while (fgets(line, sizeof(line), cpuinfo)) {
            if (strncmp(line, "model name", 10) != 0 && strncmp(line, "Hardware", 8) != 0) continue;
            char *value = strchr(line, ':');
            if (!value) continue;
            do { ++value; } while (*value == ' ' || *value == '\t');
            value[strcspn(value, "\r\n")] = '\0';
            snprintf(cpu_model, sizeof(cpu_model), "%.255s", value);
            break;
        }
        fclose(cpuinfo);
    }
#endif
    fprintf(md, "# cpu_multi_core Test Report\n\n## System Configuration\n\n"
                "| Item | Value |\n|---|---|\n| CPU Model | %s (%s) |\n"
                "| OS | %s %s |\n| Available logical CPUs | %d |\n| Tested threads | %d |\n"
                "| Compiler | %s |\n| Build profile | %s |\n"
                "| Clock | " CM_TIMER ", no CPU-frequency conversion |\n",
            cpu_model, system.machine, system.sysname, system.release,
            available, threads, __VERSION__, CM_BUILD_PROFILE);
    fprintf(md, "| Placement policy | %s |\n| Exact CPU affinity verified | %s |\n\n",
            CM_PLACEMENT, CM_AFFINITY);
#ifdef __APPLE__
    DarwinPerfLevel levels[DARWIN_MAX_PERF_LEVELS];
    size_t level_count = darwin_perflevels(levels, DARWIN_MAX_PERF_LEVELS);
    fprintf(md, "## macOS CPU performance levels\n\n"
                "OS-exposed names/counts; not a record of which core ran each worker. Zero fields are unknown.\n\n"
                "| Level | OS name | Physical CPUs | Logical CPUs | L1D KiB | L2 KiB | CPUs sharing L2 |\n"
                "|---|---|---|---|---|---|---|\n");
    for (size_t i = 0; i < level_count; ++i)
        fprintf(md, "| %zu | %s | %" PRIu64 " | %" PRIu64 " | %" PRIu64 " | %" PRIu64 " | %" PRIu64 " |\n",
                i, levels[i].name, levels[i].physical, levels[i].logical,
                levels[i].l1d / 1024, levels[i].l2 / 1024, levels[i].cpus_per_l2);
    fprintf(md, "\n");
#endif
    fprintf(md, "## ComputeMark v2 measured workloads\n\n"
                "| Operation | Category | Threads | Time(ms) | Speedup | Efficiency | Status | Rate (Gop/s) |\n"
                "|---|---|---|---|---|---|---|---|\n");
    double single[2] = {0, 0};
    for (int i = 0; i < count; ++i) {
        Result *r = &results[i];
        if (r->threads == 1) single[r->kind] = r->rate;
        double speedup = r->rate / single[r->kind];
        fprintf(md, "| %s | %s | %d | %.3f | %.3fx | %.2f%% | measured | %.9f |\n",
                r->kind ? "FP64 logistic" : "U64 xorshift", r->kind ? "FP64" : "Integer",
                r->threads, r->statistics.median / 1e6,
                speedup, speedup / r->threads * 100.0, r->rate);
    }
    fprintf(md, "\n## Protocol\n\n"
                "- Workload: %s; scalar only, binary64 FP, uint64 modulo arithmetic.\n"
                "- Integer: 24 semantic bitwise operations/iteration; FP64: 12 FLOPs/iteration.\n"
                "- Sampling protocol: " CM_SAMPLING "; %d measured samples, each >= 100 ms.\n"
                "- Rate: median of retained operations/common-wall-time rates; even medians average both middle values.\n"
                "- Outliers: outer 3*IQR fences; drift >10%% or >10%% candidates disables filtering.\n"
                "- Time column: retained median time, not the reciprocal of an even-count median rate.\n"
                "- Raw samples and exclusion indices are saved; legacy 5-sample archives remain readable but are a different protocol.\n"
                "- Same deterministic seeds and kernels on every ISA; warmup/creation excluded.\n"
                "- Placement: %s; exact affinity verified: %s.\n"
                "- macOS QoS is a scheduling hint, not P-core selection or CPU pinning.\n"
                "- Speedup = measured multi-thread rate / measured single-thread rate (not a time ratio).\n"
                "- No extrapolation, theoretical peak, SIMD rating or MHz-based IPC estimate.\n"
                "- Measures this process's available CPU capacity; quotas, virtualization, temperature,\n"
                "  background load and compiler version affect results. Compare matching protocol/profile.\n",
            CM_WORKLOAD, CM_REPEATS, CM_PLACEMENT, CM_AFFINITY);
    fprintf(md, "\n## Sampling Statistics\n\n"
                "| Workload | Threads | Samples | Kept | Rejected | Raw median(ms) | Drift(%%) | Status |\n"
                "|---|---:|---:|---:|---:|---:|---:|---|\n");
    for (int i = 0; i < count; ++i) {
        Result *r = &results[i];
        SampleStatistics *s = &r->statistics;
        fprintf(md, "| %s | %d | %u | %u | %u | %.6f | %.3f | %s |\n",
                r->kind ? "FP64" : "Integer", r->threads, s->count, s->kept, s->rejected,
                s->raw_median / 1e6, s->drift_pct, s->unstable ? "unstable; unfiltered" : "measured");
    }
    fprintf(json, "{\"schema_version\":2,\"version\":\"%s\",\"workload\":\"%s\","
                  "\"profile\":", CM_VERSION, CM_WORKLOAD);
    json_string(json, CM_BUILD_PROFILE);
    fprintf(json, ",\"build_flags\":"); json_string(json, CM_BUILD_FLAGS);
    fprintf(json, ",\"source_hash\":"); json_string(json, CM_SOURCE_HASH);
    fprintf(json, ",\"compiler\":"); json_string(json, __VERSION__);
    fprintf(json, ",\"architecture\":"); json_string(json, system.machine);
    fprintf(json, ",\"cpu_model\":"); json_string(json, cpu_model);
    fprintf(json, ",\"os\":"); json_string(json, system.sysname);
#ifdef __APPLE__
    fprintf(json, ",\"macos_system\":{\"memory_bytes\":%" PRIu64 ",\"page_bytes\":%" PRIu64
                  ",\"cacheline_bytes\":%" PRIu64 ",\"reported_perflevels\":%" PRIu64 ",\"perflevels\":[",
            darwin_uint("hw.memsize"), darwin_uint("hw.pagesize"), darwin_uint("hw.cachelinesize"),
            darwin_uint("hw.nperflevels"));
    for (size_t i = 0; i < level_count; ++i) {
        fprintf(json, "%s{\"level\":%zu,\"name\":", i ? "," : "", i);
        json_string(json, levels[i].name);
        fprintf(json, ",\"physical_cpus\":%" PRIu64 ",\"logical_cpus\":%" PRIu64
                      ",\"l1d_bytes\":%" PRIu64 ",\"l2_bytes\":%" PRIu64 ",\"cpus_per_l2\":%" PRIu64 "}",
                levels[i].physical, levels[i].logical, levels[i].l1d, levels[i].l2, levels[i].cpus_per_l2);
    }
    fprintf(json, "]}");
#endif
    fprintf(json, ",\"placement_policy\":\"%s\",\"placement_verified\":true,"
                  "\"available_threads\":%d,\"translated\":false", CM_PLACEMENT, available);
    fprintf(json, ",\"timer\":\"" CM_TIMER "\",\"selftest_passed\":true,"
                  "\"affinity_verified\":%s,\"threads\":%d,\"available_cpus\":", CM_AFFINITY, threads);
#ifdef __APPLE__
    (void)cpus;
    fprintf(json, "null");
#else
    fprintf(json, "[");
    for (int i = 0; i < available; ++i) fprintf(json, "%s%d", i ? "," : "", cpus[i]);
    fprintf(json, "]");
#endif
    fprintf(json, ",\"sampling_protocol\":\"" CM_SAMPLING "\",\"sample_count\":%u,\"records\":[", CM_REPEATS);
    for (int i = 0; i < count; ++i) {
        Result *r = &results[i];
        fprintf(json, "%s{\"workload\":\"%s\",\"threads\":%d,\"iterations_per_thread\":%" PRIu64
                      ",\"operations_per_iteration\":%d,\"samples\":[",
                i ? "," : "", r->kind ? "fp64" : "integer", r->threads, r->iterations,
                r->kind ? CM_FP_OPS : CM_INT_OPS);
        for (int j = 0; j < CM_REPEATS; ++j) {
            Sample *s = &r->samples[j];
            fprintf(json, "%s{\"elapsed_ns\":%" PRIu64 ",\"operations\":%" PRIu64
                          ",\"checksum\":\"%016" PRIx64 "\"}",
                    j ? "," : "", s->elapsed_ns, s->operations, s->checksum);
        }
        fprintf(json, "],\"statistics\":");
        sample_statistics_json(json, &r->statistics);
        fprintf(json, "}");
    }
    fprintf(json, "]}\n");
    int failed = ferror(md) || ferror(json);
    if (fclose(md) != 0) failed = 1;
    if (fclose(json) != 0) failed = 1;
    return failed;
}

int main(int argc, char **argv) {
#ifdef _WIN32
    if (!windows_native_execution()) {
        fprintf(stderr, "Emulated execution or unverified native architecture cannot produce a CPU score. Use the native package.\n");
        return 1;
    }
#endif
#ifdef __APPLE__
    if (darwin_uint("sysctl.proc_translated")) {
        fprintf(stderr, "Rosetta-translated execution is not a native CPU score. Rebuild/run arm64.\n");
        return 1;
    }
#endif
    int requested = 0;
    if (argc == 2 && strcmp(argv[1], "--self-test") == 0) {
        int ok = cm_selftest();
        puts(ok ? "ComputeMark kernel self-test PASS" : "ComputeMark kernel self-test FAIL");
        return ok ? 0 : 1;
    }
    if (argc == 3 && strcmp(argv[1], "--threads") == 0) {
        char *end;
        errno = 0;
        long value = strtol(argv[2], &end, 10);
        if (errno || !*argv[2] || *end || value < 1 || value > 262144) return 2;
        requested = (int)value;
    } else if (argc != 1) {
        fprintf(stderr, "Usage: %s [--threads N | --self-test]\n", argv[0]);
        return 2;
    }
    if (strcmp(CM_BUILD_PROFILE, CM_PROFILE) != 0 || !cm_selftest()) {
        fprintf(stderr, "Unsupported build/FP environment or wrong kernel result; no comparable score\n");
        return 1;
    }
    int *cpus = NULL; size_t slots = 0;
    int available = allowed_cpus(&cpus, &slots);
    if (available < 1 || requested > available) {
        fprintf(stderr, "Cannot resolve requested allowed CPUs; no score\n");
        free(cpus); return 1;
    }
    int threads = requested ? requested : available;
    printf("ComputeMark v2: %d available logical CPUs, testing 1 and %d threads; %s\n", available, threads, CM_PLACEMENT);
    Result results[4] = {{0}};
    int count = 0;
    for (int kind = 0; kind < 2; ++kind) {
        for (int pass = 0; pass < (threads == 1 ? 1 : 2); ++pass) {
            Result *r = &results[count++];
            r->kind = kind; r->threads = pass ? threads : 1;
            if (benchmark(r, cpus, slots)) { free(cpus); return 1; }
            printf("%s / %d threads: %.6f %s\n", kind ? "FP64" : "Integer", r->threads,
                   r->rate, kind ? "GFLOP/s" : "Gop/s");
            fflush(stdout);
        }
    }
    int failed = write_reports(results, count, cpus, available, threads);
    free(cpus);
    return failed ? 1 : 0;
}
