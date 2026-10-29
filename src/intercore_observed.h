#ifndef INTERCORE_OBSERVED_H
#define INTERCORE_OBSERVED_H
/* Bounded scheduler-observed CAS diagnostic. Platform supplies obs_cpu_token(),
 * obs_set_qos(), obs_probe(), obs_logical_cpus(), obs_platform_info().
 * Original implementation; method reference: hydroo/macos-core-to-core-latency.
 * No affinity promise; endpoint observations cannot exclude out-and-back migration. */
#include <stdatomic.h>
#include <stdint.h>
#include <time.h>
#include "sample_statistics.h"
#define OBS_MAX_CPUS 64
#define OBS_PROTOCOL "macos-observed-cpu-pairs-v2"
#define OBS_SAMPLES 32
#define OBS_MAX_ATTEMPTS 64000
#define OBS_ROUND_TRIPS 2000
#define OBS_WARMUP 128

typedef struct {
    _Alignas(128) pthread_t thread;
    unsigned index;
    uint64_t before, after, elapsed;
    int qos_ok;
} ObsWorker;
typedef struct {
    unsigned from_before, from_after, to_before, to_after;
    uint64_t elapsed_ns;
    int reason; /* 0 kept, 1 migration, 2 same CPU, 3 timeout, 4 quota, 5 ABI */
} ObsAttempt;
typedef struct { unsigned count; double values[OBS_SAMPLES]; SampleStatistics stats; } ObsPair;
static struct {
    _Alignas(128) _Atomic unsigned generation, done, ready;
    _Alignas(128) _Atomic int token;
    _Alignas(128) _Atomic int stop, pair_done, failed, ping_ready, pong_ready, warmed;
    unsigned ping, pong, count;
    uint64_t deadline;
    ObsWorker workers[OBS_MAX_CPUS];
} obs;

