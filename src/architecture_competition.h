#ifndef ARCHITECTURE_COMPETITION_H
#define ARCHITECTURE_COMPETITION_H
/* Four independent chains per arithmetic family, identical in isolated and
 * mixed streams. Mixed timing constrains resource sharing, not unique port
 * topology. Baseline SSE2 / AArch64 FP only; no frequency-derived cycles. */
#if defined(__x86_64__)
#define MIX_INIT "mov 0(%[mem]),%%r8\nmov 8(%[mem]),%%r9\nmov 16(%[mem]),%%r10\nmov 24(%[mem]),%%r11\n" \
    "mov 32(%[mem]),%%r12\nmov 40(%[mem]),%%r13\nmov 48(%[mem]),%%r14\nmov 56(%[mem]),%%r15\n" \
    "movsd 64(%[mem]),%%xmm0\nmovsd 72(%[mem]),%%xmm1\nmovsd 80(%[mem]),%%xmm2\nmovsd 88(%[mem]),%%xmm3\n" \
    "movsd 96(%[mem]),%%xmm4\nmovsd 104(%[mem]),%%xmm5\nmovsd 112(%[mem]),%%xmm6\nmovsd 120(%[mem]),%%xmm7\n" \
    "movsd 256(%[mem]),%%xmm8\nmovsd 264(%[mem]),%%xmm9\n"
#define MIX_SAVE "mov %%r8,0(%[mem])\nmov %%r9,8(%[mem])\nmov %%r10,16(%[mem])\nmov %%r11,24(%[mem])\n" \
    "mov %%r12,32(%[mem])\nmov %%r13,40(%[mem])\nmov %%r14,48(%[mem])\nmov %%r15,56(%[mem])\n" \
    "movsd %%xmm0,64(%[mem])\nmovsd %%xmm1,72(%[mem])\nmovsd %%xmm2,80(%[mem])\nmovsd %%xmm3,88(%[mem])\n" \
    "movsd %%xmm4,96(%[mem])\nmovsd %%xmm5,104(%[mem])\nmovsd %%xmm6,112(%[mem])\nmovsd %%xmm7,120(%[mem])\n"
#define MIX_IADD "add $3,%%r8\nadd $3,%%r9\nadd $3,%%r10\nadd $3,%%r11\n"
#define MIX_IMUL "imul $3,%%r12,%%r12\nimul $3,%%r13,%%r13\nimul $3,%%r14,%%r14\nimul $3,%%r15,%%r15\n"
#define MIX_FADD "addsd %%xmm8,%%xmm0\naddsd %%xmm8,%%xmm1\naddsd %%xmm8,%%xmm2\naddsd %%xmm8,%%xmm3\n"
#define MIX_FMUL "mulsd %%xmm9,%%xmm4\nmulsd %%xmm9,%%xmm5\nmulsd %%xmm9,%%xmm6\nmulsd %%xmm9,%%xmm7\n"
#define MIX_IADD8 MIX_IADD "add $3,%%r12\nadd $3,%%r13\nadd $3,%%r14\nadd $3,%%r15\n"
#define MIX_IMUL8 MIX_IMUL "imul $3,%%r8,%%r8\nimul $3,%%r9,%%r9\nimul $3,%%r10,%%r10\nimul $3,%%r11,%%r11\n"
#define MIX_FADD8 MIX_FADD "addsd %%xmm8,%%xmm4\naddsd %%xmm8,%%xmm5\naddsd %%xmm8,%%xmm6\naddsd %%xmm8,%%xmm7\n"
#define MIX_FMUL8 MIX_FMUL "mulsd %%xmm9,%%xmm0\nmulsd %%xmm9,%%xmm1\nmulsd %%xmm9,%%xmm2\nmulsd %%xmm9,%%xmm3\n"
#define MIX_IWRITE "lea 1(%%r12),%%r8\nlea 1(%%r12),%%r9\nlea 1(%%r12),%%r10\nlea 1(%%r12),%%r11\n"
#define MIX_VWRITE "pshufd $0x1b,%%xmm8,%%xmm0\npshufd $0x1b,%%xmm8,%%xmm1\npshufd $0x1b,%%xmm8,%%xmm2\npshufd $0x1b,%%xmm8,%%xmm3\n"
#define MIX_LOAD "mov 128(%[mem]),%%r12\nmov 136(%[mem]),%%r13\nmov 144(%[mem]),%%r14\nmov 152(%[mem]),%%r15\n"
#define MIX_STORE "mov %%r8,192(%[mem])\nmov %%r9,200(%[mem])\nmov %%r10,208(%[mem])\nmov %%r11,216(%[mem])\n"
#define MIX_TAIL "dec %[reps]\njnz 8b\n"
#define MIX_CLOBBERS "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15", "xmm0", "xmm1", "xmm2", "xmm3", "xmm4", "xmm5", "xmm6", "xmm7", "xmm8", "xmm9"
#else
#define MIX_INIT "ldp x8,x9,[%[mem]]\nldp x10,x11,[%[mem],#16]\nldp x12,x13,[%[mem],#32]\nldp x14,x15,[%[mem],#48]\n" \
    "ldp d0,d1,[%[mem],#64]\nldp d2,d3,[%[mem],#80]\nldp d4,d5,[%[mem],#96]\nldp d6,d7,[%[mem],#112]\n" \
    "ldp d16,d17,[%[mem],#256]\nmov x16,#3\n"
