/**
 * CPU Floating Point Operations Test
 *
 * 测试CPU浮点运算性能:
 * - 单精度/双精度浮点运算
 * - SIMD向量化检测
 * - CPI/IPC测量 (使用PMU)
 *
 * 跨平台支持: X86_64, ARM64, RISC-V, PowerPC
 */

#include "common.h"
#include "util.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>

#define ITERATIONS 100000000
#define WARMUP_ITERATIONS 1000000

/* ========== Thread-safe result storage ========== */
static volatile double g_result_f = 0.0;
static volatile double g_result_d = 0.0;

/* ========== 浮点运算测试 ========== */

/* 单精度加法测试 */
static void test_float_add(void *arg) {
    (void)arg;
    float result = 0.0f;
    for (double i = 0; i < ITERATIONS; i++) {
        result += (float)i * 0.5f;
    }
    g_result_f = result;
}

/* 单精度乘法测试 */
static void test_float_mul(void *arg) {
    (void)arg;
    float result = 1.0f;
    for (double i = 1; i < ITERATIONS; i++) {
        result *= (float)i * 0.00001f;
        if (result < 0.0001f) result = 1.0f;
    }
    g_result_f = result;
}

/* 单精度除法测试 */
static void test_float_div(void *arg) {
    (void)arg;
    float result = 1.0f;
    volatile float dividend = 1000000.0f;
    for (double i = 1; i < ITERATIONS; i++) {
        result = dividend / (float)i;
    }
    g_result_f = result;
}

/* 单精度平方根测试 */
static void test_float_sqrt(void *arg) {
    (void)arg;
    float result = 0.0f;
    for (double i = 1; i < ITERATIONS; i++) {
        result = sqrtf((float)i);
    }
    g_result_f = result;
}

/* 双精度加法测试 */
static void test_double_add(void *arg) {
    (void)arg;
    double result = 0.0;
    for (double i = 0; i < ITERATIONS; i++) {
        result += i * 0.5;
    }
    g_result_d = result;
}

/* 双精度乘法测试 */
static void test_double_mul(void *arg) {
    (void)arg;
    double result = 1.0;
    for (double i = 1; i < ITERATIONS; i++) {
        result *= i * 0.00001;
        if (result < 0.0001) result = 1.0;
    }
    g_result_d = result;
}

/* 双精度除法测试 */
static void test_double_div(void *arg) {
    (void)arg;
    double result = 1.0;
    volatile double dividend = 1000000.0;
    for (double i = 1; i < ITERATIONS; i++) {
        result = dividend / i;
    }
    g_result_d = result;
}

/* 双精度平方根测试 */
static void test_double_sqrt(void *arg) {
    (void)arg;
    double result = 0.0;
    for (double i = 1; i < ITERATIONS; i++) {
        result = sqrt(i);
    }
    g_result_d = result;
}

/* 双精度正弦测试 */
static void test_double_sin(void *arg) {
    (void)arg;
    double result = 0.0;
    for (double i = 1; i < ITERATIONS; i++) {
        result += sin(i * 0.00001);
    }
    g_result_d = result;
}

/* 双精度对数测试 */
static void test_double_log(void *arg) {
    (void)arg;
    double result = 0.0;
    for (double i = 1; i < ITERATIONS; i++) {
        result += log(i);
    }
    g_result_d = result;
}

/* 双精度幂运算测试 */
static void test_double_pow(void *arg) {
    (void)arg;
    double result = 1.0;
    for (double i = 1; i < ITERATIONS; i++) {
        result = pow(result, 0.5);  /* sqrt via pow */
        if (result < 0.1) result = 2.0;
    }
    g_result_d = result;
}

/* 浮点混合运算测试 (真实工作负载) */
static void test_float_mixed(void *arg) {
    (void)arg;
    double result = 0.0;
    for (double i = 1; i < ITERATIONS; i++) {
        result += i * 0.5;
        result *= 1.00001;
        result = sqrt(result);
        if (result > 1000.0) result = 1.0;
    }
    g_result_d = result;
}

/* ========== SIMD (NEON) tests ==========
 *
 * Each SIMD test processes 2 doubles per iteration via 128-bit NEON.
 * 100M iterations × 2 doubles = 200M ops total (same magnitude as scalar).
 * IPC should be 2x scalar (1 op/cycle) on out-of-order cores, since NEON
 * fadd/fmul are 1-cycle latency / 2-issue throughput on Apple/ARM cores.
 */
