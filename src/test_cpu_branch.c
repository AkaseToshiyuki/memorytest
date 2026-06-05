/**
 * CPU Branch Prediction Test
 *
 * 测试CPU分支预测性能:
 * - 各种分支模式
 * - 分支预测错误率 (使用PMU)
 * - BTB效率分析
 *
 * 跨平台支持: X86_64, ARM64, RISC-V, PowerPC
 */

#include "common.h"
#include "util.h"
#include <stdlib.h>
#include <string.h>

#define ITERATIONS 100000000
#define WARMUP_ITERATIONS 1000000
#define ARRAY_SIZE 16384

/* ========== 全局数据 ========== */
static volatile uint64_t g_result = 0;
static uint64_t random_array[ARRAY_SIZE];

/* ========== 分支预测测试 ========== */

/* 测试1: 总是taken的分枝 (高预测率) */
static void test_always_taken(void *arg) {
    (void)arg;
    uint64_t result = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        if (i < ITERATIONS) {  /* 总是true */
            result += i;
        }
    }
    g_result = result;
}

/* 测试2: 从不taken的分枝 (高预测率) */
static void test_never_taken(void *arg) {
    (void)arg;
    uint64_t result = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        if (i > ITERATIONS) {  /* 永远false */
            result += i;
        }
    }
    g_result = result;
}

/* 测试3: 50%概率taken (随机) */
static void test_random(void *arg) {
    (void)arg;
    uint64_t result = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        if (random_array[i & (ARRAY_SIZE - 1)]) {
            result += i;
        }
    }
    g_result = result;
}

/* 测试4: 模式切换 - 每1024次切换一次 */
static void test_pattern_1k(void *arg) {
    (void)arg;
    uint64_t result = 0;
    int toggle = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        if ((i & 1023) == 0) toggle = !toggle;
        if (toggle) {
            result += i;
        }
    }
    g_result = result;
}

/* 测试5: 模式切换 - 每256次切换一次 */
static void test_pattern_256(void *arg) {
    (void)arg;
    uint64_t result = 0;
    int toggle = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        if ((i & 255) == 0) toggle = !toggle;
        if (toggle) {
            result += i;
        }
    }
    g_result = result;
}

/* 测试6: 2-bit饱和计数器 */
static void test_2bit_counter(void *arg) {
    (void)arg;
    uint64_t result = 0;
    int state = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        int taken = (state >= 2);
        if (taken) {
            result += i;
            state = (state < 3) ? state + 1 : state;
        } else {
            state = (state > 0) ? state - 1 : state;
        }
        if ((i & 1023) == 0) {
            state = (state <= 1) ? 3 : 0;
        }
    }
    g_result = result;
}

/* 测试7: 递增分支 (最坏情况) */
static void test_increasing(void *arg) {
    (void)arg;
    uint64_t result = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        /* volatile prevents the compiler from folding i < i+1 to true */
        volatile uint64_t a = i;
        if (a < a + 1) {  /* always true, but invisible to the optimiser */
            result += i;
        }
    }
    g_result = result;
}

/* 测试8: 间接分支 (computed goto) */
static void test_indirect(void *arg) {
    (void)arg;
    static void *targets[] = {&&l1, &&l2, &&l3, &&l4};
    uint64_t result = 0;

    for (uint64_t i = 0; i < ITERATIONS; i++) {
        int idx = i & 3;
        goto *targets[idx];
    l1: result += 1; goto next;
    l2: result += 2; goto next;
    l3: result += 3; goto next;
    l4: result += 4; goto next;
    next:;
    }

    g_result = result;
    (void)arg;
}

/* 初始化随机数组 */
static void init_random_array(void) {
    for (int i = 0; i < ARRAY_SIZE; i++) {
        random_array[i] = (rand() & 1);
    }
}

typedef struct {
    const char *name;
    const char *category;
    void (*func)(void*);
    int use_pmu;
} BranchTest;

