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
#if defined(__aarch64__)
#include <arm_neon.h>
#elif defined(__x86_64__)
#include <immintrin.h>
#endif
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
static volatile double float_seed[32];
static double float_reference[32];
static _Thread_local double float_values[32];
static unsigned float_lanes = 16;
static const char *float_backend = "scalar";

static uint64_t integer_work(void) {
    uint64_t x = integer_seed;
    for (unsigned i = 0; i < 65536; ++i) {
        x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    }
    return x;
}

/* Eight independent vector recurrence chains keep the FP execution units busy.
 * Inputs are loaded afresh each batch; every lane is checked, not only a sum.
 * Values remain normal/finite. A batch is bounded for prompt mode switching. */
#define VECTOR_WORK(NAME, ATTR, TYPE, WIDTH, LOAD, STORE, SPLAT, STEP) \
ATTR static void NAME(void) { \
    double seeds[8 * WIDTH]; \
    for (unsigned i = 0; i < 8 * WIDTH; ++i) seeds[i] = float_seed[i]; \
    TYPE a0 = LOAD(seeds), a1 = LOAD(seeds + WIDTH); \
    TYPE a2 = LOAD(seeds + 2 * WIDTH), a3 = LOAD(seeds + 3 * WIDTH); \
    TYPE a4 = LOAD(seeds + 4 * WIDTH), a5 = LOAD(seeds + 5 * WIDTH); \
    TYPE a6 = LOAD(seeds + 6 * WIDTH), a7 = LOAD(seeds + 7 * WIDTH); \
    TYPE m = SPLAT(1.0000001), b = SPLAT(0.0000001); \
    for (unsigned i = 0; i < 65536; ++i) { \
        a0 = STEP(a0, m, b); a1 = STEP(a1, m, b); \
        a2 = STEP(a2, m, b); a3 = STEP(a3, m, b); \
        a4 = STEP(a4, m, b); a5 = STEP(a5, m, b); \
        a6 = STEP(a6, m, b); a7 = STEP(a7, m, b); \
    } \
    STORE(float_values, a0); STORE(float_values + WIDTH, a1); \
    STORE(float_values + 2 * WIDTH, a2); STORE(float_values + 3 * WIDTH, a3); \
    STORE(float_values + 4 * WIDTH, a4); STORE(float_values + 5 * WIDTH, a5); \
    STORE(float_values + 6 * WIDTH, a6); STORE(float_values + 7 * WIDTH, a7); \
}

#if defined(__aarch64__)
#define NEON_STEP(a, m, b) vfmaq_f64(b, a, m)
VECTOR_WORK(float_vector, , float64x2_t, 2, vld1q_f64, vst1q_f64, vdupq_n_f64, NEON_STEP)
#elif defined(__x86_64__)
#define SSE_STEP(a, m, b) _mm_add_pd(_mm_mul_pd(a, m), b)
VECTOR_WORK(float_vector, , __m128d, 2, _mm_loadu_pd, _mm_storeu_pd, _mm_set1_pd, SSE_STEP)
#if defined(__GNUC__) || defined(__clang__)
VECTOR_WORK(float_avx, __attribute__((target("avx2,fma"))), __m256d, 4,
            _mm256_loadu_pd, _mm256_storeu_pd, _mm256_set1_pd, _mm256_fmadd_pd)
#endif
#else
static void float_vector(void) {
    for (unsigned j = 0; j < 16; ++j) float_values[j] = float_seed[j];
    for (unsigned i = 0; i < 65536; ++i)
        for (unsigned j = 0; j < 16; ++j)
            float_values[j] = float_values[j] * 1.0000001 + 0.0000001;
}
#endif
static void (*float_kernel)(void) = float_vector;

static void init_float(void) {
    /* A closed-form long-double reference is independent of the SIMD loop. */
    long double factor = powl(1.0000001L, 65536);
    for (unsigned i = 0; i < 32; ++i) {
        float_seed[i] = (i + 1) * 0.25;
        float_reference[i] = (double)(((i + 1) * 0.25L + 1) * factor - 1);
    }
#if defined(__aarch64__)
    float_backend = "NEON FMA";
#elif defined(__x86_64__)
    float_backend = "SSE2";
#if defined(__GNUC__) || defined(__clang__)
    __builtin_cpu_init();
    if (__builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma")) {
        float_kernel = float_avx;
        float_lanes = 32;
        float_backend = "AVX2 FMA";
    }
#endif
#endif
}

static double float_work(void) {
    float_kernel();
    for (unsigned i = 0; i < float_lanes; ++i)
        if (!isfinite(float_values[i]) || fabs(float_values[i] - float_reference[i]) > 1e-9)
            return NAN;
    return float_values[0] + float_values[1] + float_values[2] + float_values[3];
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
    init_float();
    if (argc == 2 && !strcmp(argv[1], "--self-test")) {
        uint64_t integer = integer_work();
        double fp = float_work();
        printf("{\"integer\":\"%016" PRIx64 "\",\"float\":%.17g,\"backend\":\"%s\",\"lanes\":[",
               integer, fp, float_backend);
        for (unsigned i = 0; i < float_lanes; ++i) printf("%s%.17g", i ? "," : "", float_values[i]);
        puts("]}");
        return integer == integer_expected && isfinite(fp) && fabs(fp - float_expected) <= 1e-9 ? 0 : 1;
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
