/**
 * CPU Branch Prediction Test
 *
 * 测试CPU分支预测性能:
 * - 稳定跳转 (taken/not taken)
 * - 随机跳转 (分支预测失效)
 *
 * 跨平台支持: X86_64, ARM64, RISC-V, PowerPC
 */

#include "common.h"
#include <stdlib.h>
#include <string.h>

#define ITERATIONS 100000000
#define WARMUP_ITERATIONS 1000000
#define ARRAY_SIZE 16384

/* ========== 分支预测测试 ========== */

/* 测试1: 总是taken的分枝 (高预测率) */
static uint64_t test_branch_always_taken(uint64_t iterations) {
    uint64_t result = 0;
    for (uint64_t i = 0; i < iterations; i++) {
        if (i < iterations) {  /* 总是true */
            result += i;
        }
    }
    return result;
}

/* 测试2: 从不taken的分枝 (高预测率) */
static uint64_t test_branch_never_taken(uint64_t iterations) {
    uint64_t result = 0;
    for (uint64_t i = 0; i < iterations; i++) {
        if (i > iterations) {  /* 永远false */
            result += i;
        }
    }
    return result;
}

/* 测试3: 50%概率taken (随机) - 使用全局数组 */
static uint64_t test_branch_50_percent(uint64_t iterations) {
    uint64_t result = 0;
    extern uint64_t random_array[16384];
    for (uint64_t i = 0; i < iterations; i++) {
        if (random_array[i & (ARRAY_SIZE - 1)]) {  /* 基于随机数组 */
            result += i;
        }
    }
    return result;
}

/* 测试4: 模式切换 - 每1024次切换一次 */
static uint64_t test_branch_pattern_switch(uint64_t iterations) {
    uint64_t result = 0;
    int toggle = 0;
    for (uint64_t i = 0; i < iterations; i++) {
        if ((i & 1023) == 0) toggle = !toggle;
        if (toggle) {
            result += i;
        }
    }
    return result;
}

/* 测试5: 2-bit饱和计数器的效果 */
static uint64_t test_branch_2bit_counter(uint64_t iterations) {
    uint64_t result = 0;
    int state = 0;  /* 0-3: strongly not taken 到 strongly taken */
    for (uint64_t i = 0; i < iterations; i++) {
        int taken = (state >= 2);
        if (taken) {
            result += i;
            state = (state < 3) ? state + 1 : state;
        } else {
            state = (state > 0) ? state - 1 : state;
        }
        /* 模拟50%概率切换方向 */
        if ((i & 1023) == 0) {
            state = (state <= 1) ? 3 : 0;  /* 强制切换 */
        }
    }
    return result;
}

/* 初始化随机数组 */
static void init_random_array(uint64_t *array, size_t size) {
    for (size_t i = 0; i < size; i++) {
        array[i] = (rand() & 1);  /* 随机0或1 */
    }
}

typedef struct {
    const char *name;
    uint64_t (*func)(uint64_t);
    uint64_t result;
} BranchTest;

uint64_t random_array[ARRAY_SIZE];  /* 全局，供test_branch_50_percent使用 */

static BranchTest tests[] = {
    {"Always Taken", test_branch_always_taken, 0},
    {"Never Taken", test_branch_never_taken, 0},
    {"50% Random", test_branch_50_percent, 0},
    {"Pattern 1K", test_branch_pattern_switch, 0},
    {"2-bit Counter", test_branch_2bit_counter, 0},
};

void run_cpu_branch_test(void) {
    print_header("CPU BRANCH PREDICTION TEST");

    printf("Iterations: %d\n", ITERATIONS);
    printf("Warmup: %d\n\n", WARMUP_ITERATIONS);

    /* 初始化随机数组 */
    srand(42);
    init_random_array(random_array, ARRAY_SIZE);

    int num_tests = sizeof(tests) / sizeof(tests[0]);

    /* 预热 */
    printf("Warming up...\n");
    for (int i = 0; i < num_tests; i++) {
        volatile uint64_t dummy = tests[i].func(WARMUP_ITERATIONS);
        (void)dummy;
    }

    /* 测试 */
    printf("\n=== Branch Prediction ===\n\n");

    printf("%-16s | %-12s | %-12s | %-10s\n",
           "Pattern", "Time(ms)", "Branches/sec", "ns/branch");
    printf("%-16s | %-12s | %-12s | %-10s\n",
           "---------------", "------------", "------------", "----------");

    for (int i = 0; i < num_tests; i++) {
        /* 再次预热 */
        volatile uint64_t dummy = tests[i].func(WARMUP_ITERATIONS / 10);
        (void)dummy;

        uint64_t start = get_time_ns();
        tests[i].result = tests[i].func(ITERATIONS);
        uint64_t end = get_time_ns();

        uint64_t elapsed = end - start;
        double time_ms = (double)elapsed / 1000000.0;
        double branches_per_sec = (double)ITERATIONS / ((double)elapsed / 1000000000.0);
        double ns_per_branch = (double)elapsed / ITERATIONS;

        printf("%-16s | %-12.2f | %-12.0f | %-10.2f\n",
               tests[i].name, time_ms, branches_per_sec, ns_per_branch);
    }

    printf("\nNote: Lower ns/branch = Better branch prediction\n");
    printf("      'Always Taken' and 'Never Taken' should be fastest\n");

    /* 生成报告 */
    ReportContext *report = report_init("cpu_branch", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "Branch Prediction Test Results");

        report_write(report, "| Pattern | Time(ms) | Branches/sec | ns/branch |\n");
        report_write(report, "|----------|----------|---------------|----------|\n");

        for (int i = 0; i < num_tests; i++) {
            uint64_t start = get_time_ns();
            tests[i].result = tests[i].func(ITERATIONS);
            uint64_t end = get_time_ns();

            uint64_t elapsed = end - start;
            double time_ms = (double)elapsed / 1000000.0;
            double branches_per_sec = (double)ITERATIONS / ((double)elapsed / 1000000000.0);
            double ns_per_branch = (double)elapsed / ITERATIONS;

            report_write(report, "| %s | %.2f | %.0f | %.2f |\n",
                       tests[i].name, time_ms, branches_per_sec, ns_per_branch);
        }

        report_section(report, "Notes");
        report_write(report, "- Iterations: %d\n", ITERATIONS);
        report_write(report, "- Lower ns/branch indicates better branch prediction\n");
        report_write(report, "- Always Taken / Never Taken patterns are easiest to predict\n\n");

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }
}

int main(int argc, char *argv[]) {
    initialize_cache_config();
    print_system_info();
    run_cpu_branch_test();
    return 0;
}
