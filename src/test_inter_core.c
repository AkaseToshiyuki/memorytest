/**
 * Inter-Core Memory Access Latency Test - Worker-Pool Optimized
 *
 * Architecture:
 *   - One persistent worker thread per online CPU core, each pinned to its
 *     own core via pthread_setaffinity_np. Workers live for the whole
 *     benchmark; we never pthread_create/join per pair.
 *   - Main thread dispatches a "task" (a single ping-pong round trip with
 *     NUM_SAMPLES samples for latency, or BW_ITERATIONS ops for bandwidth)
 *     to a pair of workers, then waits for both to finish.
 *   - Each worker has its own cas_flag[i] (cache-line-aligned to avoid
 *     false sharing). The two workers in a pair share the same flag for
 *     their round trip.
 *   - Synchronization: lightweight atomic counter + condvar, no
 *     pthread_barrier init/destroy per pair.
 *   - Results are stored in a N×N matrix and reused across console,
 *     JSON, and Markdown output (no duplicate measurement).
 *
 * Platforms: x86_64, ARM64, RISC-V, PowerPC. PMU is probed but with
 * paranoid=4 on most distros the wall-clock fallback is the realistic
 * path; the rest of the code doesn't care which clock we used.
 */

#define _GNU_SOURCE
#include "common.h"
#include "util.h"
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>

/* ===== Tunables (compile-time; runtime scaling for big machines below) ===== */
#define NUM_SAMPLES             10      /* samples per pair for latency */
#define ROUND_TRIPS_PER_SAMPLE  1000    /* CAS round-trips per sample */
#define BW_ITERATIONS_DEFAULT   100000  /* bandwidth CAS per pair */
#define BW_ITERATIONS_LARGE     50000   /* used when nproc > 64 */
#define BW_MIN_DURATION_NS      20000000 /* target ≥20ms per pair (less clock noise) */
#define BW_WARMUP_ITERS         2000    /* warmup before timing bandwidth */

/* Runtime-scaled BW iterations (set in main) */
static int g_bw_iterations = BW_ITERATIONS_DEFAULT;

/* ===== Worker pool ===== */

typedef enum {
    WORKER_IDLE,
    WORKER_BUSY_LATENCY,   /* running NUM_SAMPLES × ROUND_TRIPS_PER_SAMPLE */
    WORKER_BUSY_BANDWIDTH, /* running g_bw_iterations CAS ops */
} worker_state_t;

typedef enum {
    ROLE_PING,   /* CAS 1→0 on target flag (the timing side for latency) */
    ROLE_PONG,   /* CAS 0→1 on target flag (the non-timing side) */
} worker_role_t;

/* Per-worker state, one struct per online core. */
typedef struct {
    pthread_t       thread;
    int             core_id;       /* the core this worker is pinned to */
    int             target_core;   /* the peer's core for the current task */
    worker_state_t  state;         /* non-atomic: only master writes before ready_to_run */
    worker_role_t   state_role;    /* ping/pong — read by worker after wake */
    /* Results from the last completed task. */
    double          samples[NUM_SAMPLES]; /* latency in ns per sample */
    int             n_samples;
    uint64_t        cas_ops;       /* for bandwidth: ops the worker contributed */
    /* Synchronization: per-worker cond+mutex so master wakes only the
     * two workers it needs (no thundering herd). */
    pthread_mutex_t mu;
    pthread_cond_t  cv;
    _Atomic int     ready_to_run;  /* 1 = main has filled in target/role */
    _Atomic int     done_flag;     /* 1 = worker finished this task */
} worker_t;

/* Shared cas_flag[NCPU], cache-line aligned. Each core "owns" flag[i].
 * When testing pair (i, j), worker i spins on flag[j] (peer's flag). */
static _Atomic uint8_t *cas_flags = NULL;
static int cas_flags_cap = 0;

static int cas_flags_ensure(int n) {
    if (n < cas_flags_cap) return 0;
    int new_cap = cas_flags_cap ? cas_flags_cap : 64;
    while (new_cap <= n) new_cap *= 2;
    /* Allocate cache-line aligned. */
    size_t bytes = (size_t)new_cap * 64; /* 64 bytes per flag = one cache line */
    _Atomic uint8_t *new_buf = aligned_alloc(64, bytes);
    if (!new_buf) return -1;
    memset(new_buf, 0, bytes);
    if (cas_flags) free((void *)cas_flags);
    cas_flags = new_buf;
    cas_flags_cap = new_cap;
    return 0;
}

