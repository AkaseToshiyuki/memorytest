/**
 * CPU ALU Integer Operations Test
 *
 * 测试CPU整数运算性能:
 * - 加减乘除, 位运算
 * - CPI/IPC测量 (使用PMU)
 * - 跨平台支持: X86_64, ARM64, RISC-V, PowerPC
 */

#include "common.h"
#include "util.h"
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define ITERATIONS 100000000
#define WARMUP_ITERATIONS 1000000

/* ========== Thread-safe result storage ========== */
static volatile uint64_t g_result = 0;
static volatile uint64_t g_counter = 0;

/* ========== 跨平台整数运算测试 ==========
 *
 * Anti-optimization strategy:
 *   - Use a volatile sink (compiler_barrier + final write) to prevent the
 *     compiler from hoisting or removing the loop. This is the standard idiom
 *     and produces REAL IPC numbers (no mod-bound slowdown from synthetic
 *     work).
 *   - For test_div, use a non-zero, non-flipped divisor to avoid div-by-zero
 *     and to defeat the compiler's "constant divisor" optimization.
 *   - test_count uses volatile sink (a simple count++ loop is otherwise
 *     optimized away by -O2 — the "IPC=833K" bug we previously caught).
 *   - test_mod is intentionally % (mod is the op being measured, not noise).
 */

/* Add: pure add, anti-optimized via volatile sink + chained dependency
 * (each iteration depends on the previous sum, so the compiler can't
 *  vectorize or hoist). */
static void test_add(void *arg) {
    (void)arg;
    volatile uint64_t sum = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        sum = sum + i;
    }
    g_result = sum;
}

/* Sub: pure subtract. */
static void test_sub(void *arg) {
    (void)arg;
    volatile uint64_t val = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        val = val - i;
    }
    g_result = val;
}

/* Mul: pure multiply. */
static void test_mul(void *arg) {
    (void)arg;
    volatile uint64_t product = 1;
    for (uint64_t i = 1; i < ITERATIONS; i++) {
        product = product * 3;
    }
    g_result = product;
}

/* Div: divide by a non-constant, non-zero divisor each iteration to
 * prevent "constant divisor" optimization (which gcc/clang replace with
 * a magic multiply). Use OR with i to keep divisor varying. */
static void test_div(void *arg) {
    (void)arg;
    volatile uint64_t divisor = 0xDEADBEEF;
    volatile uint64_t quotient = 1;
    for (uint64_t i = 1; i < ITERATIONS; i++) {
        divisor = (divisor * 1103515245ULL + 12345ULL) | 1ULL;  /* LCG, always odd */
        quotient = quotient / divisor + 1;
    }
    g_result = quotient;
    (void)g_counter;
}

/* Bitwise AND: real AND. Result is bounded by the smallest set bit in any
 * i, so it eventually goes to 0 — that's the expected behavior, the
 * timing remains accurate even after the result stabilizes. */
static void test_and(void *arg) {
    (void)arg;
    volatile uint64_t result = 0xFFFFFFFFFFFFFFFFULL;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        result = result & i;
    }
    g_result = result;
}

/* Bitwise OR: real OR. */
static void test_or(void *arg) {
    (void)arg;
    volatile uint64_t result = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        result = result | i;
    }
    g_result = result;
}

/* Bitwise XOR: real XOR. */
static void test_xor(void *arg) {
    (void)arg;
    volatile uint64_t result = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        result = result ^ i;
    }
    g_result = result;
}

/* Left shift: shift by a varying amount. Reset to keep within bounds. */
static void test_shift_left(void *arg) {
    (void)arg;
    volatile uint64_t result = 1;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        result = (result << 1) | 1;
        if (result > (UINT64_MAX >> 1)) result = 1;
    }
    g_result = result;
}

/* Right shift: shift by varying amount. */
static void test_shift_right(void *arg) {
    (void)arg;
    volatile uint64_t result = UINT64_MAX;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        result = result >> 1;
        if (result < 1024) result = UINT64_MAX;
    }
    g_result = result;
}