#define MIX_SAVE "stp x8,x9,[%[mem]]\nstp x10,x11,[%[mem],#16]\nstp x12,x13,[%[mem],#32]\nstp x14,x15,[%[mem],#48]\n" \
    "stp d0,d1,[%[mem],#64]\nstp d2,d3,[%[mem],#80]\nstp d4,d5,[%[mem],#96]\nstp d6,d7,[%[mem],#112]\n"
#define MIX_IADD "add x8,x8,#3\nadd x9,x9,#3\nadd x10,x10,#3\nadd x11,x11,#3\n"
#define MIX_IMUL "mul x12,x12,x16\nmul x13,x13,x16\nmul x14,x14,x16\nmul x15,x15,x16\n"
#define MIX_FADD "fadd d0,d0,d16\nfadd d1,d1,d16\nfadd d2,d2,d16\nfadd d3,d3,d16\n"
#define MIX_FMUL "fmul d4,d4,d17\nfmul d5,d5,d17\nfmul d6,d6,d17\nfmul d7,d7,d17\n"
#define MIX_IADD8 MIX_IADD "add x12,x12,#3\nadd x13,x13,#3\nadd x14,x14,#3\nadd x15,x15,#3\n"
#define MIX_IMUL8 MIX_IMUL "mul x8,x8,x16\nmul x9,x9,x16\nmul x10,x10,x16\nmul x11,x11,x16\n"
#define MIX_FADD8 MIX_FADD "fadd d4,d4,d16\nfadd d5,d5,d16\nfadd d6,d6,d16\nfadd d7,d7,d16\n"
#define MIX_FMUL8 MIX_FMUL "fmul d0,d0,d17\nfmul d1,d1,d17\nfmul d2,d2,d17\nfmul d3,d3,d17\n"
#define MIX_IWRITE "add x8,x12,#1\nadd x9,x12,#1\nadd x10,x12,#1\nadd x11,x12,#1\n"
#define MIX_VWRITE "add v0.2d,v16.2d,v16.2d\nadd v1.2d,v16.2d,v16.2d\nadd v2.2d,v16.2d,v16.2d\nadd v3.2d,v16.2d,v16.2d\n"
#define MIX_LOAD "ldr x12,[%[mem],#128]\nldr x13,[%[mem],#136]\nldr x14,[%[mem],#144]\nldr x15,[%[mem],#152]\n"
#define MIX_STORE "str x8,[%[mem],#192]\nstr x9,[%[mem],#200]\nstr x10,[%[mem],#208]\nstr x11,[%[mem],#216]\n"
#define MIX_TAIL "subs %[reps],%[reps],#1\nb.ne 8b\n"
/* Keep this aligned leaf entirely caller-saved. D8/D9 spills would require
 * SEH unwind records on Windows ARM; LLVM cannot size internal alignment
 * fragments while emitting those records. V16/V17 are ABI scratch registers. */