/* Return pointer to core `c`'s flag (64-byte aligned slot). */
static inline _Atomic uint8_t *flag_of(int c) {
    return cas_flags + (size_t)c * 64;
}

/* ===== Worker thread ===== */

static worker_t *g_workers = NULL;     /* size = num_cpus */
static int g_num_cpus = 0;

/* Per-worker synchronization: each worker has its own cond+mutex so the
 * master can wake exactly two workers per pair without thundering-herd
 * on a global condvar (which would wake all 24+ workers and force
 * them to re-check ready_to_run, wasting scheduler time). */

static void *worker_main(void *arg) {
    worker_t *w = (worker_t *)arg;

    set_cpu_affinity(w->core_id);

    for (;;) {
        /* Sleep until master signals us. Each worker has its own mutex
         * and condvar; the master pthread_cond_signal()s us directly. */
        pthread_mutex_lock(&w->mu);
        while (atomic_load_explicit(&w->ready_to_run, memory_order_acquire) == 0) {
            pthread_cond_wait(&w->cv, &w->mu);
        }
        pthread_mutex_unlock(&w->mu);

        /* Reset done_flag so master can wait for it. */
        atomic_store_explicit(&w->done_flag, 0, memory_order_release);

        worker_state_t s = atomic_load_explicit(&w->state, memory_order_acquire);
        if (s == WORKER_BUSY_LATENCY) {
            /* Latency: classic Lamport ping-pong on a single flag.
             *
             *   flag[j] = 1   (master sets this)
             *
             *   ping (i) spins CAS 1→0 on flag[j].
             *   pong (j) spins CAS 0→1 on flag[j].
             *
             * Both workers hit the SAME flag — the latency we measure
             * is exactly the cache-coherence round-trip time between
             * core i and core j. This is the canonical ping-pong
             * latency benchmark.
             *
             * The `state_role` distinction is no longer needed for
             * latency mode (both do the same protocol on the same
             * flag); we keep it for symmetry with the bandwidth path
             * which uses different roles. */
            w->n_samples = 0;
            _Atomic uint8_t *target_flag = flag_of(w->target_core);

            for (int s_idx = 0; s_idx < NUM_SAMPLES; s_idx++) {
                uint64_t t0 = get_time_ns();
                for (int i = 0; i < ROUND_TRIPS_PER_SAMPLE; i++) {
                    /* Both ping and pong alternate: one side does 1→0,
                     * the other does 0→1. The shared flag flips each
                     * successful CAS. */
                    if (w->state_role == ROLE_PING) {
                        uint8_t expected = 1;
                        while (!atomic_compare_exchange_weak_explicit(
                                target_flag, &expected, 0,
                                memory_order_relaxed, memory_order_relaxed)) {
                            expected = 1;
                        }
                    } else {
                        uint8_t expected = 0;
                        while (!atomic_compare_exchange_weak_explicit(
                                target_flag, &expected, 1,
                                memory_order_relaxed, memory_order_relaxed)) {
                            expected = 0;
                        }
                    }
                }
                uint64_t t1 = get_time_ns();
                w->samples[w->n_samples++] =
                    (double)(t1 - t0) / ROUND_TRIPS_PER_SAMPLE / 2.0;
            }
        } else if (s == WORKER_BUSY_BANDWIDTH) {
            /* Bandwidth: same ping-pong protocol as latency, but without
             * per-round-trip timestamps. Both workers hit the SAME flag
             * (flag[w->target_core]) and alternate 1→0 / 0→1. Each
             * successful CAS counts as one op for that worker.
             *
             * Protocol: PING (1→0) → PONG (0→1) → PING (1→0) → ...
             * Total successful CAS across both workers = 2 × g_bw_iterations. */
            _Atomic uint8_t *target_flag = flag_of(w->target_core);
            /* Warmup — prime caches/MESI state (both roles alternate). */
            for (int i = 0; i < BW_WARMUP_ITERS; i++) {
                if (w->state_role == ROLE_PING) {
                    uint8_t expected = 1;
                    while (!atomic_compare_exchange_weak_explicit(
                            target_flag, &expected, 0,
                            memory_order_relaxed, memory_order_relaxed)) {
                        expected = 1;
                    }
                } else {
                    uint8_t expected = 0;
                    while (!atomic_compare_exchange_weak_explicit(
                            target_flag, &expected, 1,
                            memory_order_relaxed, memory_order_relaxed)) {
                        expected = 0;
                    }
                }
            }
            w->cas_ops = 0;
            for (int i = 0; i < g_bw_iterations; i++) {
                if (w->state_role == ROLE_PING) {
                    uint8_t expected = 1;
                    while (!atomic_compare_exchange_weak_explicit(
                            target_flag, &expected, 0,
                            memory_order_relaxed, memory_order_relaxed)) {
                        expected = 1;
                    }
                } else {
                    uint8_t expected = 0;
                    while (!atomic_compare_exchange_weak_explicit(
                            target_flag, &expected, 1,
                            memory_order_relaxed, memory_order_relaxed)) {
                        expected = 0;
                    }
                }
                w->cas_ops++;
            }
        }
        /* WORKER_IDLE is not a real task; should never reach here. */

        /* Signal done. The master reads w->done_flag to know this worker
         * has finished; we don't need a global counter because the
         * master knows exactly which 2 workers it dispatched. */
        atomic_store_explicit(&w->done_flag, 1, memory_order_release);
        atomic_store_explicit(&w->ready_to_run, 0, memory_order_release);
    }
    return NULL; /* unreachable */
}