static volatile double g_simd_result = 0.0;

#if defined(HAVE_NEON_INTRINSICS)
static void test_simd_fadd(void *arg) {
    (void)arg;
    float64x2_t v_sum = vdupq_n_f64(0.0);
    float64x2_t v_half = vdupq_n_f64(0.5);
    for (long i = 0; i < ITERATIONS; i += 2) {
        float64x2_t v_i = vdupq_n_f64((double)i);
        v_sum = vmlaq_f64(v_sum, v_i, v_half);
    }
    double buf[2];
    vst1q_f64(buf, v_sum);
    g_simd_result = buf[0] + buf[1];
}
static void test_simd_fmul(void *arg) {
    (void)arg;
    float64x2_t v_prod = vdupq_n_f64(1.0);
    float64x2_t v_step = vdupq_n_f64(0.99999);
    for (long i = 0; i < ITERATIONS; i += 2) {
        v_prod = vmulq_f64(v_prod, v_step);
    }
    double buf[2];
    vst1q_f64(buf, v_prod);
    g_simd_result = buf[0] + buf[1];
}
static void test_simd_dot(void *arg) {
    (void)arg;
    float64x2_t v_sum = vdupq_n_f64(0.0);
    for (long i = 0; i < ITERATIONS; i += 2) {
        float64x2_t a = vdupq_n_f64((double)i);
        float64x2_t b = vdupq_n_f64((double)(i + 1));
        /* vdotq equivalent: pairwise multiply + horizontal add */
        float64x2_t p = vmulq_f64(a, b);
        v_sum = vaddq_f64(v_sum, p);
    }
    double buf[2];
    vst1q_f64(buf, v_sum);
    g_simd_result = buf[0] + buf[1];
}
static void test_simd_fma(void *arg) {
    (void)arg;
    /* Fused multiply-add: 2 muls + 2 adds in 1 cycle each on modern ARM */
    float64x2_t v_acc = vdupq_n_f64(0.0);
    float64x2_t v_a   = vdupq_n_f64(1.00001);
    float64x2_t v_b   = vdupq_n_f64(0.5);
    for (long i = 0; i < ITERATIONS; i += 2) {
        v_acc = vfmaq_f64(v_acc, v_a, v_b);
    }
    double buf[2];
    vst1q_f64(buf, v_acc);
    g_simd_result = buf[0] + buf[1];
}
#elif defined(HAVE_AVX2_FMA_INTRINSICS)
/* AVX2 + FMA path: full 128-bit double FMA via _mm_fmadd_pd.
 * Only enabled when the binary was compiled with both -mavx2 and -mfma.
 * Falls back to the scalar path (HAVE_SSE2_INTRINSICS) when those flags
 * are absent, so a portable -O2 build still compiles. */
static void test_simd_fadd(void *arg) {
    (void)arg;
    __m128d v_sum = _mm_setzero_pd();
    __m128d v_half = _mm_set1_pd(0.5);
    for (long i = 0; i < ITERATIONS; i += 2) {
        __m128d v_i = _mm_set1_pd((double)i);
        v_sum = _mm_add_pd(v_sum, _mm_mul_pd(v_i, v_half));
    }
    double buf[2];
    _mm_storeu_pd(buf, v_sum);
    g_simd_result = buf[0] + buf[1];
}
static void test_simd_fmul(void *arg) {
    (void)arg;
    __m128d v_prod = _mm_set1_pd(1.0);
    __m128d v_step = _mm_set1_pd(0.99999);
    for (long i = 0; i < ITERATIONS; i += 2) {
        v_prod = _mm_mul_pd(v_prod, v_step);
    }
    double buf[2];
    _mm_storeu_pd(buf, v_prod);
    g_simd_result = buf[0] + buf[1];
}
static void test_simd_dot(void *arg) {
    (void)arg;
    __m128d v_sum = _mm_setzero_pd();
    for (long i = 0; i < ITERATIONS; i += 2) {
        __m128d a = _mm_set1_pd((double)i);
        __m128d b = _mm_set1_pd((double)(i + 1));
        __m128d p = _mm_mul_pd(a, b);
        v_sum = _mm_add_pd(v_sum, p);
    }
    double buf[2];
    _mm_storeu_pd(buf, v_sum);
    g_simd_result = buf[0] + buf[1];
}
static void test_simd_fma(void *arg) {
    (void)arg;
    __m128d v_acc = _mm_setzero_pd();
    __m128d v_a   = _mm_set1_pd(1.00001);
    __m128d v_b   = _mm_set1_pd(0.5);
    for (long i = 0; i < ITERATIONS; i += 2) {
        v_acc = _mm_fmadd_pd(v_acc, v_a, v_b);
    }
    double buf[2];
    _mm_storeu_pd(buf, v_acc);
    g_simd_result = buf[0] + buf[1];
}
#elif defined(HAVE_AVX_INTRINSICS)
/* AVX-only path: can use 128-bit SSE intrinsics from <immintrin.h> but
 * no FMA. */
