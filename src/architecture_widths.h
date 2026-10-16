#ifndef ARCHITECTURE_WIDTHS_H
#define ARCHITECTURE_WIDTHS_H
/* Page-end versus page-start loops, following the page-boundary fetch probe
 * described by JamesAslan and jiegec/cpu-micro-benchmarks. Independently
 * implemented static text; no JIT, writable text, private PMU or root request.
 * One four-byte NOP precedes the page boundary in the crossed variant. */
#define WIDTH_COUNTS(M) \
    M(4) M(5) M(6) M(7) M(8) M(9) M(10) M(11) M(12) M(13) M(14) M(15) M(16) \
    M(17) M(18) M(19) M(20) M(21) M(22) M(23) M(24) M(25) M(26) M(27) M(28) \
    M(29) M(30) M(31) M(32) M(33) M(34) M(35) M(36) M(37) M(38) M(39) M(40) \
    M(41) M(42) M(43) M(44) M(45) M(46) M(47) M(48) M(49) M(50) M(51) M(52) \
    M(53) M(54) M(55) M(56) M(57) M(58) M(59) M(60) M(61) M(62) M(63) M(64)
#if defined(__APPLE__)
/* Mach-O relocates function atoms independently and caps text alignment at
 * the segment's page alignment. Align the function atom as well as its loop. */