#define MIX_CLOBBERS "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v16", "v17"
#endif
#define MIX_NONE ""
/* Expand these small fixed streams in C, preserving x86 $ immediates through
 * Darwin assemblers' positional-argument handling inside .rept macros. */
#define MIX_REPEAT_4(text) text text text text
#define MIX_REPEAT_8(text) MIX_REPEAT_4(text) MIX_REPEAT_4(text)
#define MIX_REPEAT_16(text) MIX_REPEAT_8(text) MIX_REPEAT_8(text)
#define MIX_STREAM_4_32(a,b) MIX_REPEAT_8(MIX_##a MIX_##b)
#define MIX_STREAM_8_32(a,b) MIX_REPEAT_4(MIX_##a MIX_##b)
#define MIX_STREAM_4_64(a,b) MIX_REPEAT_16(MIX_##a MIX_##b)
#define MIX_STREAM_8_64(a,b) MIX_REPEAT_8(MIX_##a MIX_##b)
#define MIX_LIST(M,u) M(iadd,IADD,NONE,4,u) M(imul,IMUL,NONE,4,u) M(fadd,FADD,NONE,4,u) M(fmul,FMUL,NONE,4,u) \
    M(load,LOAD,NONE,4,u) M(store,STORE,NONE,4,u) M(iadd_imul,IADD,IMUL,8,u) M(fadd_fmul,FADD,FMUL,8,u) \
    M(iadd_fadd,IADD,FADD,8,u) M(load_store,LOAD,STORE,8,u) M(iadd_load,IADD,LOAD,8,u) M(fadd_load,FADD,LOAD,8,u) \
    M(iadd8,IADD8,NONE,8,u) M(imul8,IMUL8,NONE,8,u) M(fadd8,FADD8,NONE,8,u) M(fmul8,FMUL8,NONE,8,u) \
    M(iwrite,IWRITE,NONE,4,u) M(vwrite,VWRITE,NONE,4,u) M(iwrite_vwrite,IWRITE,VWRITE,8,u)
