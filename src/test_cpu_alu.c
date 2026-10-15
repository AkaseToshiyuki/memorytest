/**
 * CPU ALU Integer Operations Test
 *
 * 测试CPU整数运算性能:
 * - 加减乘除, 位运算
 * - Register dependency-chain diagnostics, not inferred CPI/IPC
 * - Counted work on the native compiler/ISA
 */

#include "common.h"
#include "util.h"
#define BENCH_DEFAULT_SAMPLES 50
#include "bench_protocol.h"
#include <stdlib.h>
#include <string.h>
#include <time.h>

static uint64_t alu_iterations;
#define ITERATIONS alu_iterations

/* ========== Thread-safe result storage ========== */
static volatile uint64_t g_result = 0;
/* The read/write register constraint prevents algebraic loop removal without
 * forcing an extra memory load/store on every iteration. This is a compiler
 * barrier, not a CPU fence, and does not claim peak instruction throughput. */
#define KEEP_REGISTER(value) __asm__ volatile("" : "+r"(value))

/* ========== 跨平台整数运算测试 ==========
 *
 * Anti-optimization strategy:
 *   - Keep the dependency in registers with compiler barriers and a final
 *     volatile sink. Per-iteration volatile accesses would measure memory too.
 *   - For test_div, use a non-zero, non-flipped divisor to avoid div-by-zero
 *     and to defeat the compiler's "constant divisor" optimization.
 *   - test_count uses a register barrier (a simple count++ loop is otherwise
 *     optimized away by -O2 — the "IPC=833K" bug we previously caught).
 *   - test_mod is intentionally % (mod is the op being measured, not noise).
 */

/* Add: pure add, anti-optimized via volatile sink + chained dependency
 * (each iteration depends on the previous sum, so the compiler can't
 *  vectorize or hoist). */
static void test_add(void *arg) {
    (void)arg;
    uint64_t sum = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        sum = sum + i;
        KEEP_REGISTER(sum);
    }
    g_result = sum;
}

/* Sub: pure subtract. */
static void test_sub(void *arg) {
    (void)arg;
    uint64_t val = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        val = val - i;
        KEEP_REGISTER(val);
    }
    g_result = val;
}

/* Mul: pure multiply. */
static void test_mul(void *arg) {
    (void)arg;
    uint64_t product = 1;
    uint64_t factor = 3;
    for (uint64_t i = 1; i < ITERATIONS; i++) {
        KEEP_REGISTER(factor);
        product = product * factor;
        KEEP_REGISTER(product);
    }
    g_result = product;
}

/* Div: divide by a non-constant, non-zero divisor each iteration to
 * prevent "constant divisor" optimization (which gcc/clang replace with
 * a magic multiply). Use OR with i to keep divisor varying. */
static void test_div(void *arg) {
    (void)arg;
    uint64_t divisor = 0xDEADBEEF;
    uint64_t quotient = 1;
    for (uint64_t i = 1; i < ITERATIONS; i++) {
        divisor = (divisor * 1103515245ULL + 12345ULL) | 1ULL;  /* LCG, always odd */
        KEEP_REGISTER(divisor);
        quotient = quotient / divisor + 1;
        KEEP_REGISTER(quotient);
    }
    g_result = quotient;
}

/* Bitwise AND: real AND. Result is bounded by the smallest set bit in any
 * i, so it eventually goes to 0 — that's the expected behavior, the
 * timing remains accurate even after the result stabilizes. */
static void test_and(void *arg) {
    (void)arg;
    uint64_t result = 0xFFFFFFFFFFFFFFFFULL;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        result = result & i;
        KEEP_REGISTER(result);
    }
    g_result = result;
}

/* Bitwise OR: real OR. */
static void test_or(void *arg) {
    (void)arg;
    uint64_t result = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        result = result | i;
        KEEP_REGISTER(result);
    }
    g_result = result;
}

/* Bitwise XOR: real XOR. */
static void test_xor(void *arg) {
    (void)arg;
    uint64_t result = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        result = result ^ i;
        KEEP_REGISTER(result);
    }
    g_result = result;
}

/* Left shift: shift by a varying amount. Reset to keep within bounds. */
static void test_shift_left(void *arg) {
    (void)arg;
    uint64_t result = 1;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        result = (result << 1) | 1;
        if (result > (UINT64_MAX >> 1)) result = 1;
        KEEP_REGISTER(result);
    }
    g_result = result;
}

