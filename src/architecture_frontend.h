#ifndef ARCHITECTURE_FRONTEND_H
#define ARCHITECTURE_FRONTEND_H
#include "architecture_counters.h"
/* Static text: works with hardened runtimes and needs no writable executable
 * memory. Each branch is taken; skipped padding controls target spacing. */
#define FE_STR_(x) #x
#define FE_STR(x) FE_STR_(x)
#if defined(__x86_64__)
#define FE_BRANCH(stride) ".byte 0xeb," FE_STR(stride) "-2\n.fill " FE_STR(stride) "-2,1,0x90\n"
#define FE_NOP_1 ".byte 0x90\n"
#define FE_NOP_4 ".byte 0x0f,0x1f,0x40,0x00\n"
#define FE_NOP_8 ".byte 0x0f,0x1f,0x84,0x00,0,0,0,0\n"
#else
#define FE_BRANCH(stride) "b 1f\n.if " FE_STR(stride) ">4\n.space " FE_STR(stride) "-4\n.endif\n1:\n"
#define FE_NOP_4 "nop\n"
#endif
#define FE_COUNTS(M,s) M(128,s) M(256,s) M(512,s) M(768,s) M(1024,s) M(1536,s) M(2048,s) M(3072,s) M(4096,s) M(6144,s) M(8192,s) M(12288,s) M(16384,s) M(24576,s) M(32768,s)
#define FE_BYTES(M,s) M(128,s) M(256,s) M(512,s) M(1024,s) M(2048,s) M(4096,s) M(8192,s) M(16384,s) M(32768,s) M(65536,s) M(131072,s) M(262144,s)
#define FE_BRANCH_FN(n,s) static __attribute__((noinline, aligned(64))) void fe_btb_##n##_##s(void) { \
    __asm__ volatile(".rept " FE_STR(n) "\n" FE_BRANCH(s) ".endr\n" ::: "memory"); }
#define FE_NOP_FN(n,s) static __attribute__((noinline, aligned(64))) void fe_nop_##n##_##s(void) { \
    __asm__ volatile(".rept " FE_STR(n) "/" FE_STR(s) "\n" FE_NOP_##s ".endr\n" ::: "memory"); }
FE_COUNTS(FE_BRANCH_FN,8) FE_COUNTS(FE_BRANCH_FN,16) FE_COUNTS(FE_BRANCH_FN,32)
FE_BYTES(FE_NOP_FN,4)
#if defined(__x86_64__)
FE_BYTES(FE_NOP_FN,1) FE_BYTES(FE_NOP_FN,8)
#endif
typedef struct { void (*run)(void); const char *kind; unsigned count, stride, bytes; } FrontDefinition;
#define FE_BRANCH_ROW(n,s) {fe_btb_##n##_##s, "btb", n, s, n*s},
#define FE_NOP_ROW(n,s) {fe_nop_##n##_##s, "nop", n/s, s, n},
static const FrontDefinition frontend_definitions[] = {
    FE_COUNTS(FE_BRANCH_ROW,8) FE_COUNTS(FE_BRANCH_ROW,16) FE_COUNTS(FE_BRANCH_ROW,32)
    FE_BYTES(FE_NOP_ROW,4)
#if defined(__x86_64__)
    FE_BYTES(FE_NOP_ROW,1) FE_BYTES(FE_NOP_ROW,8)
#endif
};
#define FRONTEND_COUNT (sizeof(frontend_definitions) / sizeof(frontend_definitions[0]))
typedef struct {
    const FrontDefinition *definition;
    ArchCounter *counter;
    ArchCounts counts[BENCH_SAMPLES];
    unsigned calls;
} FrontWork;
static int frontend_work(void *context, uint64_t repetitions) {
    FrontWork *work = context;
    ArchCounts start, end, delta = {0};
    int valid = strcmp(work->counter->source, "unavailable") && !arch_counter_read(work->counter, &start);
    for (uint64_t i = 0; i < repetitions; ++i) work->definition->run();
    if (!valid || arch_counter_read(work->counter, &end) || arch_counter_delta(&start, &end, &delta))
        memset(&delta, 0, sizeof(delta));
    work->counts[work->calls++ % BENCH_SAMPLES] = delta;
    return 0;
}
static int frontend_report(FILE *json, ReportContext *report) {
    ArchCounter counter = arch_counter_open();
    uint64_t target = BENCH_TARGET_NS < UINT64_C(10000000) ? BENCH_TARGET_NS : UINT64_C(10000000);
    uint64_t warmup = BENCH_WARMUP_NS < UINT64_C(2000000) ? BENCH_WARMUP_NS : UINT64_C(2000000);
    fprintf(json, ",\"frontend\":{\"protocol\":\"frontend-static-v2\",\"sample_target_ns\":%llu,"
        "\"counter_source\":\"%s\",\"decode_counter_source\":\"%s\",\"records\":[",
        (unsigned long long)target, counter.source, counter.decode_source);
    report_section(report, "Front-end Working Sets");
    report_write(report, "- Counter source: %s\n- BTB: 128–32768 taken targets, 8/16/32-byte spacing.\n"
        "- NOP working sets: 128 B–256 KiB. Calls, returns and loop overhead are retained.\n"
        "- Raw cycle ratios describe effective instruction supply; physical fetch/decode widths are not identified.\n\n", counter.source);
    for (unsigned index = 0; index < FRONTEND_COUNT; ++index) {
        const FrontDefinition *def = &frontend_definitions[index];
        FrontWork work = {.definition = def, .counter = &counter};
        BenchMeasurement result;
        printf("Front-end probe: %s %u x %u B\n", def->kind, def->count, def->stride); fflush(stdout);
        if (bench_measure_target(frontend_work, &work, &result, target, warmup)) {
            arch_counter_close(&counter); return -1;
        }
        fprintf(json, "%s{\"kind\":\"%s\",\"count\":%u,\"stride\":%u,\"code_bytes\":%u,\"blocks\":%llu,\"elapsed_ns\":[",
            index ? "," : "", def->kind, def->count, def->stride, def->bytes, (unsigned long long)result.repetitions);
        for (unsigned i = 0; i < BENCH_SAMPLES; ++i)
            fprintf(json, "%s%llu", i ? "," : "", (unsigned long long)result.samples[i]);
        fputs("],\"cycles\":[", json);
        for (unsigned i = 0; i < BENCH_SAMPLES; ++i)
            fprintf(json, "%s%llu", i ? "," : "", (unsigned long long)work.counts[(work.calls+i) % BENCH_SAMPLES].cycles);
        fputs("],\"instructions\":[", json);
        for (unsigned i = 0; i < BENCH_SAMPLES; ++i)
            fprintf(json, "%s%llu", i ? "," : "", (unsigned long long)work.counts[(work.calls+i) % BENCH_SAMPLES].instructions);
        fputs("],\"mite_uops\":[", json);
        for (unsigned i = 0; i < BENCH_SAMPLES; ++i)
            fprintf(json, "%s%llu", i ? "," : "", (unsigned long long)work.counts[(work.calls+i) % BENCH_SAMPLES].mite);
        fputs("]}", json);
    }
    fputs("]}", json);
    arch_counter_close(&counter);
    return ferror(json) ? -1 : 0;
}
#endif