static void obs_pause(void) {
#if defined(__aarch64__) || defined(__arm64__)
    __asm__ volatile("yield");
#elif defined(__x86_64__) || defined(__i386__)
    __asm__ volatile("pause");
#endif
}
static int obs_expired(void) {
    if (atomic_load_explicit(&obs.stop, memory_order_relaxed) ||
        atomic_load_explicit(&obs.failed, memory_order_relaxed)) return 1;
    if (get_time_ns() > obs.deadline) {
        atomic_store(&obs.failed, 1);
        return 1;
    }
    return 0;
}
static int obs_bounce(int expected, unsigned iterations) {
    unsigned waits = 0;
    for (unsigned i = 0; i < iterations; ++i) {
        int wanted = expected;
        while (!atomic_compare_exchange_weak_explicit(&obs.token, &wanted, 1 - expected,
                                                       memory_order_relaxed, memory_order_relaxed)) {
            wanted = expected;
            obs_pause();
            if ((++waits & 255) == 0 && obs_expired()) return -1;
        }
        if ((i & 255) == 0 && obs_expired()) return -1;
    }
    return 0;
}
static void *obs_worker(void *argument) {
    ObsWorker *worker = argument;
    worker->qos_ok = obs_set_qos(worker->index) == 0;
    atomic_fetch_add_explicit(&obs.ready, 1, memory_order_release);
    unsigned generation = 0;
    while (!atomic_load_explicit(&obs.stop, memory_order_acquire)) {
        unsigned next = atomic_load_explicit(&obs.generation, memory_order_acquire);
        if (next == generation) { obs_pause(); continue; }
        generation = next;
        if (worker->index == obs.ping || worker->index == obs.pong) {
            int ping = worker->index == obs.ping;
            _Atomic int *mine = ping ? &obs.ping_ready : &obs.pong_ready;
            _Atomic int *peer = ping ? &obs.pong_ready : &obs.ping_ready;
            atomic_store_explicit(mine, 1, memory_order_release);
            while (!atomic_load_explicit(peer, memory_order_acquire))
                if (obs_expired()) break;
            /* Discarded warmup is separate from the timer. Ping completes the
             * final pong before stopping its clock, unlike a half-round trip. */
            if (!obs_expired() && !obs_bounce(ping ? 0 : 1, OBS_WARMUP)) {
                atomic_fetch_add_explicit(&obs.warmed, 1, memory_order_release);
                while (atomic_load_explicit(&obs.warmed, memory_order_acquire) != 2)
                    if (obs_expired()) break;
                worker->before = obs_cpu_token();
                uint64_t start = get_time_ns();
                int failed = obs_bounce(ping ? 0 : 1, OBS_ROUND_TRIPS);
                if (ping && !failed) {
                    while (atomic_load_explicit(&obs.token, memory_order_relaxed) != 0)
                        if (obs_expired()) break;
                }
                uint64_t end = get_time_ns();
                worker->after = obs_cpu_token();
                worker->elapsed = end > start ? end - start : 0;
            }
            if (ping) atomic_store_explicit(&obs.pair_done, 1, memory_order_release);
        } else {
            /* Keep nonparticipants runnable to encourage scheduler coverage.
             * This is a declared load condition, not CPU placement control. */
            uintptr_t value = worker->index + 1;
            while (!atomic_load_explicit(&obs.pair_done, memory_order_acquire)) {
                for (unsigned i = 0; i < 256; ++i) {
                    value = value * 1664525 + 1013904223;
                    __asm__ volatile("" : "+r"(value));
                }
                if (obs_expired()) break;
            }
        }
        atomic_fetch_add_explicit(&obs.done, 1, memory_order_release);
    }
    return NULL;
}
static void obs_stop(unsigned created) {
    atomic_store_explicit(&obs.stop, 1, memory_order_release);
    for (unsigned i = 0; i < created; ++i) pthread_join(obs.workers[i].thread, NULL);
}
static void obs_sleep(void) {
    struct timespec delay = {0, 100000};
    nanosleep(&delay, NULL);
}
static int obs_id(unsigned ids[OBS_MAX_CPUS], unsigned *count, unsigned id, unsigned logical) {
    for (unsigned i = 0; i < *count; ++i) if (ids[i] == id) return (int)i;
    if (*count >= logical || *count >= OBS_MAX_CPUS) return -1;
    ids[*count] = id;
    return (int)(*count)++;
}
static int obs_write(ObsAttempt *attempts, unsigned attempts_count, ObsPair *pairs,
                     unsigned *ids, unsigned id_count, unsigned logical, unsigned workers, unsigned budget_ms) {
    ReportContext *report = report_init("inter_core_latency", REPORT_FORMAT_MARKDOWN);
    if (!report) return 1;
    FILE *audit = fopen(REPORTS_DIR "/inter_core_observed.json", "w");
    if (!audit) { report_free(report); return 1; }
    unsigned ready_pairs = 0;
    report_write_system_info(report);
    report_section(report, "Observed CPU-Pair Latency (experimental)");
    report_write(report, "- Protocol: " OBS_PROTOCOL "\n- Samples: %d\n"
        "- No exact CPU affinity. Endpoint observations cannot exclude intermediate migration.\n"
        "- CPU IDs use an implementation-specific register layout, not a stable public ABI.\n"
        "- Other workers remain runnable. QoS is user-initiated, not P/E core selection.\n"
        "- Latency is CAS round-trip time / 2; not an independently measured one-way delay.\n"
        "- Missing pairs are not interpolated; no topology clustering or pinned-score comparison.\n\n",
        OBS_SAMPLES);
    obs_platform_info(report);
    report_write(report, "| From observed CPU | To observed CPU | Samples | CAS half RTT(ns) | Drift(%%) | Status |\n"
                         "|---|---|---|---|---|---|\n");
    for (unsigned i = 0; i < id_count; ++i) for (unsigned j = 0; j < id_count; ++j) {
        ObsPair *pair = &pairs[i * OBS_MAX_CPUS + j];
        if (pair->count != OBS_SAMPLES || sample_statistics(pair->values, pair->count, &pair->stats)) continue;
        ++ready_pairs;
        report_write(report, "| %u | %u | %u | %.6f | %.6f | %s |\n", ids[i], ids[j], pair->count,
            pair->stats.median, pair->stats.drift_pct, pair->stats.unstable ? "unstable" : "measured");
    }
    report_write(report, "\n- Covered ordered pairs: %u / %u; observed CPU IDs: %u; workers: %u; budget: %u ms.\n",
                 ready_pairs, logical * (logical - 1), id_count, workers, budget_ms);
    fprintf(audit, "{\"schema_version\":1,\"protocol\":\"" OBS_PROTOCOL "\","
        "\"os\":\"Darwin\",\"affinity_verified\":false,\"migration_verification\":\"endpoints-only\","
        "\"qos\":\"user-initiated\",\"qos_verified\":true,\"background_load\":\"other-workers-runnable\","
        "\"cpu_id_source\":\"TPIDR_EL0-low12-private-ABI\",\"timer\":\"CLOCK_MONOTONIC\","
        "\"sample_count\":%u,\"round_trips\":%u,\"warmup_round_trips\":%u,\"logical_cpus\":%u,"
        "\"workers\":%u,\"budget_ms\":%u,\"covered_pairs\":%u,\"attempts\":[",
        OBS_SAMPLES, OBS_ROUND_TRIPS, OBS_WARMUP, logical, workers, budget_ms, ready_pairs);
    const char *reasons[] = {"kept", "migration", "same_cpu", "timeout", "quota", "cpu_id_unavailable"};
    for (unsigned i = 0; i < attempts_count; ++i) {
        ObsAttempt *a = &attempts[i];
        fprintf(audit, "%s{\"from\":[%u,%u],\"to\":[%u,%u],\"elapsed_ns\":%llu,\"status\":\"%s\"}",
            i ? "," : "", a->from_before, a->from_after, a->to_before, a->to_after,
            (unsigned long long)a->elapsed_ns, reasons[a->reason]);
    }
    fputs("]}\n", audit);
    int io_error = ferror(audit);
    if (fclose(audit)) io_error = 1;
    if (report_finish(report)) io_error = 1;
    if (io_error) return 1;
    if (!ready_pairs) {
        fprintf(stderr, "UNSUPPORTED: no observed CPU pair reached %u usable samples within the budget.\n", OBS_SAMPLES);
        return 77;
    }
    return 0;
}