#define MIX_FN(name,a,b,group,u) \
static __attribute__((noinline,aligned(64))) void mix_##name##_##u(uint64_t reps, void *memory) { \
    __asm__ volatile(MIX_INIT ".balign 64\n8:\n" MIX_STREAM_##group##_##u(a,b) MIX_TAIL MIX_SAVE \
        : [reps] "+&r" (reps) : [mem] "r" (memory) : MIX_CLOBBERS, "cc", "memory"); \
}
MIX_LIST(MIX_FN,32) MIX_LIST(MIX_FN,64)
typedef struct { const char *family; unsigned unroll; void (*run)(uint64_t, void *); } CompetitionDefinition;
#define MIX_ROW(name,a,b,group,u) {#name,u,mix_##name##_##u},
static const CompetitionDefinition competition_definitions[] = {MIX_LIST(MIX_ROW,32) MIX_LIST(MIX_ROW,64)};
static void competition_initialize(uint64_t *memory) {
    for (unsigned i = 0; i < 64; ++i) memory[i] = i+1;
    for (unsigned i = 0; i < 8; ++i) { double value = i+1; memcpy(memory+8+i, &value, sizeof(value)); }
    double add = .25, multiply = 1.0 + 0x1p-32;
    memcpy(memory+32, &add, sizeof(add)); memcpy(memory+33, &multiply, sizeof(multiply));
}
typedef struct {
    const CompetitionDefinition *definition;
    ArchCounter *counter;
    unsigned calls;
    ArchCounts counts[BENCH_SAMPLES];
    _Alignas(64) uint64_t memory[64];
} CompetitionWork;
static int competition_work(void *context, uint64_t reps) {
    CompetitionWork *work = context;
    competition_initialize(work->memory);
    ArchCounts before, after, delta = {0};
    int valid = strcmp(work->counter->source, "unavailable") && !arch_counter_read(work->counter, &before);
    work->definition->run(reps, work->memory);
    if (!valid || arch_counter_read(work->counter, &after) || arch_counter_delta(&before, &after, &delta)) memset(&delta, 0, sizeof(delta));
    work->counts[work->calls++ % BENCH_SAMPLES] = delta; return 0;
}
static int competition_report(FILE *json, ReportContext *report) {
    ArchCounter counter = arch_counter_open();
    uint64_t target = BENCH_TARGET_NS < UINT64_C(5000000) ? BENCH_TARGET_NS : UINT64_C(5000000);
    uint64_t warmup = BENCH_WARMUP_NS < UINT64_C(500000) ? BENCH_WARMUP_NS : UINT64_C(500000);
    fprintf(json, ",\"competition_scan\":{\"protocol\":\"mixed-throughput-v2\",\"sample_target_ns\":%llu,\"counter_source\":\"%s\","
        "\"decode_counter_source\":\"unavailable\",\"counter_status\":\"%s\",\"counter_error\":%d,\"chains_per_family\":4,\"records\":[",
        (unsigned long long)target, counter.source, counter.status, counter.error);
    report_section(report, "Execution Resource Competition");
    report_write(report, "- Four independent chains/family; 32/64 payload instructions; single and 1:1 mixed streams.\n"
        "- Isolated ADD/MUL/FP64/loads/stores and six mixtures. Disjoint load/store addresses in 512 B.\n"
        "- Eight-chain arithmetic controls and independent integer/vector result writes, isolated and mixed.\n"
        "- Competition includes frontend, dependency latency, execution and retirement limits. Physical port count remains underdetermined.\n\n");
    int failed = 0;
    for (unsigned i = 0; i < sizeof(competition_definitions)/sizeof(competition_definitions[0]); ++i) {
        CompetitionWork work = {.definition = &competition_definitions[i], .counter = &counter};
        BenchMeasurement result;
        printf("Execution competition: %s %u instructions\n", work.definition->family, work.definition->unroll); fflush(stdout);
        if (bench_measure_target(competition_work, &work, &result, target, warmup)) { failed = 1; break; }
        ArchSampleBatch previous;
        arch_batch_capture(&previous, &result, work.calls, NULL, work.counts);
        int retried = !arch_batch_stable(&previous, strcmp(counter.source, "unavailable"));
        if (retried && bench_measure_target(competition_work, &work, &result, target*4, warmup*4)) { failed = 1; break; }
        fprintf(json, "%s{\"family\":\"%s\",\"unroll\":%u,\"blocks\":%llu,\"elapsed_ns\":[", i ? "," : "",
            work.definition->family, work.definition->unroll, (unsigned long long)result.repetitions);
        for (unsigned j = 0; j < BENCH_SAMPLES; ++j) fprintf(json, "%s%llu", j ? "," : "", (unsigned long long)result.samples[j]);
        const char *fields[] = {"cycles", "instructions"};
        for (unsigned f = 0; f < 2; ++f) {
            fprintf(json, "],\"%s\":[", fields[f]);
            for (unsigned j = 0; j < BENCH_SAMPLES; ++j) {
                ArchCounts *counts = &work.counts[(work.calls+j)%BENCH_SAMPLES];
                fprintf(json, "%s%llu", j ? "," : "", (unsigned long long)(f ? counts->instructions : counts->cycles));
            }
        }
        fputc(']', json);
        arch_previous_json(json, &previous, retried, 1, 0);
        fputc('}', json); if (ferror(json)) { failed = 1; break; }
    }
    fputs("]}", json); arch_counter_close(&counter); return failed || ferror(json) ? -1 : 0;
}
#endif