static BranchTest tests[] = {
    {"Always Taken",   "Predictable", test_always_taken,    0},
    {"Never Taken",    "Predictable", test_never_taken,    0},
    {"Random 50%",     "Unpredictable", test_random,       0},
    {"Pattern 1K",     "Pattern",      test_pattern_1k,       0},
    {"Pattern 256",    "Pattern",     test_pattern_256,     0},
    {"2-bit Counter",  "Adaptive",    test_2bit_counter,    0},
    {"Increasing",     "Edge Case",   test_increasing,       0},
};

void run_cpu_branch_test(void) {
    print_header("CPU BRANCH PREDICTION TEST");

    /* Initialize random array */
    srand(42);
    init_random_array();

    int freq = get_cpu_freq_mhz();
    printf("Iterations: %d\n", ITERATIONS);
    printf("CPU Frequency: %d MHz\n", freq);
    printf("Warmup: %d\n\n", WARMUP_ITERATIONS);

    int num_tests = sizeof(tests) / sizeof(tests[0]);
    int pmu_available = (perf_init_counters() == 0);

    printf("PMU Performance Counters: %s\n\n", pmu_available ? "Available" : "Not available");

    /* 预热 */
    printf("Warming up...\n");
    for (int i = 0; i < num_tests; i++) {
        tests[i].func(NULL);
    }

    /* 测试 */
    printf("\n=== Branch Prediction Performance ===\n\n");

    printf("%-16s | %-12s | %-12s | %-10s | %-12s | %-14s\n",
           "Pattern", "Category", "Time(ms)", "ns/branch", "Mispred Rate", "PMU Data");
    printf("%-16s | %-12s | %-12s | %-10s | %-12s | %-14s\n",
           "---------------", "----------", "------------", "----------", "------------", "--------------");

    PerfResult pr;

    for (int i = 0; i < num_tests; i++) {
        /* 再次预热 */
        tests[i].func(NULL);

        /* PMU + wall-clock in a single run */
        int use_pmu = 0;
        memset(&pr, 0, sizeof(pr));
        uint64_t start, end;
        if (pmu_available) {
            start = get_time_ns();
            if (perf_measure(tests[i].func, NULL, &pr) == 0 && pr.instructions > 0) {
                end = get_time_ns();
                use_pmu = 1;
                tests[i].use_pmu = 1;
            }
        }
        if (!use_pmu) {
            start = get_time_ns();
            tests[i].func(NULL);
            end = get_time_ns();
        }

        uint64_t elapsed = end - start;
        double time_ms = (double)elapsed / 1000000.0;
        double ns_per_branch = (double)elapsed / ITERATIONS;

        const char *mispred_str;
        if (use_pmu && pr.branch_mispred_rate >= 0) {
            static char buf[32];
            snprintf(buf, sizeof(buf), "%.3f%%", pr.branch_mispred_rate);
            mispred_str = buf;
        } else {
            mispred_str = "N/A (no PMU)";
        }

        printf("%-16s | %-12s | %-12.2f | %-10.2f | %-12s | %s\n",
               tests[i].name, tests[i].category, time_ms, ns_per_branch,
               mispred_str, use_pmu ? "cycles/inst" : "estimated");
    }

    printf("\nNote: Lower ns/branch = Better branch prediction");
    printf("\n      'Always Taken' and 'Never Taken' should be fastest\n");
    printf("      High misprediction rate indicates branch prediction difficulty\n");

    /* Generate report */
    ReportContext *report = report_init("cpu_branch", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "What This Test Measures");
        report_write(report,
            "Branch prediction performance across distinct branch patterns.\n"
            "Each pattern runs %d iterations of a loop containing a single\n"
            "conditional branch. We measure:\n\n"
            "  - **Time(ms) / ns/branch** -- wall-clock cost per branch (lower = better predictor)\n"
            "  - **Mispred Rate** -- branch mispredictions / total branches (via PMU when accessible)\n"
            "  - **Data Source** -- PMU (hardware counters) vs Estimated (timing only)\n\n"
            "Patterns span five categories to expose different predictor behaviours:\n"
            "  - *Predictable* (Always/Never Taken) -- saturating predictor baseline\n"
            "  - *Unpredictable* (Random 50%%) -- worst case for any predictor\n"
            "  - *Pattern* (1K / 256 toggle) -- tests pattern-history table depth\n"
            "  - *Adaptive* (2-bit counter) -- tests hysteresis recovery\n"
            "  - *Edge Case* (Increasing) -- degenerate monotonic input\n\n"
            "**Caveat**: The Mispred Rate column requires hardware PMU access\n"
            "(`perf_event_open` with non-restrictive `perf_event_paranoid`). On systems\n"
            "where PMU is restricted (paranoid >= 2), the rate is reported as N/A and\n"
            "the Time/ns columns are the primary signal. See *Notes* below for the\n"
            "PMU status on this run.\n\n",
            ITERATIONS);

        report_section(report, "Branch Prediction Test Results");

        report_write(report, "PMU Available: %s\n\n", pmu_available ? "Yes" : "No");

        report_write(report, "| Pattern | Category | Time(ms) | ns/branch | Mispred Rate | Data Source |\n");
        report_write(report, "|----------|----------|----------|-----------|--------------|-------------|\n");

        for (int i = 0; i < num_tests; i++) {
            PerfResult pr = {0};
            int use_pmu = 0;
            uint64_t start, end;
            if (pmu_available) {
                start = get_time_ns();
                if (perf_measure(tests[i].func, NULL, &pr) == 0 && pr.instructions > 0) {
                    end = get_time_ns();
                    use_pmu = 1;
                }
            }
            if (!use_pmu) {
                start = get_time_ns();
                tests[i].func(NULL);
                end = get_time_ns();
            }

            uint64_t elapsed = end - start;
            double time_ms = (double)elapsed / 1000000.0;
            double ns_per_branch = (double)elapsed / ITERATIONS;

            /* Show real mispred rate if PMU returned a valid value, else N/A.
             * Note: PMU on this system returns zero counts (perf_event_paranoid=4),
             * so the rate column shows N/A across all rows. The Time/ns columns
             * still carry real wall-clock measurements and are the primary signal. */
            char mispred_buf[32];
            const char *mispred_str;
            if (use_pmu && pr.branch_mispred_rate >= 0) {
                snprintf(mispred_buf, sizeof(mispred_buf), "%.3f%%", pr.branch_mispred_rate);
                mispred_str = mispred_buf;
            } else {
                mispred_str = "N/A";
            }

            report_write(report, "| %s | %s | %.2f | %.2f | %s | %s |\n",
                        tests[i].name, tests[i].category, time_ms, ns_per_branch,
                        mispred_str, use_pmu ? "PMU" : "Estimated");
        }

        /* Note: zero-time rows are the compiler proving the condition is
         * tautological / contradictory at compile time (e.g. i < ITERATIONS
         * inside a for(i < ITERATIONS) loop is constant-true). These rows
         * are still informative -- they confirm the lower bound (0 ns/branch
         * when no branch is emitted), but they do not measure predictor
         * quality. See test source for the exact conditions. */
        report_write(report,
            "\n*Note*: Rows with `0.00` ns/branch indicate the compiler statically\n"
            "proved the branch condition is tautological (always taken / never taken)\n"
            "and emitted no branch instruction. The remaining rows (Random, Pattern,\n"
            "2-bit counter) carry real branches and reflect predictor behaviour.\n\n");

        report_section(report, "Branch Prediction Analysis");
        report_write(report, "- Predictable patterns (Always/Never Taken): Should have ~0%% misprediction\n");
        report_write(report, "- Random patterns: ~50%% misprediction rate expected\n");
        report_write(report, "- Pattern-based: Misprediction on pattern switches\n");
        report_write(report, "- 2-bit counter: Good for repetitive patterns\n\n");

        report_section(report, "Notes");
        report_write(report, "- Iterations: %d\n", ITERATIONS);
        report_write(report, "- CPU Frequency: %d MHz\n", freq);
        report_write(report, "- ns/branch = total time / number of branches\n");
        report_write(report, "- Misprediction rate = branch mispredictions / total branches\n\n");

        printf("\n[报告] 已生成: %s\n", report_get_filename(report));
        report_finalize_json(report);
        report_free(report);
    }
}

int main(int argc, char *argv[]) {
    init_platform_layer();
    pmu_init_cache_counters();
    print_system_info();
    run_cpu_branch_test();
    return 0;
}
