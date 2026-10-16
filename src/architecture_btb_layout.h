#ifndef ARCHITECTURE_BTB_LAYOUT_H
#define ARCHITECTURE_BTB_LAYOUT_H
/* Small BTB structures need low target counts and PC-index controls. A second
 * layout offset is required before accepting a spacing-specific transition. */
#define BTB_LAYOUT_COUNTS(M,s,o) M(16,s,o) M(32,s,o) M(48,s,o) M(64,s,o) M(96,s,o) M(128,s,o) M(192,s,o) M(256,s,o) M(384,s,o) M(512,s,o) M(768,s,o) M(1024,s,o) M(1536,s,o) M(2048,s,o) M(3072,s,o) M(4096,s,o)
#define BTB_LAYOUT_SPACINGS(M,o) BTB_LAYOUT_COUNTS(M,4,o) BTB_LAYOUT_COUNTS(M,8,o) BTB_LAYOUT_COUNTS(M,16,o) BTB_LAYOUT_COUNTS(M,32,o) BTB_LAYOUT_COUNTS(M,64,o)
#if defined(__x86_64__)
#define BTB_LAYOUT_SETUP "lea 8f(%%rip),%[first]\nlea 9f(%%rip),%[last]\njmp 8f\n"
#define BTB_LAYOUT_TAIL "dec %[reps]\n.byte 0x0f,0x85\n.long 8b-.-4\n"
#else
#define BTB_LAYOUT_SETUP "adr %[first],8f\nadr %[last],9f\nb 8f\n"
#define BTB_LAYOUT_TAIL "subs %[reps],%[reps],#1\nb.ne 8b\n"
#endif
#define BTB_LAYOUT_FN(n,s,o) \
static __attribute__((noinline, aligned(128))) void btb_layout_##n##_##s##_##o(uint64_t reps, uintptr_t *begin, uintptr_t *end) { \
    uintptr_t first, last; \
    __asm__ volatile(BTB_LAYOUT_SETUP ".balign 128\n.if " FE_STR(o) "\n.space " FE_STR(o) "\n.endif\n8:\n.rept " FE_STR(n) "\n" \
        FE_BRANCH(s) ".endr\n9:\n" BTB_LAYOUT_TAIL \
        : [reps] "+&r" (reps), [first] "=&r" (first), [last] "=&r" (last) :: "cc", "memory"); \
    *begin = first; *end = last; \
}
BTB_LAYOUT_SPACINGS(BTB_LAYOUT_FN,0) BTB_LAYOUT_SPACINGS(BTB_LAYOUT_FN,32)
typedef struct { unsigned count, stride, offset; WidthKernel run; } BtbLayoutDefinition;
#define BTB_LAYOUT_ROW(n,s,o) {n,s,o,btb_layout_##n##_##s##_##o},
static const BtbLayoutDefinition btb_layout_definitions[] = {BTB_LAYOUT_SPACINGS(BTB_LAYOUT_ROW,0) BTB_LAYOUT_SPACINGS(BTB_LAYOUT_ROW,32)};
typedef struct { const BtbLayoutDefinition *definition; uintptr_t begin, end; } BtbLayoutWork;
static int btb_layout_work(void *context, uint64_t reps) {
    BtbLayoutWork *work = context;
    work->definition->run(reps, &work->begin, &work->end);
    return work->begin%128 != work->definition->offset || work->end-work->begin != work->definition->count*work->definition->stride;
}
static int btb_layout_report(FILE *json, ReportContext *report) {
    uint64_t target = BENCH_TARGET_NS < UINT64_C(2000000) ? BENCH_TARGET_NS : UINT64_C(2000000);
    uint64_t warmup = BENCH_WARMUP_NS < UINT64_C(250000) ? BENCH_WARMUP_NS : UINT64_C(250000);
    fprintf(json, ",\"btb_layout_scan\":{\"protocol\":\"btb-layout-v1\",\"sample_target_ns\":%llu,\"loop_control_branches\":1,\"records\":[", (unsigned long long)target);
    report_section(report, "BTB Layout Controls");
    report_write(report, "- 16–4096 targets; 4/8/16/32/64 B spacing; 0/32 B offsets modulo 128.\n"
        "- One additional loop-control branch per iteration. Two offsets and reversed sweep order.\n"
        "- Transitions are spacing-specific; target count, PC indexing and code footprint retained.\n\n");
    unsigned count = sizeof(btb_layout_definitions)/sizeof(btb_layout_definitions[0]);
    for (unsigned i = 0; i < count; ++i) {
        unsigned index = i < count/2 ? i : count+count/2-1-i;
        BtbLayoutWork work = {.definition = &btb_layout_definitions[index]};
        const BtbLayoutDefinition *def = work.definition;
        BenchMeasurement result;
        printf("BTB layout: %u x %u B offset %u\n", def->count, def->stride, def->offset); fflush(stdout);
        if (bench_measure_target(btb_layout_work, &work, &result, target, warmup)) return -1;
        fprintf(json, "%s{\"count\":%u,\"stride\":%u,\"offset\":%u,\"actual_offset\":%zu,\"code_bytes\":%zu,\"blocks\":%llu,\"elapsed_ns\":[",
            i ? "," : "", def->count, def->stride, def->offset, (size_t)(work.begin%128), (size_t)(work.end-work.begin), (unsigned long long)result.repetitions);
        for (unsigned j = 0; j < BENCH_SAMPLES; ++j) fprintf(json, "%s%llu", j ? "," : "", (unsigned long long)result.samples[j]);
        fputs("]}", json);
        if (ferror(json)) return -1;
    }
    fputs("]}", json); return ferror(json) ? -1 : 0;
}
#endif