#if defined(__aarch64__) || defined(__arm64__)
#define WIDTH_ALIGNMENT 16384
#else
#define WIDTH_ALIGNMENT 4096
#endif
#define WIDTH_FUNCTION_ALIGNMENT WIDTH_ALIGNMENT
#elif defined(_WIN32)
#define WIDTH_ALIGNMENT 4096
#define WIDTH_FUNCTION_ALIGNMENT 64
#else
#define WIDTH_ALIGNMENT 65536
#define WIDTH_FUNCTION_ALIGNMENT 64
#endif
#if defined(__x86_64__)
#define WIDTH_SETUP "leaq 1f(%%rip),%[start]\nleaq 2f(%%rip),%[end]\njmp 1f\n"
/* Force a six-byte Jcc even in the short loops; no encoding-size discontinuity. */
#define WIDTH_TAIL "dec %[reps]\n.byte 0x0f,0x85\n.long 1b-.-4\n2:\n"
#define WIDTH_EXTRA_BYTES 1
#else
#define WIDTH_SETUP "adr %[start],1f\nadr %[end],2f\nb 1f\n"
#define WIDTH_TAIL "subs %[reps],%[reps],#1\nb.ne 1b\n2:\n"
#define WIDTH_EXTRA_BYTES 0
#endif
#define WIDTH_FN(n,c) \
static __attribute__((noinline, aligned(WIDTH_FUNCTION_ALIGNMENT))) void width_##n##_##c(uint64_t reps, uintptr_t *begin, uintptr_t *end) { \
    uintptr_t first, last; \
    __asm__ volatile(WIDTH_SETUP ".balign " FE_STR(WIDTH_ALIGNMENT) "\n.if " FE_STR(c) "\n.space " \
        FE_STR(WIDTH_ALIGNMENT) "-4\n.endif\n1:\n" \
        ".rept " FE_STR(n) "-2\n" FE_NOP_4 ".endr\n" WIDTH_TAIL \
        : [reps] "+r" (reps), [start] "=&r" (first), [end] "=&r" (last) :: "cc", "memory"); \
    *begin = first; *end = last; \
}
#define WIDTH_PAIR(n) WIDTH_FN(n,0) WIDTH_FN(n,1)
WIDTH_COUNTS(WIDTH_PAIR)
typedef void (*WidthKernel)(uint64_t, uintptr_t *, uintptr_t *);
typedef struct { unsigned count; WidthKernel run[2]; } WidthDefinition;
#define WIDTH_ROW(n) {n, {width_##n##_0, width_##n##_1}},
static const WidthDefinition width_definitions[] = {WIDTH_COUNTS(WIDTH_ROW)};
typedef struct {
    const WidthDefinition *definition;
    ArchCounter *counter;
    unsigned calls;
    uint64_t elapsed[2][BENCH_SAMPLES];
    ArchCounts counts[2][BENCH_SAMPLES];
    uintptr_t begin[2], end[2];
} WidthWork;
static int width_work(void *context, uint64_t repetitions) {
    WidthWork *work = context;
    unsigned slot = work->calls % BENCH_SAMPLES;
    for (unsigned pass = 0; pass < 2; ++pass) {
        /* AB/BA alternation: both variants use identical work and close timing. */
        unsigned crossed = pass ^ (work->calls & 1);
        ArchCounts before, after, delta = {0};
        int valid = strcmp(work->counter->source, "unavailable") && !arch_counter_read(work->counter, &before);
        uint64_t start = get_time_ns();
        work->definition->run[crossed](repetitions, &work->begin[crossed], &work->end[crossed]);
        uint64_t stop = get_time_ns();
        if (stop <= start) return -1;
        if (!valid || arch_counter_read(work->counter, &after) || arch_counter_delta(&before, &after, &delta))
            memset(&delta, 0, sizeof(delta));
        work->elapsed[crossed][slot] = stop-start;
        work->counts[crossed][slot] = delta;
    }
    ++work->calls;
    return 0;
}
static int width_report(FILE *json, ReportContext *report) {
    ArchCounter counter = arch_counter_open();
    uint64_t target = BENCH_TARGET_NS < UINT64_C(10000000) ? BENCH_TARGET_NS : UINT64_C(10000000);
    uint64_t warmup = BENCH_WARMUP_NS < UINT64_C(2000000) ? BENCH_WARMUP_NS : UINT64_C(2000000);
#ifdef _WIN32
    SYSTEM_INFO info; GetSystemInfo(&info); size_t page = info.dwPageSize;
#else
    long page_query = sysconf(_SC_PAGESIZE); size_t page = page_query > 0 ? (size_t)page_query : 0;
#endif
    if (!page || page > WIDTH_ALIGNMENT || WIDTH_ALIGNMENT % page) { arch_counter_close(&counter); return -1; }
    fprintf(json, ",\"width_scan\":{\"protocol\":\"page-boundary-width-v1\",\"page_bytes\":%zu,"
        "\"sample_target_ns\":%llu,\"counter_source\":\"%s\",\"decode_counter_source\":\"%s\",\"records\":[",
        page, (unsigned long long)target, counter.source, counter.decode_source);
    report_section(report, "Page-boundary Fetch Scan");
    report_write(report, "- 4–64 instructions/iteration; identical page-start/page-end loops, alternating AB/BA.\n"
        "- Page size: %zu B; 4-byte NOP; loop branch bytes included; raw wall times and hardware counts retained.\n"
        "- Width candidates require a distinctive paired staircase fit. Counter availability and decoder-path qualification are recorded.\n\n", page);
    for (unsigned index = 0; index < sizeof(width_definitions)/sizeof(width_definitions[0]); ++index) {
        WidthWork work = {.definition = &width_definitions[index], .counter = &counter};
        BenchMeasurement result;
        printf("Fetch width pair: %u instructions\n", work.definition->count); fflush(stdout);
        if (bench_measure_target(width_work, &work, &result, target, warmup)) { arch_counter_close(&counter); return -1; }
        for (unsigned crossed = 0; crossed < 2; ++crossed) {
            size_t bytes = 4 * work.definition->count + WIDTH_EXTRA_BYTES;
            if (work.begin[crossed] % page != (crossed ? page-4 : 0) || work.end[crossed]-work.begin[crossed] != bytes) {
                fprintf(stderr, "Fetch layout mismatch: count=%u crossed=%u page=%zu offset=%zu bytes=%zu expected=%zu\n",
                    work.definition->count, crossed, page, (size_t)(work.begin[crossed]%page),
                    (size_t)(work.end[crossed]-work.begin[crossed]), bytes);
                arch_counter_close(&counter); return -1;
            }
            fprintf(json, "%s{\"count\":%u,\"crossed\":%u,\"code_bytes\":%zu,\"page_offset\":%zu,"
                "\"blocks\":%llu,\"elapsed_ns\":[", index || crossed ? "," : "", work.definition->count, crossed,
                bytes, (size_t)(work.begin[crossed] % page), (unsigned long long)result.repetitions);
            for (unsigned i = 0; i < BENCH_SAMPLES; ++i)
                fprintf(json, "%s%llu", i ? "," : "", (unsigned long long)work.elapsed[crossed][(work.calls+i)%BENCH_SAMPLES]);
            const char *fields[] = {"cycles", "instructions", "mite_uops"};
            for (unsigned field = 0; field < 3; ++field) {
                fprintf(json, "],\"%s\":[", fields[field]);
                for (unsigned i = 0; i < BENCH_SAMPLES; ++i) {
                    ArchCounts *v = &work.counts[crossed][(work.calls+i)%BENCH_SAMPLES];
                    fprintf(json, "%s%llu", i ? "," : "", (unsigned long long)(field == 0 ? v->cycles : field == 1 ? v->instructions : v->mite));
                }
            }
            fputs("]}", json);
        }
    }
    fputs("]}", json); arch_counter_close(&counter);
    return ferror(json) ? -1 : 0;
}
#endif