static void test_simd_fadd(void *arg) {
    (void)arg;
    __m128d v_sum = _mm_setzero_pd();
    __m128d v_half = _mm_set1_pd(0.5);
    for (long i = 0; i < ITERATIONS; i += 2) {
        __m128d v_i = _mm_set1_pd((double)i);
        v_sum = _mm_add_pd(v_sum, _mm_mul_pd(v_i, v_half));
    }
    double buf[2];
    _mm_storeu_pd(buf, v_sum);
    g_simd_result = buf[0] + buf[1];
}
static void test_simd_fmul(void *arg) {
    (void)arg;
    __m128d v_prod = _mm_set1_pd(1.0);
    __m128d v_step = _mm_set1_pd(0.99999);
    for (long i = 0; i < ITERATIONS; i += 2) {
        v_prod = _mm_mul_pd(v_prod, v_step);
    }
    double buf[2];
    _mm_storeu_pd(buf, v_prod);
    g_simd_result = buf[0] + buf[1];
}
static void test_simd_dot(void *arg) {
    (void)arg;
    __m128d v_sum = _mm_setzero_pd();
    for (long i = 0; i < ITERATIONS; i += 2) {
        __m128d a = _mm_set1_pd((double)i);
        __m128d b = _mm_set1_pd((double)(i + 1));
        __m128d p = _mm_mul_pd(a, b);
        v_sum = _mm_add_pd(v_sum, p);
    }
    double buf[2];
    _mm_storeu_pd(buf, v_sum);
    g_simd_result = buf[0] + buf[1];
}
/* FMA without FMA ISA: fall back to mul+add (slower but correct). */
static void test_simd_fma(void *arg) {
    (void)arg;
    __m128d v_acc = _mm_setzero_pd();
    __m128d v_a   = _mm_set1_pd(1.00001);
    __m128d v_b   = _mm_set1_pd(0.5);
    for (long i = 0; i < ITERATIONS; i += 2) {
        v_acc = _mm_add_pd(v_acc, _mm_mul_pd(v_a, v_b));
    }
    double buf[2];
    _mm_storeu_pd(buf, v_acc);
    g_simd_result = buf[0] + buf[1];
}
#elif defined(HAVE_SSE2_INTRINSICS)
/* SSE2 baseline: x86_64 always has SSE2. The C compiler can be told to
 * emit SSE2 code via -msse2 (default on x86_64). <emmintrin.h> provides
 * the 128-bit integer/float intrinsics that work on any x86_64. */