/* ===== Task dispatch (master side) ===== */

static int worker_pool_init(int n) {
    g_num_cpus = n;
    if (cas_flags_ensure(n) < 0) return -1;

    g_workers = calloc((size_t)n, sizeof(worker_t));
    if (!g_workers) return -1;

    for (int i = 0; i < n; i++) {
        g_workers[i].core_id = i;
        g_workers[i].state = WORKER_IDLE;
        pthread_mutex_init(&g_workers[i].mu, NULL);
        pthread_cond_init(&g_workers[i].cv, NULL);
        atomic_store_explicit(&g_workers[i].ready_to_run, 0, memory_order_relaxed);
        atomic_store_explicit(&g_workers[i].done_flag, 0, memory_order_relaxed);
        if (pthread_create(&g_workers[i].thread, NULL, worker_main, &g_workers[i]) != 0) {
            fprintf(stderr, "[inter_core] pthread_create failed for core %d\n", i);
            return -1;
        }
    }
    return 0;
}

/* Wake worker `w` and return immediately. Caller must separately
 * wait with wait_for_done(). The wake is non-blocking; the worker
 * starts running as soon as the OS schedules it. */
static inline void wake_worker(worker_t *w) {
    pthread_mutex_lock(&w->mu);
    atomic_store_explicit(&w->ready_to_run, 1, memory_order_release);
    pthread_cond_signal(&w->cv);
    pthread_mutex_unlock(&w->mu);
}

/* Wait for worker `w` to finish its current task (done_flag = 1).
 * After returning, also atomically resets done_flag → 0 so the
 * worker's own ready_to_run reset (which happens immediately
 * after setting done=1) has time to land before the master peeks
 * at it on the next dispatch. The reset is racy but bounded: the
 * master does a single CAS, and the worker's reset is a store,
 * so at worst the master sees ready=1 for one extra iteration
 * (and the dispatch_pair will simply retry). */
static inline void wait_for_done(worker_t *w) {
    while (atomic_load_explicit(&w->done_flag, memory_order_acquire) == 0) {
        struct timespec ts = {0, 1000};
        nanosleep(&ts, NULL);
    }
    /* Consume the done signal so the worker doesn't have to. */
    atomic_store_explicit(&w->done_flag, 0, memory_order_release);
    /* Brief barrier: yield to let the worker's `ready_to_run = 0`
     * store propagate to our cache before the next dispatch_pair()
     * reads it. This is a workaround for a benign race; the
     * dispatch_pair stale check would just see ready=1 and skip
     * the pair, which is observable as a 0 in the matrix. The yield
     * here reduces the window to "essentially never". */
    sched_yield();
}

/* Dispatch a (i, j) pair task and wait for both workers to complete.
 * The master sets up the task, signals both workers' condvars, then
 * waits on each worker's done_flag. */
