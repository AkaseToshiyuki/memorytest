/* Floating-point diagnostics. Every named operation contributes to a retained
 * result. Bounded blocks avoid overflow, underflow and disappearing sqrt/div
 * loops. Counts describe arithmetic calls/operations, not retired instructions.
 * This module is deliberately separate from ComputeMark's scoring kernels. */
#include "common.h"
#include "util.h"
#define BENCH_DEFAULT_SAMPLES 50
#include "bench_protocol.h"
#include <math.h>
#if defined(HAVE_SSE2_INTRINSICS)
#include <emmintrin.h>
#endif

enum { FP_STEPS = 64 };
static volatile double fp_seed[8] = {1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75};
static volatile double fp_exponent = 1.125;
static volatile double fp_result;
static double fp_lanes[8];
typedef void (*FloatKernel)(uint64_t);

/* Four independent dependency chains. All four results from EVERY block are
 * consumed; volatile seed reads prevent hoisting repeated identical blocks. */
#define SCALAR_RECURRENCE(NAME, TYPE, EXPR) \
static void NAME(uint64_t blocks) { \
    double total = 0; \
    for (uint64_t block = 0; block < blocks; ++block) { \
        TYPE x[4] = {(TYPE)fp_seed[0], (TYPE)fp_seed[1], (TYPE)fp_seed[2], (TYPE)fp_seed[3]}; \
        for (unsigned i = 0; i < FP_STEPS; ++i) \
            for (unsigned j = 0; j < 4; ++j) x[j] = (EXPR); \
        for (unsigned j = 0; j < 4; ++j) { fp_lanes[j] = x[j]; total += x[j]; } \
    } \
    fp_result = total; \
}
SCALAR_RECURRENCE(float_add, float, x[j] + 0.125f)
SCALAR_RECURRENCE(float_mul, float, x[j] * 1.0001f)
SCALAR_RECURRENCE(float_div, float, x[j] / 1.0001f)
SCALAR_RECURRENCE(double_add, double, x[j] + 0.125)
SCALAR_RECURRENCE(double_mul, double, x[j] * 1.0001)
SCALAR_RECURRENCE(double_div, double, x[j] / 1.0001)
SCALAR_RECURRENCE(float_mixed, double, sqrt((x[j] + 0.5) * 1.0001))

/* Independent inputs and four reductions keep every sqrt/libm call observable.
 * The timer covers input formation and reduction overhead as well as the call. */
#define SCALAR_MAP(NAME, TYPE, EXPR) \
static void NAME(uint64_t blocks) { \
    double total = 0; \
    for (uint64_t block = 0; block < blocks; ++block) { \
        TYPE base = (TYPE)fp_seed[0], sum[4] = {0}; \
        double exponent = fp_exponent; (void)exponent; \
        for (unsigned i = 0; i < FP_STEPS; ++i) \
            for (unsigned j = 0; j < 4; ++j) { \
                TYPE input = base + (TYPE)(4 * i + j) * (TYPE)0.125; \
                sum[j] += (EXPR); \
            } \
        for (unsigned j = 0; j < 4; ++j) { fp_lanes[j] = sum[j]; total += sum[j]; } \
    } \
    fp_result = total; \
}
SCALAR_MAP(float_sqrt, float, sqrtf(input))
SCALAR_MAP(double_sqrt, double, sqrt(input))
SCALAR_MAP(double_sin, double, sin(input))
SCALAR_MAP(double_log, double, log(input))
SCALAR_MAP(double_pow, double, pow(input, exponent))

#if defined(__aarch64__)
typedef float64x2_t FPVector;
#define FP_LOAD vld1q_f64
#define FP_STORE vst1q_f64
#define FP_SET vdupq_n_f64
#define FP_ADD vaddq_f64
#define FP_MUL vmulq_f64
#define FP_FMA(a, m, b) vfmaq_f64(b, a, m)
#define FP_SIMD 1
#define FP_HAS_FMA 1
#elif defined(HAVE_SSE2_INTRINSICS)
typedef __m128d FPVector;
#define FP_LOAD _mm_loadu_pd
#define FP_STORE _mm_storeu_pd
#define FP_SET _mm_set1_pd
#define FP_ADD _mm_add_pd
#define FP_MUL _mm_mul_pd
#define FP_SIMD 1
#endif