#include <emmintrin.h>
static void test_simd_fadd(void *arg) {
    (void)arg;
    __m128d v_sum = _mm_setzero_pd();
    __m128d v_half = _mm_set1_pd(0.5);
    for (long i = 0; i < ITERATIONS; i += 2) {
        __m128d v_i = _mm_set1_pd((double)i);
        v_sum = _mm_add_pd(v_sum, _mm_mul_pd(v_i, v_half));
    }
    double buf[2];
    _mm_storeu_pd(buf, v_sum);
    g_simd_result = buf[0] + buf[1];
}
static void test_simd_fmul(void *arg) {
    (void)arg;
    __m128d v_prod = _mm_set1_pd(1.0);
    __m128d v_step = _mm_set1_pd(0.99999);
    for (long i = 0; i < ITERATIONS; i += 2) {
        v_prod = _mm_mul_pd(v_prod, v_step);
    }
    double buf[2];
    _mm_storeu_pd(buf, v_prod);
    g_simd_result = buf[0] + buf[1];
}
static void test_simd_dot(void *arg) {
    (void)arg;
    __m128d v_sum = _mm_setzero_pd();
    for (long i = 0; i < ITERATIONS; i += 2) {
        __m128d a = _mm_set1_pd((double)i);
        __m128d b = _mm_set1_pd((double)(i + 1));
        __m128d p = _mm_mul_pd(a, b);
        v_sum = _mm_add_pd(v_sum, p);
    }
    double buf[2];
    _mm_storeu_pd(buf, v_sum);
    g_simd_result = buf[0] + buf[1];
}
/* FMA without FMA: fall back to mul+add. */
static void test_simd_fma(void *arg) {
    (void)arg;
    __m128d v_acc = _mm_setzero_pd();
    __m128d v_a   = _mm_set1_pd(1.00001);
    __m128d v_b   = _mm_set1_pd(0.5);
    for (long i = 0; i < ITERATIONS; i += 2) {
        v_acc = _mm_add_pd(v_acc, _mm_mul_pd(v_a, v_b));
    }
    double buf[2];
    _mm_storeu_pd(buf, v_acc);
    g_simd_result = buf[0] + buf[1];
}
#else
/* Fallback scalar stubs for platforms without SIMD intrinsics */
static void test_simd_fadd(void *arg) { (void)arg; g_simd_result = 0.0; }
static void test_simd_fmul(void *arg) { (void)arg; g_simd_result = 1.0; }
static void test_simd_dot (void *arg) { (void)arg; g_simd_result = 0.0; }
static void test_simd_fma (void *arg) { (void)arg; g_simd_result = 0.0; }
#endif

typedef struct {
    const char *name;
    const char *type;  /* "float" or "double" */
    void (*func)(void*);
    int has_pmu;
} FloatTest;

static FloatTest tests[] = {
    {"float Add",  "float",  test_float_add,   0},
    {"float Mul",  "float",  test_float_mul,   0},
    {"float Div",  "float",  test_float_div,   0},
    {"float Sqrt", "float",  test_float_sqrt,  0},
    {"double Add", "double", test_double_add,  0},
    {"double Mul", "double", test_double_mul,  0},
    {"double Div", "double", test_double_div,  0},
    {"double Sqrt","double", test_double_sqrt, 0},
    {"double Sin", "double", test_double_sin,  0},
    {"double Log", "double", test_double_log,  0},
    {"double Pow", "double", test_double_pow,  0},
    {"Mixed",      "double", test_float_mixed, 0},
    {"SIMD FAdd",  "simd",   test_simd_fadd,   0},
    {"SIMD FMul",  "simd",   test_simd_fmul,   0},
    {"SIMD Dot",   "simd",   test_simd_dot,    0},
    {"SIMD FMA",   "simd",   test_simd_fma,    0},
};

