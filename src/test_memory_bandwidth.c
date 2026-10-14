/* Memory diagnostics: useful byte counts / one shared wall-clock interval.
 * Persistent workers, explicit warmup, calibrated repetitions and medians.
 * Scalar dependency-chain latency is separate from multi-worker throughput. */
#include "common.h"
#include "util.h"
#include "bench_protocol.h"
#include "memory_kernels.h"
#include <errno.h>

#define MAX_THREADS 256
typedef struct MemoryPool MemoryPool;
typedef struct { MemoryPool *pool; unsigned index; double result; } MemoryWorker;
struct MemoryPool {
    pthread_mutex_t mutex;
    pthread_cond_t wake, done;
    unsigned threads, generation, finished, ready, operation;
    int stopping;
    uint64_t passes;
    size_t words;
    double *a, *b, *c;
    pthread_t handles[MAX_THREADS];
    MemoryWorker workers[MAX_THREADS];
};

static void *memory_worker(void *opaque) {
    MemoryWorker *worker = opaque;
    MemoryPool *pool = worker->pool;
    size_t count = pool->words / pool->threads, offset = worker->index * count;
    stream_initialize(pool->a + offset, pool->b + offset, pool->c + offset, count);
    pthread_mutex_lock(&pool->mutex);
    ++pool->ready;
    pthread_cond_broadcast(&pool->done);
    unsigned generation = 0;
    while (!pool->stopping) {
        while (!pool->stopping && generation == pool->generation) pthread_cond_wait(&pool->wake, &pool->mutex);
        if (pool->stopping) break;
        generation = pool->generation;
        unsigned operation = pool->operation;
        uint64_t passes = pool->passes;
        pthread_mutex_unlock(&pool->mutex);
        worker->result = stream_kernel(pool->a + offset, pool->b + offset, pool->c + offset, count, operation, passes);
        pthread_mutex_lock(&pool->mutex);
        ++pool->finished;
        pthread_cond_broadcast(&pool->done);
    }
    pthread_mutex_unlock(&pool->mutex);
    return NULL;
}

static int memory_work(void *context, uint64_t repetitions) {
    MemoryPool *pool = context;
    if (pool->threads == 1) {
        pool->workers[0].result = stream_kernel(pool->a, pool->b, pool->c, pool->words, pool->operation, repetitions);
    } else {
        pthread_mutex_lock(&pool->mutex);
        pool->passes = repetitions;
        pool->finished = 0;
        ++pool->generation;
        pthread_cond_broadcast(&pool->wake);
        while (pool->finished < pool->threads) pthread_cond_wait(&pool->done, &pool->mutex);
        pthread_mutex_unlock(&pool->mutex);
    }
    for (unsigned i = 0; i < pool->threads; ++i)
        if (!isfinite(pool->workers[i].result) || pool->workers[i].result <= 0) return -1;
    return 0;
}

static void memory_stop(MemoryPool *pool, unsigned created) {
    pthread_mutex_lock(&pool->mutex);
    pool->stopping = 1;
    pthread_cond_broadcast(&pool->wake);
    pthread_mutex_unlock(&pool->mutex);
    for (unsigned i = 0; i < created; ++i) pthread_join(pool->handles[i], NULL);
    pthread_cond_destroy(&pool->wake);
    pthread_cond_destroy(&pool->done);
    pthread_mutex_destroy(&pool->mutex);
}

