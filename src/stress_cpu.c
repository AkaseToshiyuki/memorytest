/* Independent stress workloads. No ComputeMark code, scores or hardware writes. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef _WIN32
#include "windows_compat.h"
#else
#include <pthread.h>
#include <unistd.h>
#endif

enum { CPU = 1, FPU = 2, CACHE = 4, RAM = 8 };
static atomic_int modes, stopping, failed;
static size_t ram_per_thread;
static const size_t cache_bytes = 64 * 1024;
/* Volatile inputs prevent the optimizer from hoisting an invariant workload. */
static volatile uint64_t integer_seed = UINT64_C(0x123456789abcdef0);
static volatile double float_seed[4] = {0.25, 0.5, 0.75, 1.0};

static uint64_t integer_work(void) {
    uint64_t x = integer_seed;
    for (unsigned i = 0; i < 65536; ++i) {
        x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    }
    return x;
}

static double float_work(void) {
    double x[4] = {float_seed[0], float_seed[1], float_seed[2], float_seed[3]};
    for (unsigned i = 0; i < 65536; ++i)
        for (unsigned j = 0; j < 4; ++j) x[j] = x[j] * 1.0000001 + 0.0000001;
    return x[0] + x[1] + x[2] + x[3];
}

/* Expected results are independently generated in tests/test_stress.py. */
static const uint64_t integer_expected = UINT64_C(0xd1b04aa9e63ddb2b);
static const double float_expected = 2.5427382897343546;

static void failure(const char *code) {
    if (!atomic_exchange(&failed, 1)) {
        printf("error %s\n", code);
        fflush(stdout);
    }
    atomic_store(&stopping, 1);
}

static int memory_work(volatile uint64_t *buffer, size_t words, size_t *cursor, uint64_t seed) {
    size_t count = words < 65536 ? words : 65536;
    if (*cursor + count > words) *cursor = 0;
    for (size_t i = 0; i < count; ++i) buffer[*cursor + i] = (uint64_t)(*cursor + i) ^ seed;
    for (size_t i = 0; i < count; ++i)
        if (buffer[*cursor + i] != ((uint64_t)(*cursor + i) ^ seed)) return -1;
    *cursor += count;
    return 0;
}

static void pause_idle(void) {
#ifdef _WIN32
    Sleep(10);
#else
    struct timespec wait = {0, 10000000};
    nanosleep(&wait, NULL);
#endif
}

static void *worker(void *unused) {
    (void)unused;
    volatile uint64_t *cache = NULL, *ram = NULL;
    size_t cache_cursor = 0, ram_cursor = 0;
    uint64_t seed = UINT64_C(0xaaaaaaaa55555555);
    while (!atomic_load(&stopping)) {
        int selected = atomic_load(&modes);
        if (!(selected & CACHE) && cache) { free((void *)cache); cache = NULL; }
        if (!(selected & RAM) && ram) { free((void *)ram); ram = NULL; }
        if (!selected) { pause_idle(); continue; }
        if ((selected & CPU) && integer_work() != integer_expected) { failure("calculation_error"); break; }
        if (selected & FPU) {
            double value = float_work();
            if (!isfinite(value) || fabs(value - float_expected) > 1e-9) { failure("calculation_error"); break; }
        }
        if (selected & CACHE) {
            if (!cache) cache = malloc(cache_bytes);
            if (!cache) { failure("memory_error"); break; }
            if (memory_work(cache, cache_bytes / 8, &cache_cursor, seed)) { failure("calculation_error"); break; }
        }
        if (selected & RAM) {
            if (!ram) ram = malloc(ram_per_thread);
            if (!ram) { failure("memory_error"); break; }
            if (memory_work(ram, ram_per_thread / 8, &ram_cursor, seed)) { failure("calculation_error"); break; }
        }
        seed = ~seed;
    }
    free((void *)cache);
    free((void *)ram);
    return NULL;
}

static int positive(const char *text, long maximum, long *value) {
    char *end;
    errno = 0;
    *value = strtol(text, &end, 10);
    return !errno && *text && !*end && *value >= 1 && *value <= maximum;
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--self-test")) {
        uint64_t integer = integer_work();
        double fp = float_work();
        printf("{\"integer\":\"%016" PRIx64 "\",\"float\":%.17g}\n", integer, fp);
        return integer == integer_expected && fabs(fp - float_expected) <= 1e-9 ? 0 : 1;
    }
    if (argc != 3) return 2;
    long threads, memory_mib;
    if (!positive(argv[1], 256, &threads) || !positive(argv[2], 1048576, &memory_mib)) return 2;
    ram_per_thread = ((size_t)memory_mib * 1024 * 1024 / (size_t)threads) & ~(size_t)7;
    if (ram_per_thread < 8) return 2;
    pthread_t *workers = calloc((size_t)threads, sizeof(*workers));
    if (!workers) return 1;
    int created = 0;
    for (; created < threads; ++created) {
        if (pthread_create(&workers[created], NULL, worker, NULL)) { failure("backend_error"); break; }
    }
    printf("ready\n"); fflush(stdout);
    char command[64];
    while (!atomic_load(&stopping) && fgets(command, sizeof(command), stdin)) {
        char *end;
        long mask = strtol(command, &end, 10);
        if (end == command || (*end != '\n' && *end != '\0') || mask < 0 || mask > 15) { failure("backend_error"); break; }
        atomic_store(&modes, (int)mask);
        printf("modes %ld\n", mask); fflush(stdout);
    }
    atomic_store(&stopping, 1);
    for (int i = 0; i < created; ++i) pthread_join(workers[i], NULL);
    free(workers);
    return atomic_load(&failed) ? 1 : 0;
}