void run_cpu_float_test(void) {
    print_header("CPU FLOATING POINT OPERATIONS TEST");

    int freq = get_cpu_freq_mhz();
    printf("Iterations: %.0f\n", (double)ITERATIONS);
    printf("CPU Frequency: %d MHz\n", freq);
    printf("Warmup: %.0f\n\n", (double)WARMUP_ITERATIONS);

    int num_tests = sizeof(tests) / sizeof(tests[0]);
    int pmu_available = (perf_init_counters() == 0);

    /* Print SIMD capabilities */
    const SIMDInfo *simd = get_simd_info();
    printf("SIMD Capabilities: %s\n", simd->simd_flags);
    printf("Max Vector Width: %d bits\n", simd->vector_width);
    printf("PMU Performance Counters: %s\n\n", pmu_available ? "Available" : "Not available");

    /* 预热 */
    printf("Warming up...\n");
    for (int i = 0; i < num_tests; i++) {
        tests[i].func(NULL);
    }

    /* 测试 */
    printf("\n=== Floating Point Operations ===\n\n");

    printf("%-12s | %-10s | %-12s | %-10s | %-8s | %-8s\n",
           "Operation", "Type", "Ops/sec", "ns/op", "CPI", "IPC");
    printf("%-12s | %-10s | %-12s | %-10s | %-8s | %-8s\n",
           "------------", "----------", "------------", "----------", "--------", "--------");

    PerfResult pr;

    for (int i = 0; i < num_tests; i++) {
        /* 再次预热 */
        tests[i].func(NULL);

        /* Try PMU measurement */
        int use_pmu = 0;
        memset(&pr, 0, sizeof(pr));
        if (pmu_available) {
            if (perf_measure(tests[i].func, NULL, &pr) == 0 && pr.instructions > 0) {
                use_pmu = 1;
                tests[i].has_pmu = 1;
            }
        }

        uint64_t start = get_time_ns();
        tests[i].func(NULL);
        uint64_t end = get_time_ns();

        uint64_t elapsed = end - start;
        /* SIMD tests process 2 doubles per loop iteration, so multiply by 2 */
        int ops_per_iter = (tests[i].type && strcmp(tests[i].type, "simd") == 0) ? 2 : 1;
        double ops_per_sec = (double)ITERATIONS * ops_per_iter / ((double)elapsed / 1000000000.0);
        double ns_per_op = (double)elapsed / ((double)ITERATIONS * ops_per_iter);

        double cpi, ipc;
        if (use_pmu) {
            cpi = pr.cpi;
            ipc = pr.ipc;
        } else {
            if (freq > 0) {
                cpi = ns_per_op * freq / 1000.0;
                ipc = 1.0 / cpi;
            } else {
                cpi = -1.0;  /* unknown */
                ipc = 0.0;
            }
        }

        printf("%-12s | %-10s | %-12.0f | %-10.2f | %-8.2f | %-8.2f\n",
               tests[i].name, tests[i].type, ops_per_sec, ns_per_op, cpi, ipc);
    }

    /* Generate report */
    ReportContext *report = report_init("cpu_float", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "Floating Point Operations Test Results");

        report_write(report, "SIMD Capabilities: %s\n", simd->simd_flags);
        report_write(report, "Max Vector Width: %d bits\n", simd->vector_width);
        report_write(report, "PMU Available: %s\n\n", pmu_available ? "Yes" : "No");

        report_write(report, "| Operation | Type | Time(ms) | Ops/sec | ns/op | CPI | IPC |\n");
        report_write(report, "|-----------|------|----------|---------|-------|-----|-----|\n");

        for (int i = 0; i < num_tests; i++) {
            PerfResult pr = {0};
            int use_pmu = 0;
            if (pmu_available) {
                if (perf_measure(tests[i].func, NULL, &pr) == 0 && pr.instructions > 0) {
                    use_pmu = 1;
                }
            }

            uint64_t start = get_time_ns();
            tests[i].func(NULL);
            uint64_t end = get_time_ns();

            uint64_t elapsed = end - start;
            double time_ms = (double)elapsed / 1000000.0;
            int ops_per_iter = (tests[i].type && strcmp(tests[i].type, "simd") == 0) ? 2 : 1;
            double ops_per_sec = (double)ITERATIONS * ops_per_iter / ((double)elapsed / 1000000000.0);
            double ns_per_op = (double)elapsed / ((double)ITERATIONS * ops_per_iter);

            double cpi = use_pmu ? pr.cpi : (freq > 0 ? (ns_per_op * freq / 1000.0) : -1.0);
            double ipc = use_pmu ? pr.ipc : (freq > 0 ? (1.0 / cpi) : 0.0);

            /* When freq is unknown, write "N/A" for CPI/IPC so sanity
             * parser sees non-numeric and skips. */
            if (cpi < 0) {
                report_write(report, "| %s | %s | %.2f | %.0f | %.2f | N/A | N/A |\n",
                            tests[i].name, tests[i].type, time_ms, ops_per_sec, ns_per_op);
            } else {
                report_write(report, "| %s | %s | %.2f | %.0f | %.2f | %.2f | %.2f |\n",
                            tests[i].name, tests[i].type, time_ms, ops_per_sec, ns_per_op, cpi, ipc);
            }
        }

        report_section(report, "Notes");
        report_write(report, "- Iterations: %.0f\n", (double)ITERATIONS);
        report_write(report, "- CPU Frequency: %d MHz\n", freq);
        report_write(report, "- CPI < 1 indicates superscalar execution\n");
        report_write(report, "- SIMD tests (128-bit NEON) measure 2 doubles per iteration\n\n");

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }
}

int main(int argc, char *argv[]) {
    init_platform_layer();
    initialize_cache_config();
    initialize_system_config();
    pmu_init_cache_counters();
    print_system_info();
    run_cpu_float_test();
    return 0;
}