/* Mod: real modulo. Mod is the op being measured (not anti-opt noise). */
static void test_mod(void *arg) {
    (void)arg;
    volatile uint64_t divisor = 1000003;
    volatile uint64_t result = 0;
    for (uint64_t i = 1; i < ITERATIONS; i++) {
        result = result % divisor + i;
    }
    g_result = result;
}

/* Increment loop: simple counter. Volatile sink prevents optimization. */
static void test_count_loop(void *arg) {
    (void)arg;
    volatile uint64_t count = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        count = count + 1;
    }
    g_result = count;
}

/* Load + ALU: read from a small array (L1-resident) and accumulate. */
static void test_load_mix(void *arg) {
    (void)arg;
    volatile uint64_t *arr = (volatile uint64_t *)arg;
    uint64_t sum = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        sum += arr[i & 1023];
    }
    g_result = sum;
}

typedef struct {
    const char *name;
    void (*func)(void*);
    void *arg;
    int has_pmu;
} ALUTest;

static uint64_t test_array[1024];

static ALUTest tests[] = {
    {"Add",       test_add,       NULL,           0},
    {"Sub",       test_sub,       NULL,           0},
    {"Mul",       test_mul,       NULL,           0},
    {"Div",       test_div,       NULL,           0},
    {"AND",       test_and,       NULL,           0},
    {"OR",        test_or,        NULL,           0},
    {"XOR",       test_xor,       NULL,           0},
    {"SHL",       test_shift_left, NULL,         0},
    {"SHR",       test_shift_right, NULL,         0},
    {"Mod",       test_mod,       NULL,           0},
    {"Count",     test_count_loop, NULL,          0},
    {"Load+ALU",  test_load_mix,   test_array,    0},
};