/* Right shift: shift by varying amount. */
static void test_shift_right(void *arg) {
    (void)arg;
    uint64_t result = UINT64_MAX;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        result = result >> 1;
        if (result < 1024) result = UINT64_MAX;
        KEEP_REGISTER(result);
    }
    g_result = result;
}

/* Mod: real modulo. Mod is the op being measured (not anti-opt noise). */
static void test_mod(void *arg) {
    (void)arg;
    uint64_t divisor = 1000003;
    uint64_t result = 0;
    for (uint64_t i = 1; i < ITERATIONS; i++) {
        KEEP_REGISTER(divisor);
        result = result % divisor + i;
        KEEP_REGISTER(result);
    }
    g_result = result;
}

/* Increment loop: simple counter, retained in a register. */
static void test_count_loop(void *arg) {
    (void)arg;
    uint64_t count = 0;
    for (uint64_t i = 0; i < ITERATIONS; i++) {
        count = count + 1;
        KEEP_REGISTER(count);
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
    int starts_at_one;
} ALUTest;

static uint64_t test_array[1024];

static ALUTest tests[] = {
    {"Add",       test_add,       NULL,           0},
    {"Sub",       test_sub,       NULL,           0},
    {"Mul",       test_mul,       NULL,           1},
    {"Div",       test_div,       NULL,           1},
    {"AND",       test_and,       NULL,           0},
    {"OR",        test_or,        NULL,           0},
    {"XOR",       test_xor,       NULL,           0},
    {"SHL",       test_shift_left, NULL,         0},
    {"SHR",       test_shift_right, NULL,         0},
    {"Mod",       test_mod,       NULL,           1},
    {"Count",     test_count_loop, NULL,          0},
    {"Load+ALU",  test_load_mix,   test_array,    0},
};


static int alu_work(void *context, uint64_t blocks) {
    ALUTest *test = context;
    alu_iterations = blocks * 256 + test->starts_at_one;
    test->func(test->arg);
    return 0;
}

int run_cpu_alu_test(void) {
    for (unsigned i = 0; i < 1024; ++i) test_array[i] = i;
    enum { count = sizeof(tests) / sizeof(*tests) };
    BenchMeasurement results[count];
    for (unsigned i = 0; i < count; ++i) {
        printf("Warming and sampling: %s\n", tests[i].name); fflush(stdout);
        if (bench_measure(alu_work, &tests[i], &results[i])) return 1;
    }
    ReportContext *report = report_init("cpu_alu", REPORT_FORMAT_MARKDOWN);
    if (!report) return 1;
    report_write_system_info(report);
    report_section(report, "CPU ALU Performance Test Results");
    bench_report_conditions(report);
    report_write(report, "| Operation | Time(ms) | Ops/sec | ns/op | Operations | Spread(%%) | Data Source |\n");
    report_write(report, "|-----------|----------|---------|-------|------------|-----------|-------------|\n");
    for (unsigned i = 0; i < count; ++i) {
        double operations = (double)results[i].repetitions * 256;
        double ns = (double)results[i].elapsed_ns / operations;
        printf("%s: %.6f ns/op (%d samples)\n", tests[i].name, ns, BENCH_SAMPLES);
        report_write(report, "| %s | %.9f | %.0f | %.6f | %.0f | %.3f | Measured |\n",
                     tests[i].name, (double)results[i].elapsed_ns / 1e6, 1e9 / ns, ns,
                     operations, bench_spread(&results[i]));
    }
    report_section(report, "Notes");
    report_write(report, "Counts describe register dependency-chain iterations, including loop and auxiliary work.\n"
                 "Compiler barriers retain the work without per-iteration volatile spills; Load+ALU intentionally loads memory.\n"
                 "Not peak instruction throughput. No CPI/IPC is derived from nominal frequency.\n");
    FILE *audit = bench_audit_begin(report);
    if (!audit) { report_free(report); return 1; }
    for (unsigned i = 0; i < count; ++i) bench_audit_row(report, audit, tests[i].name, &results[i], i);
    if (bench_audit_finish(audit)) { report_free(report); return 1; }
    report_finalize_json(report);
    return report_finish(report) ? 1 : 0;
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    init_platform_layer();
    print_system_info();
    return run_cpu_alu_test();
}
