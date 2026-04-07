/**
 * CPU Floating Point Operations Test
 *
 * 测试CPU浮点运算性能:
 * - 单精度浮点 (float)
 * - 双精度浮点 (double)
 *
 * 跨平台支持: X86_64, ARM64, RISC-V, PowerPC
 */

#include "common.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>

#define ITERATIONS 100000000
#define WARMUP_ITERATIONS 1000000

/* ========== 浮点运算测试 ========== */

/* 单精度加法测试 */
static double test_float_add(double iterations) {
    float result = 0.0f;
    for (double i = 0; i < iterations; i++) {
        result += (float)i * 0.5f;
    }
    return result;
}

/* 单精度乘法测试 */
static double test_float_mul(double iterations) {
    float result = 1.0f;
    for (double i = 1; i < iterations; i++) {
        result *= (float)i * 0.00001f;
        if (result < 0.0001f) result = 1.0f;  /* 防止下溢 */
    }
    return result;
}

/* 单精度除法测试 */
static double test_float_div(double iterations) {
    float result = 1.0f;
    volatile float dividend = 1000000.0f;
    for (double i = 1; i < iterations; i++) {
        result = dividend / (float)i;
    }
    return result;
}

/* 单精度平方根测试 */
static double test_float_sqrt(double iterations) {
    float result = 0.0f;
    for (double i = 1; i < iterations; i++) {
        result = sqrtf((float)i);
    }
    return result;
}

/* 双精度加法测试 */
static double test_double_add(double iterations) {
    double result = 0.0;
    for (double i = 0; i < iterations; i++) {
        result += i * 0.5;
    }
    return result;
}

/* 双精度乘法测试 */
static double test_double_mul(double iterations) {
    double result = 1.0;
    for (double i = 1; i < iterations; i++) {
        result *= i * 0.00001;
        if (result < 0.0001) result = 1.0;  /* 防止下溢 */
    }
    return result;
}

/* 双精度除法测试 */
static double test_double_div(double iterations) {
    double result = 1.0;
    volatile double dividend = 1000000.0;
    for (double i = 1; i < iterations; i++) {
        result = dividend / i;
    }
    return result;
}

/* 双精度平方根测试 */
static double test_double_sqrt(double iterations) {
    double result = 0.0;
    for (double i = 1; i < iterations; i++) {
        result = sqrt(i);
    }
    return result;
}

/* 双精度正弦测试 */
static double test_double_sin(double iterations) {
    double result = 0.0;
    for (double i = 1; i < iterations; i++) {
        result += sin(i * 0.00001);
    }
    return result;
}

/* 双精度对数测试 */
static double test_double_log(double iterations) {
    double result = 0.0;
    for (double i = 1; i < iterations; i++) {
        result += log(i);
    }
    return result;
}

typedef struct {
    const char *name;
    double (*func)(double);
    double result;
} FloatTest;

static FloatTest tests[] = {
    {"float Add", test_float_add, 0},
    {"float Mul", test_float_mul, 0},
    {"float Div", test_float_div, 0},
    {"float Sqrt", test_float_sqrt, 0},
    {"double Add", test_double_add, 0},
    {"double Mul", test_double_mul, 0},
    {"double Div", test_double_div, 0},
    {"double Sqrt", test_double_sqrt, 0},
    {"double Sin", test_double_sin, 0},
    {"double Log", test_double_log, 0},
};

void run_cpu_float_test(void) {
    print_header("CPU FLOATING POINT OPERATIONS TEST");

    printf("Iterations: %.0f\n", (double)ITERATIONS);
    printf("Warmup: %.0f\n\n", (double)WARMUP_ITERATIONS);

    int num_tests = sizeof(tests) / sizeof(tests[0]);

    /* 预热 */
    printf("Warming up...\n");
    for (int i = 0; i < num_tests; i++) {
        volatile double dummy = tests[i].func(WARMUP_ITERATIONS);
        (void)dummy;
    }

    /* 测试 */
    printf("\n=== Floating Point Operations ===\n\n");

    printf("%-12s | %-12s | %-12s | %-10s\n",
           "Operation", "Time(ms)", "Ops/sec", "ns/op");
    printf("%-12s | %-12s | %-12s | %-10s\n",
           "------------", "------------", "------------", "----------");

    for (int i = 0; i < num_tests; i++) {
        /* 再次预热 */
        volatile double dummy = tests[i].func(WARMUP_ITERATIONS / 10);

        uint64_t start = get_time_ns();
        tests[i].result = tests[i].func(ITERATIONS);
        uint64_t end = get_time_ns();

        uint64_t elapsed = end - start;
        double time_ms = (double)elapsed / 1000000.0;
        double ops_per_sec = (double)ITERATIONS / ((double)elapsed / 1000000000.0);
        double ns_per_op = (double)elapsed / ITERATIONS;

        printf("%-12s | %-12.2f | %-12.0f | %-10.2f\n",
               tests[i].name, time_ms, ops_per_sec, ns_per_op);

        (void)dummy;
    }

    /* 生成报告 */
    ReportContext *report = report_init("cpu_float", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "Floating Point Operations Test Results");

        report_write(report, "| Operation | Time(ms) | Ops/sec | ns/op |\n");
        report_write(report, "|-----------|----------|---------|-------|\n");

        for (int i = 0; i < num_tests; i++) {
            uint64_t start = get_time_ns();
            tests[i].result = tests[i].func(ITERATIONS);
            uint64_t end = get_time_ns();

            uint64_t elapsed = end - start;
            double time_ms = (double)elapsed / 1000000.0;
            double ops_per_sec = (double)ITERATIONS / ((double)elapsed / 1000000000.0);
            double ns_per_op = (double)elapsed / ITERATIONS;

            report_write(report, "| %s | %.2f | %.0f | %.2f |\n",
                       tests[i].name, time_ms, ops_per_sec, ns_per_op);
        }

        report_section(report, "Notes");
        report_write(report, "- Iterations: %.0f\n", (double)ITERATIONS);
        report_write(report, "- Architecture: %s\n\n", arch_names[current_arch]);

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }
}

int main(int argc, char *argv[]) {
    initialize_cache_config();
    print_system_info();
    run_cpu_float_test();
    return 0;
}