#ifdef FP_SIMD
#define VECTOR_RECURRENCE(NAME, EXPR) \
static void NAME(uint64_t blocks) { \
    double total = 0; \
    for (uint64_t block = 0; block < blocks; ++block) { \
        double seeds[8]; \
        for (unsigned j = 0; j < 8; ++j) seeds[j] = fp_seed[j]; \
        FPVector x[4] = {FP_LOAD(seeds), FP_LOAD(seeds + 2), FP_LOAD(seeds + 4), FP_LOAD(seeds + 6)}; \
        FPVector step = FP_SET(0.125), factor = FP_SET(1.0001); \
        (void)step; (void)factor; \
        for (unsigned i = 0; i < FP_STEPS; ++i) \
            for (unsigned j = 0; j < 4; ++j) x[j] = (EXPR); \
        for (unsigned j = 0; j < 4; ++j) FP_STORE(fp_lanes + 2 * j, x[j]); \
        for (unsigned j = 0; j < 8; ++j) total += fp_lanes[j]; \
    } \
    fp_result = total; \
}
VECTOR_RECURRENCE(simd_add, FP_ADD(x[j], step))
VECTOR_RECURRENCE(simd_mul, FP_MUL(x[j], factor))
static void simd_dot(uint64_t blocks) {
    double total = 0;
    for (uint64_t block = 0; block < blocks; ++block) {
        double seeds[8];
        for (unsigned j = 0; j < 8; ++j) seeds[j] = fp_seed[j];
        FPVector a[4] = {FP_LOAD(seeds), FP_LOAD(seeds + 2), FP_LOAD(seeds + 4), FP_LOAD(seeds + 6)};
        FPVector x[4] = {FP_SET(0), FP_SET(0), FP_SET(0), FP_SET(0)};
        for (unsigned i = 0; i < FP_STEPS; ++i) {
            FPVector b = FP_SET((double)i + 1);
            for (unsigned j = 0; j < 4; ++j) {
                x[j] = FP_ADD(x[j], FP_MUL(a[j], b));
                a[j] = FP_ADD(a[j], FP_SET(0.125));
            }
        }
        for (unsigned j = 0; j < 4; ++j) FP_STORE(fp_lanes + 2 * j, x[j]);
        for (unsigned j = 0; j < 8; ++j) total += fp_lanes[j];
    }
    fp_result = total;
}
#ifdef FP_HAS_FMA
VECTOR_RECURRENCE(simd_fma, FP_FMA(x[j], factor, step))
#else
VECTOR_RECURRENCE(simd_muladd, FP_ADD(FP_MUL(x[j], factor), step))
#endif
#endif

typedef struct { const char *name, *type; FloatKernel kernel; unsigned operations, lanes; } FloatTest;
static const FloatTest float_tests[] = {
    {"float Add", "float", float_add, 256, 4},
    {"float Mul", "float", float_mul, 256, 4},
    {"float Div", "float", float_div, 256, 4},
    {"float Sqrt", "float", float_sqrt, 256, 4},
    {"double Add", "double", double_add, 256, 4},
    {"double Mul", "double", double_mul, 256, 4},
    {"double Div", "double", double_div, 256, 4},
    {"double Sqrt", "double", double_sqrt, 256, 4},
    {"double Sin", "double", double_sin, 256, 4},
    {"double Log", "double", double_log, 256, 4},
    {"double Pow", "double", double_pow, 256, 4},
    {"Mixed", "double", float_mixed, 768, 4},
#ifdef FP_SIMD
    {"SIMD FAdd", "simd", simd_add, 512, 8},
    {"SIMD FMul", "simd", simd_mul, 512, 8},
    {"SIMD Dot", "simd", simd_dot, 1024, 8},
#ifdef FP_HAS_FMA
    {"SIMD FMA", "simd", simd_fma, 1024, 8},
#else
    {"SIMD Mul+Add", "simd", simd_muladd, 1024, 8},
#endif
#endif
};

static int float_work(void *context, uint64_t repetitions) {
    const FloatTest *test = context;
    test->kernel(repetitions);
    if (!isfinite(fp_result) || fp_result == 0) return -1;
    for (unsigned i = 0; i < test->lanes; ++i)
        if (!isfinite(fp_lanes[i]) || !isnormal(fp_lanes[i])) return -1;
    return 0;
}

int run_cpu_float_test(void) {
    enum { count = sizeof(float_tests) / sizeof(*float_tests) };
    BenchMeasurement results[count];
    for (unsigned i = 0; i < count; ++i) {
        printf("Warming and sampling: %s\n", float_tests[i].name); fflush(stdout);
        if (bench_measure(float_work, (void *)&float_tests[i], &results[i])) return 1;
    }
    ReportContext *report = report_init("cpu_float", REPORT_FORMAT_MARKDOWN);
    if (!report) return 1;
    report_write_system_info(report);
    report_section(report, "Floating Point Operations Test Results");
    bench_report_conditions(report);
    report_write(report, "| Operation | Type | Time(ms) | Ops/sec | ns/op | Operations | Spread(%%) |\n");
    report_write(report, "|-----------|------|----------|---------|-------|------------|-----------|\n");
    for (unsigned i = 0; i < count; ++i) {
        double operations = (double)results[i].repetitions * float_tests[i].operations;
        double ns = (double)results[i].elapsed_ns / operations;
        printf("%s: %.6f ns/op (%d samples)\n", float_tests[i].name, ns, BENCH_SAMPLES);
        report_write(report, "| %s | %s | %.9f | %.0f | %.6f | %.0f | %.3f |\n",
                     float_tests[i].name, float_tests[i].type, (double)results[i].elapsed_ns / 1e6,
                     1e9 / ns, ns, operations, bench_spread(&results[i]));
    }
    report_section(report, "Notes");
    report_write(report, "Named add/mul/div/sqrt/libm calls count once per scalar lane; multiply-add counts twice.\n"
                 "Mixed counts add, multiply and sqrt. SIMD counts actual lanes, not loop-counter increments.\n"
                 "Timing includes loop, input, reduction and result-validation overhead. Not peak instruction throughput.\n"
                 "No CPI/IPC is derived from nominal frequency. Unsupported SIMD is omitted.\n");
    FILE *audit = bench_audit_begin(report);
    if (!audit) { report_free(report); return 1; }
    for (unsigned i = 0; i < count; ++i) bench_audit_row(report, audit, float_tests[i].name, &results[i], i);
    if (bench_audit_finish(audit)) { report_free(report); return 1; }
    report_finalize_json(report);
    return report_finish(report) ? 1 : 0;
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    init_platform_layer();
    print_system_info();
    return run_cpu_float_test();
}
