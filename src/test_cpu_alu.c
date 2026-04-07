/**
 * CPU ALU Integer Operations Test
 *
 * 测试CPU整数运算性能:
 * - 加减乘除
 * - 位运算
 *
 * 跨平台支持: X86_64, ARM64, RISC-V, PowerPC
 */

#include "common.h"
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define ITERATIONS 100000000
#define WARMUP_ITERATIONS 1000000

/* ========== 跨平台整数运算测试 ========== */

/* 加法测试 */
static uint64_t test_add(uint64_t iterations) {
    uint64_t sum = 0;
    for (uint64_t i = 0; i < iterations; i++) {
        sum += i;
    }
    return sum;
}

/* 减法测试 */
static uint64_t test_sub(uint64_t iterations) {
    uint64_t val = UINT64_MAX;
    for (uint64_t i = 0; i < iterations; i++) {
        val -= i;
    }
    return val;
}

/* 乘法测试 */
static uint64_t test_mul(uint64_t iterations) {
    uint64_t product = 1;
    for (uint64_t i = 1; i < iterations; i++) {
        product = (product * 3 + 7) % 1000000007;  /* 防止常量传播 */
    }
    return product;
}

/* 除法测试 - 使用慢速除数避免编译器优化 */
static uint64_t test_div(uint64_t iterations) {
    uint64_t quotient = UINT64_MAX / 1000;
    volatile uint64_t divisor = 1000000007;  /* 使用volatile防止优化 */
    for (uint64_t i = 0; i < iterations; i++) {
        quotient = (quotient / divisor) + 1;
        divisor = 1000000007 - divisor;  /* 变化除数 */
    }
    return quotient;
}

/* 位与测试 */
static uint64_t test_and(uint64_t iterations) {
    uint64_t result = 0xFFFFFFFF;
    for (uint64_t i = 0; i < iterations; i++) {
        result &= i;
    }
    return result;
}

/* 位或测试 */
static uint64_t test_or(uint64_t iterations) {
    uint64_t result = 0;
    for (uint64_t i = 0; i < iterations; i++) {
        result |= i;
    }
    return result;
}

/* 位异或测试 */
static uint64_t test_xor(uint64_t iterations) {
    uint64_t result = 0;
    for (uint64_t i = 0; i < iterations; i++) {
        result ^= i;
    }
    return result;
}

/* 左移测试 */
static uint64_t test_shift_left(uint64_t iterations) {
    volatile uint64_t result = 1;
    for (uint64_t i = 0; i < iterations; i++) {
        result = (result << 1) | 1;
        if (result > UINT64_MAX >> 1) result = 1;  /* 防止溢出 */
    }
    return result;
}

/* 右移测试 */
static uint64_t test_shift_right(uint64_t iterations) {
    volatile uint64_t result = UINT64_MAX;
    for (uint64_t i = 0; i < iterations; i++) {
        result >>= 1;
        if (result < 1024) result = UINT64_MAX;  /* 重置 */
    }
    return result;
}

typedef struct {
    const char *name;
    uint64_t (*func)(uint64_t);
    uint64_t result;  /* 用于防止优化 */
} ALUTest;

static ALUTest tests[] = {
    {"Add", test_add, 0},
    {"Sub", test_sub, 0},
    {"Mul", test_mul, 0},
    {"Div", test_div, 0},
    {"AND", test_and, 0},
    {"OR", test_or, 0},
    {"XOR", test_xor, 0},
    {"SHL", test_shift_left, 0},
    {"SHR", test_shift_right, 0},
};

void run_cpu_alu_test(void) {
    print_header("CPU ALU INTEGER OPERATIONS TEST");

    printf("Iterations: %d\n", ITERATIONS);
    printf("Warmup: %d\n\n", WARMUP_ITERATIONS);

    int num_tests = sizeof(tests) / sizeof(tests[0]);

    /* 预热 */
    printf("Warming up...\n");
    for (int i = 0; i < num_tests; i++) {
        tests[i].result = tests[i].func(WARMUP_ITERATIONS);
    }

    /* 测试 */
    printf("\n=== Integer Operations ===\n\n");

    printf("%-8s | %-12s | %-12s | %-10s\n",
           "Operation", "Time(ms)", "Ops/sec", "ns/op");
    printf("%-8s | %-12s | %-12s | %-10s\n",
           "--------", "------------", "------------", "----------");

    double total_time = 0;

    for (int i = 0; i < num_tests; i++) {
        /* 再次预热确保缓存 */
        volatile uint64_t dummy = tests[i].func(WARMUP_ITERATIONS / 10);

        uint64_t start = get_time_ns();
        tests[i].result = tests[i].func(ITERATIONS);
        uint64_t end = get_time_ns();

        uint64_t elapsed = end - start;
        double time_ms = (double)elapsed / 1000000.0;
        double ops_per_sec = (double)ITERATIONS / ((double)elapsed / 1000000000.0);
        double ns_per_op = (double)elapsed / ITERATIONS;

        printf("%-8s | %-12.2f | %-12.0f | %-10.2f\n",
               tests[i].name, time_ms, ops_per_sec, ns_per_op);

        total_time += time_ms;

        (void)dummy;  /* 防止优化 */
    }

    printf("\nTotal time: %.2f ms\n", total_time);

    /* 生成报告 */
    ReportContext *report = report_init("cpu_alu", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "ALU Integer Operations Test Results");

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
        report_write(report, "- Iterations: %d\n", ITERATIONS);
        report_write(report, "- Architecture: %s\n\n", arch_names[current_arch]);

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }
}

int main(int argc, char *argv[]) {
    initialize_cache_config();
    print_system_info();
    run_cpu_alu_test();
    return 0;
}