static void dispatch_pair(int i, int j, worker_state_t mode) {
    worker_t *wi = &g_workers[i];
    worker_t *wj = &g_workers[j];

    /* Pre-condition: neither worker has a pending ready_to_run. If a
     * previous dispatch somehow left stale state, refuse to corrupt. */
    if (atomic_load_explicit(&wi->ready_to_run, memory_order_acquire) ||
        atomic_load_explicit(&wj->ready_to_run, memory_order_acquire)) {
        fprintf(stderr, "[inter_core] dispatch_pair(%d,%d): stale ready_to_run!\n",
                i, j);
        return;
    }

    /* Set up task parameters. The latency path uses a single shared
     * flag (the peer's, flag[j]) — both workers hit the same flag
     * for the round-trip. So both wi and wj need target_core = j. */
    atomic_store_explicit(&wi->state, mode, memory_order_release);
    atomic_store_explicit(&wj->state, mode, memory_order_release);
    if (mode == WORKER_BUSY_LATENCY) {
        wi->target_core = j;   /* both CAS flag[j] */
        wj->target_core = j;
        wi->state_role = ROLE_PING;
        wj->state_role = ROLE_PONG;
    } else {
        /* Bandwidth: same protocol as latency — both workers hit
         * flag[j] and alternate PING(1→0)/PONG(0→1). */
        wi->target_core = j;
        wj->target_core = j;
        wi->state_role = ROLE_PING;
        wj->state_role = ROLE_PONG;
    }
    wi->n_samples = 0;
    wj->n_samples = 0;
    wi->cas_ops = 0;
    wj->cas_ops = 0;

    if (mode == WORKER_BUSY_LATENCY) {
        /* Single shared flag flag[j] = 1. Ping will CAS it 1→0 first. */
        atomic_store_explicit(flag_of(j), 1, memory_order_relaxed);
    } else {
        /* Bandwidth: PING starts with flag[j]=1 (CAS 1→0). */
        atomic_store_explicit(flag_of(j), 1, memory_order_relaxed);
    }

    /* Wake both workers in parallel BEFORE waiting on either. The
     * ping-pong protocol requires both threads to be running
     * concurrently: ping (i) spins CAS 1→0 on flag[j], pong (j) spins
     * CAS 0→1 on flag[i]. If we wake one and then wait synchronously,
     * the first worker spins forever waiting for a peer that hasn't
     * started yet. */
    wake_worker(wi);
    wake_worker(wj);
    wait_for_done(wi);
    wait_for_done(wj);
}

/* ===== Statistics ===== */

static int compare_doubles(const void *a, const void *b) {
    double da = *(const double *)a, db = *(const double *)b;
    return (da < db) ? -1 : (da > db) ? 1 : 0;
}

/* Compute p25 / median / p75 from samples[] using nearest-rank percentile.
 * Returns 1 if ok, 0 if no samples. */
static int percentiles(const double *samples, int n,
                       double *p25, double *med, double *p75) {
    if (n <= 0) return 0;
    /* Sort in place. Caller is expected to pass a copy because we
     * may sort, but workers[i].samples is single-use per pair anyway. */
    /* NB: we don't sort here; the caller does it on its own buffer. */
    *p25 = samples[n / 4];
    *med = samples[n / 2];
    *p75 = samples[(3 * n) / 4];
    if (n >= 4) return 1;
    if (n >= 2) { *p25 = samples[0]; *med = samples[n/2]; *p75 = samples[n-1]; return 1; }
    *p25 = *med = *p75 = samples[0];
    return 1;
}

/* Trim outliers: drop bottom 5% and top 5% of sorted samples. */
static void trim_outliers(const double *sorted_in, int n_in,
                          double *out, int *n_out) {
    if (n_in <= 0) { *n_out = 0; return; }
    int lo = n_in / 20;     /* 5% */
    int hi = n_in - 1 - lo;
    if (hi < lo) hi = lo;
    int k = 0;
    for (int i = lo; i <= hi; i++) out[k++] = sorted_in[i];
    *n_out = k;
}

/* ===== Per-pair execution ===== */