int run_memory_bandwidth_test(size_t bytes, int threads) {
    if (threads < 1 || threads > MAX_THREADS || bytes < (size_t)threads * 256 || bytes % ((size_t)threads * 256)) return 2;
    MemoryPool pool = {.mutex = PTHREAD_MUTEX_INITIALIZER, .wake = PTHREAD_COND_INITIALIZER,
                      .done = PTHREAD_COND_INITIALIZER, .threads = (unsigned)threads, .words = bytes / sizeof(double)};
    pool.a = malloc(bytes); pool.b = malloc(bytes); pool.c = malloc(bytes);
    unsigned created = 0;
    int status = 1;
    if (!pool.a || !pool.b || !pool.c) goto cleanup;
    if (threads == 1) stream_initialize(pool.a, pool.b, pool.c, pool.words);
    else {
        for (unsigned i = 0; i < pool.threads; ++i) {
            pool.workers[i] = (MemoryWorker){&pool, i, 0};
            if (pthread_create(&pool.handles[i], NULL, memory_worker, &pool.workers[i])) goto cleanup;
            ++created;
        }
        pthread_mutex_lock(&pool.mutex);
        while (pool.ready < pool.threads) pthread_cond_wait(&pool.done, &pool.mutex);
        pthread_mutex_unlock(&pool.mutex);
    }
    BenchMeasurement bandwidth[STREAM_OPERATIONS], latency;
    for (unsigned i = 0; i < STREAM_OPERATIONS; ++i) {
        pool.operation = i;
        printf("Warming and sampling: %s\n", stream_names[i]); fflush(stdout);
        if (bench_measure(memory_work, &pool, &bandwidth[i])) goto cleanup;
    }
    /* Retire the workers before using their destination allocation as a chain. */
    memory_stop(&pool, created);
    created = 0;
    chain_build(pool.c, bytes);
    ChainWork chain = {pool.c, bytes, (uintptr_t)pool.c};
    printf("Warming and sampling: dependent random access\n"); fflush(stdout);
    if (bench_measure(chain_work, &chain, &latency)) goto freed;
    ReportContext *report = report_init("memory_bandwidth", REPORT_FORMAT_MARKDOWN);
    if (!report) goto freed;
    report_write_system_info(report);
    report_section(report, "Memory Bandwidth Test Results");
    bench_report_conditions(report);
    report_write(report, "- Memory Size: %.6f MiB\n- Total Allocation: %.6f MiB\n- Thread Count: %d\n\n",
                 (double)bytes / MB, (double)bytes * 3 / MB, threads);
    report_write(report, "| Operation | Bandwidth (MB/s) | Time(ms) | Bytes | Spread(%%) |\n");
    report_write(report, "|-----------|------------------|----------|-------|-----------|\n");
    for (unsigned i = 0; i < STREAM_OPERATIONS; ++i) {
        double payload = (double)stream_bytes(i, pool.words / threads) * threads * bandwidth[i].repetitions;
        double rate = payload * 1000.0 / (double)bandwidth[i].elapsed_ns;
        report_write(report, "| %s | %.6f | %.6f | %.0f | %.3f |\n", stream_names[i], rate,
                     (double)bandwidth[i].elapsed_ns / 1e6, payload, bench_spread(&bandwidth[i]));
    }
    report_section(report, "Memory Latency");
    double loads = (double)latency.repetitions * CHAIN_STEPS;
    report_write(report, "| Operation | Size | ns/op | Time(ms) | Operations | Spread(%%) |\n");
    report_write(report, "|-----------|------|-------|----------|------------|-----------|\n");
    report_write(report, "| Dependent random read | %.6f MiB | %.6f | %.6f | %.0f | %.3f |\n",
                 (double)bytes / MB, (double)latency.elapsed_ns / loads,
                 (double)latency.elapsed_ns / 1e6, loads, bench_spread(&latency));
    report_section(report, "Notes");
    report_write(report, "MB/s is decimal useful bytes / common elapsed time, including worker release/completion overhead.\n"
                 "Read/write count one array; copy/scale two; add/triad three. Strides count 8 bytes per actual load.\n"
                 "No inferred cache-line, write-allocation or DRAM-bus traffic is added.\n"
                 "Arrays use ordinary OS pages and worker first touch, without CPU or NUMA pinning.\n"
                 "Latency uses one dependent load chain with 64-byte nodes across the full listed size.\n"
                 "Working sets may still be cache-resident; no forced-DRAM guarantee or inferred write latency.\n");
    report_finalize_json(report);
    status = report_finish(report) ? 1 : 0;
    goto freed;
cleanup:
    memory_stop(&pool, created);
freed:
    free(pool.a); free(pool.b); free(pool.c);
    return status;
}

static void print_usage(const char *name) {
    printf("Usage: %s [-s SIZE] [-t THREADS]\nThree arrays; total allocation limited to 50%% of available memory.\n", name);
}

int main(int argc, char **argv) {
    init_platform_layer();
    size_t available = detect_available_memory();
    if (!available) { fputs("Cannot determine a safe memory budget\n", stderr); return 1; }
    size_t cap = available / 6, bytes = 256 * MB;
    size_t last = global_cache_config.l3_total_size;
    if (!last) last = global_cache_config.l3_size > global_cache_config.l2_size
                      ? global_cache_config.l3_size : global_cache_config.l2_size;
    if (last > bytes / 4 && last <= SIZE_MAX / 4) bytes = last * 4;
    if (bytes > 2 * (size_t)GB) bytes = 2 * (size_t)GB;
    if (bytes > cap) bytes = cap;
    int threads = global_system_config.cpu_cores_physical;
    if (threads < 1) threads = (int)sysconf(_SC_NPROCESSORS_ONLN);
    if (threads < 1) threads = 1;
    if (threads > MAX_THREADS) threads = MAX_THREADS;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) { print_usage(argv[0]); return 0; }
        if ((!strcmp(argv[i], "-s") || !strcmp(argv[i], "--size")) && i + 1 < argc) bytes = parse_size(argv[++i]);
        else if ((!strcmp(argv[i], "-t") || !strcmp(argv[i], "--threads")) && i + 1 < argc) {
            char *end; errno = 0; long value = strtol(argv[++i], &end, 10);
            if (errno || *end || value < 1 || value > MAX_THREADS) return 2;
            threads = (int)value;
        } else { print_usage(argv[0]); return 2; }
    }
    if (!bytes || bytes > SIZE_MAX / 3) return 2;
    if (bytes > cap) bytes = cap;
    size_t quantum = (size_t)threads * 256;
    bytes -= bytes % quantum;
    if (bytes < quantum) return 2;
    print_system_info();
    return run_memory_bandwidth_test(bytes, threads);
}