void run_cpu_alu_test(void) {
    print_header("CPU ALU INTEGER OPERATIONS TEST");

    /* Initialize test array */
    for (int i = 0; i < 1024; i++) test_array[i] = i;

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
        if (tests[i].arg == NULL) {
            tests[i].func(NULL);
        } else {
            tests[i].func(test_array);
        }
    }

    /* 测试 */
    printf("\n=== Integer Operations ===\n\n");

    printf("%-10s | %-10s | %-12s | %-10s | %-8s | %-8s | %-10s\n",
           "Operation", "Time(ms)", "Ops/sec", "ns/op", "CPI", "IPC", "PMU");
    printf("%-10s | %-10s | %-12s | %-10s | %-8s | %-8s | %-10s\n",
           "----------", "----------", "------------", "----------", "--------", "--------", "----------");

    PerfResult perf_result = {0};

    for (int i = 0; i < num_tests; i++) {
        /* Setup args */
        void *arg = tests[i].arg;
        if (tests[i].arg == test_array) {
            /* Reset array to avoid caching effects */
            for (int j = 0; j < 1024; j++) test_array[j] = j;
            arg = (void*)test_array;
        }

        /* 再次预热 */
        tests[i].func(arg);

        /* Try PMU measurement first */
        PerfResult pr = {0};
        int use_pmu = 0;

        if (pmu_available) {
            if (perf_measure(tests[i].func, arg, &pr) == 0 && pr.instructions > 0) {
                use_pmu = 1;
                tests[i].has_pmu = 1;
            }
        }

        /* Timing measurement */
        uint64_t start = get_time_ns();
        tests[i].func(arg);
        uint64_t end = get_time_ns();

        uint64_t elapsed = end - start;
        double time_ms = (double)elapsed / 1000000.0;
        double ops_per_sec = (double)ITERATIONS / ((double)elapsed / 1000000000.0);
        double ns_per_op = (double)elapsed / ITERATIONS;

        /* Use PMU data if available, otherwise estimate */
        double cpi, ipc;
        if (use_pmu) {
            cpi = pr.cpi;
            ipc = pr.ipc;
        } else {
            /* Fallback: estimate CPI assuming 1 instruction per op.
             * If freq is unknown (0), we cannot derive a meaningful CPI/IPC
             * from wall-clock ns; mark it as N/A (-1) and 0 respectively. */
            if (freq > 0) {
                cpi = ns_per_op * freq / 1000.0;  /* cycles per ns * ns per op */
                ipc = 1.0 / cpi;
            } else {
                cpi = -1.0;  /* unknown */
                ipc = 0.0;
            }
        }

        printf("%-10s | %-10.2f | %-12.0f | %-10.2f | %-8.2f | %-8.2f | %-10s\n",
               tests[i].name, time_ms, ops_per_sec, ns_per_op,
               cpi, ipc, use_pmu ? "cycles/inst" : "estimated");

        (void)perf_result;  /* unused */
    }

    /* Print SIMD info */
    const SIMDInfo *simd = get_simd_info();
    printf("\n=== SIMD Capabilities ===\n\n");
    printf("Supported: %s\n", simd->simd_flags);
    printf("Max Vector Width: %d bits\n", simd->vector_width);

    /* Generate report */
    ReportContext *report = report_init("cpu_alu", REPORT_FORMAT_MARKDOWN);
    if (report) {
        report_write_system_info(report);
        report_section(report, "CPU ALU Performance Test Results");
        report_write(report, "PMU Available: %s\n\n", pmu_available ? "Yes" : "No");

        report_write(report, "| Operation | Time(ms) | Ops/sec | ns/op | CPI | IPC | Data Source |\n");
        report_write(report, "|-----------|----------|---------|-------|-----|-----|-------------|\n");

        for (int i = 0; i < num_tests; i++) {
            void *arg = tests[i].arg;
            if (tests[i].arg == test_array) {
                for (int j = 0; j < 1024; j++) test_array[j] = j;
                arg = (void*)test_array;
            }

            PerfResult pr = {0};
            int use_pmu = 0;
            if (pmu_available) {
                if (perf_measure(tests[i].func, arg, &pr) == 0 && pr.instructions > 0) {
                    use_pmu = 1;
                }
            }

            uint64_t start = get_time_ns();
            tests[i].func(arg);
            uint64_t end = get_time_ns();

            uint64_t elapsed = end - start;
            double time_ms = (double)elapsed / 1000000.0;
            double ops_per_sec = (double)ITERATIONS / ((double)elapsed / 1000000000.0);
            double ns_per_op = (double)elapsed / ITERATIONS;

            double cpi = use_pmu ? pr.cpi : (freq > 0 ? (ns_per_op * freq / 1000.0) : -1.0);
            double ipc = use_pmu ? pr.ipc : (freq > 0 ? (1.0 / cpi) : 0.0);

            /* When freq is unknown, write "N/A" for CPI/IPC so sanity
             * parser sees non-numeric and skips (out[v] = None). */
            if (cpi < 0) {
                report_write(report, "| %s | %.2f | %.0f | %.2f | N/A | N/A | %s |\n",
                            tests[i].name, time_ms, ops_per_sec, ns_per_op,
                            use_pmu ? "PMU" : "Estimated");
            } else {
                report_write(report, "| %s | %.2f | %.0f | %.2f | %.2f | %.2f | %s |\n",
                            tests[i].name, time_ms, ops_per_sec, ns_per_op,
                            cpi, ipc, use_pmu ? "PMU" : "Estimated");
            }
        }

        report_section(report, "SIMD Capabilities");
        report_write(report, "- Supported: %s\n", simd->simd_flags);
        report_write(report, "- Max Vector Width: %d bits\n\n", simd->vector_width);

        report_section(report, "Notes");
        report_write(report, "- Iterations: %d\n", ITERATIONS);
        report_write(report, "- CPU Frequency: %d MHz\n", freq);
        report_write(report, "- CPI = Cycles Per Instruction\n");
        report_write(report, "- IPC = Instructions Per Cycle\n");
        report_write(report, "- CPI < 1 indicates superscalar execution (multiple ops/cycle)\n\n");

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
    run_cpu_alu_test();
    return 0;
}