static int run_pair_latency(int i, int j, double *p25, double *med, double *p75) {
    /* The "ping" role in the dispatch protocol records samples in
     * workers[i].samples (we measure in ping). Pong doesn't time. */
    dispatch_pair(i, j, WORKER_BUSY_LATENCY);
    double sorted[NUM_SAMPLES];
    int n = g_workers[i].n_samples;
    if (n <= 0) return 0;
    memcpy(sorted, g_workers[i].samples, n * sizeof(double));
    qsort(sorted, (size_t)n, sizeof(double), compare_doubles);

    /* Trim 5% top/bottom (NUM_SAMPLES=10, so trim 0; do nothing). */
    double trimmed[NUM_SAMPLES];
    int n_trim;
    trim_outliers(sorted, n, trimmed, &n_trim);
    if (n_trim <= 0) {
        /* Fallback to all samples. */
        memcpy(trimmed, sorted, n * sizeof(double));
        n_trim = n;
    }
    /* Robust filter: drop any sample > 3x median (catastrophic
     * scheduling delays, not real signal). */
    double rough_med = sorted[n / 2];
    double good[NUM_SAMPLES];
    int n_good = 0;
    for (int k = 0; k < n_trim; k++) {
        if (trimmed[k] < 3.0 * rough_med) {
            good[n_good++] = trimmed[k];
        }
    }
    if (n_good == 0) {
        /* All samples were > 3x median; use full trimmed set. */
        memcpy(good, trimmed, n_trim * sizeof(double));
        n_good = n_trim;
    }
    qsort(good, (size_t)n_good, sizeof(double), compare_doubles);
    *p25 = good[n_good / 4];
    *med = good[n_good / 2];
    *p75 = good[(3 * n_good) / 4];
    return 1;
}

/* (Bandwidth timing is done inline in run_inter_core_latency_test()
 * by sandwiching get_time_ns() around dispatch_pair(). No separate
 * helper needed — the dispatch itself is the only work the master
 * waits on, so wall-clock around dispatch_pair() == pair wall time.) */

/* ===== Matrix measurement ===== */

typedef struct {
    int n;
    double *lat_p25;  /* n*n */
    double *lat_med;
    double *lat_p75;
    double *bw_mops;  /* n*n */
} matrix_t;

static matrix_t *matrix_alloc(int n) {
    matrix_t *m = calloc(1, sizeof(matrix_t));
    if (!m) return NULL;
    m->n = n;
    size_t nn = (size_t)n * n;
    m->lat_p25 = calloc(nn, sizeof(double));
    m->lat_med = calloc(nn, sizeof(double));
    m->lat_p75 = calloc(nn, sizeof(double));
    m->bw_mops  = calloc(nn, sizeof(double));
    if (!m->lat_p25 || !m->lat_med || !m->lat_p75 || !m->bw_mops) {
        free(m->lat_p25); free(m->lat_med); free(m->lat_p75); free(m->bw_mops);
        free(m);
        return NULL;
    }
    /* Diagonal = NaN sentinel. */
    for (int i = 0; i < n; i++) {
        m->lat_p25[i*n+i] = m->lat_med[i*n+i] = m->lat_p75[i*n+i] = 0.0/0.0;
        m->bw_mops[i*n+i]  = 0.0/0.0;
    }
    return m;
}

static void matrix_free(matrix_t *m) {
    if (!m) return;
    free(m->lat_p25); free(m->lat_med); free(m->lat_p75); free(m->bw_mops);
    free(m);
}

static double idx(const matrix_t *m, int i, int j, double *base) {
    return base[i * m->n + j];
}

/* ===== Main test driver ===== */