static int observed_main(int argc, char **argv) {
    unsigned logical = obs_logical_cpus(), workers = logical, budget_ms = 20000;
    for (int i = 1; i < argc; ++i) {
        if ((!strcmp(argv[i], "--budget-ms") || !strcmp(argv[i], "--workers")) && i + 1 < argc) {
            int budget = !strcmp(argv[i], "--budget-ms");
            char *end; errno = 0; unsigned long value = strtoul(argv[++i], &end, 10);
            if (errno || *end || value < 1 || value > (budget ? 30000 : OBS_MAX_CPUS)) return 2;
            if (budget) budget_ms = (unsigned)value; else workers = (unsigned)value;
        } else return 2;
    }
    if (logical < 2 || logical > OBS_MAX_CPUS || workers < 2 || workers > logical || obs_probe()) {
        fputs("UNSUPPORTED: native Apple Silicon CPU observations unavailable or CPU count outside 2..64.\n", stderr);
        return 77;
    }
    ObsAttempt *attempts = calloc(OBS_MAX_ATTEMPTS, sizeof(*attempts));
    ObsPair *pairs = calloc(OBS_MAX_CPUS * OBS_MAX_CPUS, sizeof(*pairs));
    if (!attempts || !pairs) { free(attempts); free(pairs); return 1; }
    unsigned created = 0, count = 0, ids[OBS_MAX_CPUS], id_count = 0;
    int status = 1;
    memset(&obs, 0, sizeof(obs));
    if (!atomic_is_lock_free(&obs.token)) goto cleanup;
    uint64_t deadline = get_time_ns() + (uint64_t)budget_ms * 1000000;
    for (unsigned i = 0; i < workers; ++i) {
        obs.workers[i].index = i;
        if (pthread_create(&obs.workers[i].thread, NULL, obs_worker, &obs.workers[i])) goto cleanup;
        ++created;
    }
    while (atomic_load_explicit(&obs.ready, memory_order_acquire) != workers) {
        if (get_time_ns() > deadline) goto cleanup;
        obs_sleep();
    }
    for (unsigned i = 0; i < workers; ++i) if (!obs.workers[i].qos_ok) goto cleanup;
    for (; count < OBS_MAX_ATTEMPTS && get_time_ns() < deadline; ++count) {
        unsigned order = count % (workers * (workers - 1));
        obs.ping = order / (workers - 1);
        obs.pong = order % (workers - 1);
        if (obs.pong >= obs.ping) ++obs.pong;
        obs.deadline = get_time_ns() + UINT64_C(100000000);
        if (obs.deadline > deadline) obs.deadline = deadline;
        atomic_store(&obs.token, 0); atomic_store(&obs.done, 0);
        atomic_store(&obs.failed, 0); atomic_store(&obs.pair_done, 0);
        atomic_store(&obs.ping_ready, 0); atomic_store(&obs.pong_ready, 0);
        atomic_store(&obs.warmed, 0);
        ObsWorker *ping = &obs.workers[obs.ping], *pong = &obs.workers[obs.pong];
        ping->before = ping->after = pong->before = pong->after = UINT64_MAX;
        ping->elapsed = pong->elapsed = 0;
        atomic_fetch_add_explicit(&obs.generation, 1, memory_order_release);
        while (atomic_load_explicit(&obs.done, memory_order_acquire) != workers) {
            if (get_time_ns() > obs.deadline) atomic_store(&obs.failed, 1);
            if (get_time_ns() > obs.deadline + UINT64_C(1000000000)) {
                fputs("Observed CPU-pair worker failed to stop; terminating benchmark.\n", stderr);
                _Exit(1); /* Process isolation is the final bounded cleanup. */
            }
            obs_sleep();
        }
        ObsAttempt *attempt = &attempts[count];
        *attempt = (ObsAttempt){ping->before & 4095, ping->after & 4095, pong->before & 4095,
                               pong->after & 4095, ping->elapsed, 0};
        if (atomic_load(&obs.failed) || !ping->elapsed || !pong->elapsed) attempt->reason = 3;
        else if ((ping->before | ping->after | pong->before | pong->after) >> 20) attempt->reason = 5;
        else if (attempt->from_before != attempt->from_after || attempt->to_before != attempt->to_after) attempt->reason = 1;
        else if (attempt->from_before == attempt->to_before) attempt->reason = 2;
        else {
            int i = obs_id(ids, &id_count, attempt->from_before, logical);
            int j = obs_id(ids, &id_count, attempt->to_before, logical);
            if (i < 0 || j < 0) attempt->reason = 5;
            else {
                ObsPair *pair = &pairs[i * OBS_MAX_CPUS + j];
                if (pair->count == OBS_SAMPLES) attempt->reason = 4;
                else pair->values[pair->count++] = (double)ping->elapsed / (2 * OBS_ROUND_TRIPS);
            }
        }
        unsigned covered = 0;
        for (unsigned i = 0; i < id_count; ++i) for (unsigned j = 0; j < id_count; ++j)
            covered += pairs[i * OBS_MAX_CPUS + j].count == OBS_SAMPLES;
        if (covered == logical * (logical - 1)) { ++count; break; }
    }
    obs_stop(created); created = 0;
    status = obs_write(attempts, count, pairs, ids, id_count, logical, workers, budget_ms);
cleanup:
    obs_stop(created);
    free(attempts); free(pairs);
    return status;
}
#endif