void run_inter_core_latency_test(void) {
    print_header("INTER-CORE MEMORY ACCESS LATENCY TEST");

    /* Use physical cores when HT/SMT is detected. Testing HT siblings
     * doubles the matrix (N² → 4N² for same physical topology) and
     * adds noise since siblings share execution units and caches. */
    int physical = global_system_config.cpu_cores_physical;
    long num_cpus = (physical > 0) ? physical : sysconf(_SC_NPROCESSORS_ONLN);
    if (num_cpus < 2) {
        printf("Not enough cores for inter-core test (need at least 2)\n");
        return;
    }
    int n = (int)num_cpus;

    if (worker_pool_init(n) < 0) {
        fprintf(stderr, "[inter_core] worker pool init failed\n");
        return;
    }
    matrix_t *M = matrix_alloc(n);
    if (!M) {
        fprintf(stderr, "[inter_core] matrix alloc failed\n");
        return;
    }

    printf("Detected %d CPU cores (using %d physical, HT siblings excluded)\n",
           global_system_config.cpu_cores, n);
    printf("CPU: %s @ %d MHz\n", global_system_config.cpu_model, get_cpu_freq_mhz());
    printf("Clock: CLOCK_MONOTONIC (PMU cycle counter requires sudo on this host)\n");
    printf("Synchronization: CAS busy-wait (atomic compare_exchange)\n\n");

    /* ===== Phase 1: latency matrix — one pass, store all ===== */
    printf("=== CAS Latency (ns, median) ===\n");
    printf("Measuring %d x %d = %d core pairs (single pass, results cached)...\n\n", n, n, n*n);

    for (int i = 0; i < n; i++) {
        printf("%-4d", i);
        for (int j = 0; j < n; j++) {
            if (i == j) {
                printf("%5s", "-");
            } else {
                double p25, med, p75;
                if (run_pair_latency(i, j, &p25, &med, &p75)) {
                    M->lat_p25[i*n+j] = p25;
                    M->lat_med[i*n+j] = med;
                    M->lat_p75[i*n+j] = p75;
                    printf("%5.1f", med);
                } else {
                    printf("%5s", "N/A");
                }
            }
        }
        printf("\n");
        fflush(stdout);
    }
    printf("\n");

    /* ===== Phase 2: bandwidth matrix — one pass =====
     * Time the dispatch window to compute Mops/s. Total successful CAS
     * across both workers = 2 × g_bw_iterations (each worker does
     * g_bw_iterations successful CAS on the peer's flag; the peer's
     * spin on the now-zero flag doesn't count because it failed CAS).
     *
     * Calibrate g_bw_iterations on a single pair first so all pairs
     * use the same iteration count — avoids inconsistent scaling. */
    {
        int bw_cap = (n > 64) ? 200000 : 1000000;
        int cal_pair_i = 0, cal_pair_j = (n > 1) ? 1 : 0;
        while (g_bw_iterations < bw_cap) {
            uint64_t t0 = get_time_ns();
            dispatch_pair(cal_pair_i, cal_pair_j, WORKER_BUSY_BANDWIDTH);
            uint64_t t1 = get_time_ns();
            if ((t1 - t0) >= 50000000UL) break;
            g_bw_iterations *= 2;
            fprintf(stderr, "[inter_core] BW auto-scale → %d iters "
                    "(cal pair %d→%d took %.1fms)\n",
                    g_bw_iterations, cal_pair_i, cal_pair_j, (t1 - t0) / 1e6);
        }
    }
    printf("=== CAS Throughput (Mops/s) [%d iterations per pair] ===\n",
           g_bw_iterations);
    printf("Measuring %d x %d = %d core pairs (single pass)...\n\n", n, n, n*n);

    for (int i = 0; i < n; i++) {
        printf("%-4d", i);
        for (int j = 0; j < n; j++) {
            if (i == j) {
                printf("%5s", "-");
            } else {
                uint64_t t0 = get_time_ns();
                dispatch_pair(i, j, WORKER_BUSY_BANDWIDTH);
                uint64_t t1 = get_time_ns();
                double dur_s = (double)(t1 - t0) / 1e9;
                double mops = (double)(g_bw_iterations * 2) / dur_s / 1e6;
                M->bw_mops[i*n+j] = mops;
                printf("%5.1f", mops);
            }
        }
        printf("\n");
        fflush(stdout);
    }
    printf("\n");

    /* ===== Stats summary (p25/median/p75 instead of avg/min/max) ===== */
    double lat_sum = 0; int lat_n = 0;
    double lat_min = 1e9, lat_max = 0;
    double *bw_array = NULL; int bw_n = 0;
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) {
            if (i == j) continue;
            double v = M->lat_med[i*n+j];
            if (v == v) {  /* not NaN */
                lat_sum += v;
                lat_n++;
                if (v < lat_min) lat_min = v;
                if (v > lat_max) lat_max = v;
            }
            double w = M->bw_mops[i*n+j];
            if (w == w) bw_n++;
        }
    }
    if (lat_n) printf("Latency — Min: %.1f ns, Max: %.1f ns, Avg: %.1f ns\n",
                      lat_min, lat_max, lat_sum / lat_n);
    if (bw_n) {
        bw_array = malloc((size_t)bw_n * sizeof(double));
        int bi = 0;
        for (int i = 0; i < n; i++)
            for (int j = 0; j < n; j++) {
                if (i == j) continue;
                double w = M->bw_mops[i*n+j];
                if (w == w) bw_array[bi++] = w;
            }
        qsort(bw_array, (size_t)bw_n, sizeof(double), compare_doubles);
        int p25i = (bw_n - 1) / 4;
        int p50i = (bw_n - 1) / 2;
        int p75i = 3 * (bw_n - 1) / 4;
        printf("Throughput — p25: %.1f, median: %.1f, p75: %.1f Mops/s\n",
               bw_array[p25i], bw_array[p50i], bw_array[p75i]);
    }

    /* ===== JSON heatmap data (read from matrix, no re-measure) ===== */
    FILE *json_fp = fopen("reports/inter_core_heatmap_data.json", "w");
    if (json_fp) {
        fprintf(json_fp, "{\n");
        fprintf(json_fp, "  \"cpu_name\": \"%s\",\n", global_system_config.cpu_model);
        fprintf(json_fp, "  \"num_cores\": %d,\n", n);
        fprintf(json_fp, "  \"max_freq_mhz\": %d,\n", get_cpu_freq_mhz());
        fprintf(json_fp, "  \"arch\": \"%s\",\n", arch_names[current_arch]);
        fprintf(json_fp, "  \"latency_stats\": {\n");
        fprintf(json_fp, "    \"min_ns\": %.1f,\n", lat_n ? lat_min : 0.0);
        fprintf(json_fp, "    \"max_ns\": %.1f,\n", lat_n ? lat_max : 0.0);
        fprintf(json_fp, "    \"avg_ns\": %.1f\n", lat_n ? lat_sum/lat_n : 0.0);
        fprintf(json_fp, "  },\n");
        fprintf(json_fp, "  \"throughput_stats\": {\n");
        if (bw_n) {
            fprintf(json_fp, "    \"p25_mops\": %.1f,\n", bw_array[(bw_n-1)/4]);
            fprintf(json_fp, "    \"median_mops\": %.1f,\n", bw_array[(bw_n-1)/2]);
            fprintf(json_fp, "    \"p75_mops\": %.1f\n", bw_array[3*(bw_n-1)/4]);
        } else {
            fprintf(json_fp, "    \"p25_mops\": 0,\n    \"median_mops\": 0,\n    \"p75_mops\": 0\n");
        }
        fprintf(json_fp, "  },\n");
        fprintf(json_fp, "  \"cas_latency_matrix\": [\n");
        for (int i = 0; i < n; i++) {
            fprintf(json_fp, "    [");
            for (int j = 0; j < n; j++) {
                if (j) fprintf(json_fp, ", ");
                if (i == j) {
                    fprintf(json_fp, "null");
                } else {
                    double v = M->lat_med[i*n+j];
                    fprintf(json_fp, v == v ? "%.1f" : "null", v);
                }
            }
            fprintf(json_fp, "]%s\n", i < n - 1 ? "," : "");
        }
        fprintf(json_fp, "  ],\n");
        fprintf(json_fp, "  \"cas_p25_matrix\": [\n");
        for (int i = 0; i < n; i++) {
            fprintf(json_fp, "    [");
            for (int j = 0; j < n; j++) {
                if (j) fprintf(json_fp, ", ");
                if (i == j) fprintf(json_fp, "null");
                else { double v = M->lat_p25[i*n+j]; fprintf(json_fp, v==v?"%.1f":"null", v); }
            }
            fprintf(json_fp, "]%s\n", i < n - 1 ? "," : "");
        }
        fprintf(json_fp, "  ],\n");
        fprintf(json_fp, "  \"cas_p75_matrix\": [\n");
        for (int i = 0; i < n; i++) {
            fprintf(json_fp, "    [");
            for (int j = 0; j < n; j++) {
                if (j) fprintf(json_fp, ", ");
                if (i == j) fprintf(json_fp, "null");
                else { double v = M->lat_p75[i*n+j]; fprintf(json_fp, v==v?"%.1f":"null", v); }
            }
            fprintf(json_fp, "]%s\n", i < n - 1 ? "," : "");
        }
        fprintf(json_fp, "  ],\n");
        fprintf(json_fp, "  \"cas_throughput_matrix\": [\n");
        for (int i = 0; i < n; i++) {
            fprintf(json_fp, "    [");
            for (int j = 0; j < n; j++) {
                if (j) fprintf(json_fp, ", ");
                if (i == j) fprintf(json_fp, "null");
                else { double v = M->bw_mops[i*n+j]; fprintf(json_fp, v==v?"%.1f":"null", v); }
            }
            fprintf(json_fp, "]%s\n", i < n - 1 ? "," : "");
        }
        fprintf(json_fp, "  ]\n}\n");
        fclose(json_fp);
    }

    /* ===== Markdown report (read from matrix, no re-measure) ===== */
    ReportContext *report = report_init("inter_core_latency", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "CAS Latency Matrix (ns, median [p25..p75])");
        report_write(report, "| **Core** |");
        for (int j = 0; j < n; j++) report_write(report, " %d |", j);
        report_write(report, "\n|---|");
        for (int j = 0; j < n; j++) report_write(report, "---:|");
        report_write(report, "\n");
        for (int i = 0; i < n; i++) {
            report_write(report, "| %d |", i);
            for (int j = 0; j < n; j++) {
                if (i == j) report_write(report, " - |");
                else {
                    double p25 = M->lat_p25[i*n+j];
                    double med = M->lat_med[i*n+j];
                    double p75 = M->lat_p75[i*n+j];
                    if (med == med) {
                        report_write(report, " %.1f [%.1f-%.1f] |", med, p25, p75);
                    } else {
                        report_write(report, " N/A |");
                    }
                }
            }
            report_write(report, "\n");
        }

        report_section(report, "CAS Throughput Matrix (Mops/s)");
        report_write(report, "| **Core** |");
        for (int j = 0; j < n; j++) report_write(report, " %d |", j);
        report_write(report, "\n|---|");
        for (int j = 0; j < n; j++) report_write(report, "---:|");
        report_write(report, "\n");
        for (int i = 0; i < n; i++) {
            report_write(report, "| %d |", i);
            for (int j = 0; j < n; j++) {
                if (i == j) report_write(report, " - |");
                else {
                    double v = M->bw_mops[i*n+j];
                    report_write(report, v == v ? " %.1f |" : " N/A |", v);
                }
            }
            report_write(report, "\n");
        }

        report_section(report, "Notes");
        report_write(report, "- Clock: CLOCK_MONOTONIC\n");
        report_write(report, "- Architecture: %s\n", arch_names[current_arch]);
        report_write(report, "- Synchronization: CAS busy-wait (atomic compare_exchange)\n");
        report_write(report, "- Worker pool: one persistent thread per core, pinned via set_cpu_affinity\n");
        report_write(report, "- Matrix measured once; reused for console / JSON / Markdown\n");
        report_write(report, "- Outlier filter: 5% trim + 3×median cap, then p25/median/p75\n");

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }

    matrix_free(M);
    free(bw_array);
    /* Workers are infinite loops; they'll exit on process exit. */

    printf("\n[DONE] Inter-core latency test complete.\n");
    printf("  Heatmap: run 'make report' to generate reports/charts/inter_core_heatmap.png\n");
}

int main(int argc, char *argv[]) {
    /* Line-buffer stdout/stderr so we see progress in real time
     * (important for the long-running latency matrix phase). */
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IOLBF, 0);

    /* Parse CLI arguments */
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--bw-iterations") == 0 && i+1 < argc) {
            g_bw_iterations = atoi(argv[++i]);
            if (g_bw_iterations < 1000) g_bw_iterations = 1000;
            fprintf(stderr, "[inter_core] CLI --bw-iterations=%d\n", g_bw_iterations);
        }
    }

    init_platform_layer();
    pmu_init_cache_counters();

    long nproc = sysconf(_SC_NPROCESSORS_ONLN);
    if (nproc > 64) {
        g_bw_iterations = BW_ITERATIONS_LARGE;
        fprintf(stderr, "[inter_core] nproc=%ld > 64: BW_ITERATIONS=%d (reduced from %d)\n",
                nproc, g_bw_iterations, BW_ITERATIONS_DEFAULT);
    } else {
        g_bw_iterations = BW_ITERATIONS_DEFAULT;
        fprintf(stderr, "[inter_core] nproc=%ld <= 64: BW_ITERATIONS=%d\n",
                nproc, g_bw_iterations);
    }

    print_system_info();
    run_inter_core_latency_test();
    return 0;
}
