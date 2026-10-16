#ifndef ARCHITECTURE_RESOURCES_H
#define ARCHITECTURE_RESOURCES_H
/* Independently implemented dual-miss-window experiment. Static baseline-ISA
 * text only. Counts describe filler instructions, never physical ROB entries.
 * Parallel and serial controls have identical instruction/memory counts; the
 * serial control adds a real address dependency between the two random rings.
 * Both anchors join before the next iteration. Encoded, cache-line-aligned
 * pointers defeat plain pointer-value prefetch without permitting OOB access. */
#ifndef ARCH_RESOURCE_RING_BYTES
#define ARCH_RESOURCE_RING_BYTES (64u * 1024u * 1024u)
#endif
#define RES_SMALL(M,f,d) M(f,d,0) M(f,d,8) M(f,d,16) M(f,d,24) M(f,d,32) M(f,d,48) M(f,d,64) M(f,d,80) M(f,d,96) M(f,d,128) M(f,d,160) M(f,d,192) M(f,d,224) M(f,d,256) M(f,d,320) M(f,d,384) M(f,d,448) M(f,d,512)
#define RES_LARGE(M,f,d) RES_SMALL(M,f,d) M(f,d,640) M(f,d,768) M(f,d,1024) M(f,d,1280) M(f,d,1536) M(f,d,2048)
#define RES_ROB(M,f,d) RES_LARGE(M,f,d) M(f,d,2560) M(f,d,3072) M(f,d,3584) M(f,d,4096)
#if defined(__aarch64__) || defined(__arm64__)
#define RES_SCALAR_FP(M,d) RES_LARGE(M,prf_fp32,d) RES_LARGE(M,prf_fp64,d) RES_SMALL(M,sched_fp32,d) RES_SMALL(M,sched_fp64,d)
#else
/* Legacy SSE scalar operations preserve an upper destination lane and can
 * carry false dependencies. Do not use them as independent PRF allocators. */
#define RES_SCALAR_FP(M,d)
#endif
#define RES_FAMILIES(M,d) RES_ROB(M,rob_nop,d) RES_LARGE(M,prf_i32,d) RES_LARGE(M,prf_i64,d) RES_LARGE(M,prf_vec128,d) \
    RES_SMALL(M,sched_int,d) RES_SMALL(M,sched_vector,d) RES_SMALL(M,load_ready,d) RES_SMALL(M,load_wait,d) \
    RES_SMALL(M,store_ready,d) RES_SMALL(M,store_wait,d) RES_SCALAR_FP(M,d)
#if defined(__x86_64__)
#define RES_INIT "lea 64(%[scratch]),%%r8\nmovdqu (%[scratch]),%%xmm0\n"
#define RES_ANCHOR_A "mov (%[a]),%[a]\nxor %[key],%[a]\n"
#define RES_ANCHOR_B "mov (%[b]),%[b]\nxor %[key],%[b]\n"
#define RES_WAIT "mov %[a],%%r10\nand $63,%%r10\nadd %%r8,%%r10\nmovq %%r10,%%xmm1\n"
#define RES_PAYLOAD_rob_nop "nop\n"
#define RES_PAYLOAD_prf_i32 "lea 1(%%r8),%%r9d\n"
#define RES_PAYLOAD_prf_i64 "lea 1(%%r8),%%r9\n"
/* Darwin's .rept macro expansion consumes positional $0 in $0x1b. Encode
 * PSHUFD xmm0/xmm1 -> xmm2, imm8=0x1b explicitly on every x86 assembler. */
#define RES_PAYLOAD_prf_vec128 ".byte 0x66,0x0f,0x70,0xd0,0x1b\n"
#define RES_PAYLOAD_sched_int "lea 1(%%r10),%%r9\n"
#define RES_PAYLOAD_sched_vector ".byte 0x66,0x0f,0x70,0xd1,0x1b\n"
#define RES_PAYLOAD_load_ready "mov res_off%=(%%r8),%%r9\n"
#define RES_PAYLOAD_load_wait "mov res_off%=(%%r10),%%r9\n"
#define RES_PAYLOAD_store_ready "mov %%r8,res_off%=(%%r8)\n"
#define RES_PAYLOAD_store_wait "mov %%r10,res_off%=(%%r8)\n"
#define RES_GATE(s) ".if " FE_STR(s) "\nmov %[a],%%r11\n.else\nmov %[scratch],%%r11\n.endif\nand $63,%%r11\nadd %%r11,%[b]\n"
#define RES_JOIN "mov %[a],%%r11\nand $63,%%r11\nadd %%r11,%[b]\nmov %[b],%%r11\nand $63,%%r11\nadd %%r11,%[a]\n"
#define RES_TAIL "dec %[reps]\njnz 8b\n"
#define RES_CLOBBERS "r8", "r9", "r10", "r11", "xmm0", "xmm1", "xmm2"
#else
#define RES_INIT "add x8,%[scratch],#64\nldr q0,[%[scratch]]\n"
#define RES_ANCHOR_A "ldr %[a],[%[a]]\neor %[a],%[a],%[key]\n"
#define RES_ANCHOR_B "ldr %[b],[%[b]]\neor %[b],%[b],%[key]\n"
#define RES_WAIT "and x10,%[a],#63\nadd x10,x10,x8\ndup v1.2d,x10\nucvtf d3,x10\nfcvt s4,d3\n"
#define RES_PAYLOAD_rob_nop "nop\n"
#define RES_PAYLOAD_prf_i32 "add w9,w8,#1\n"
#define RES_PAYLOAD_prf_i64 "add x9,x8,#1\n"
#define RES_PAYLOAD_prf_vec128 "add v2.2d,v0.2d,v0.2d\n"
#define RES_PAYLOAD_prf_fp32 "fadd s2,s0,s0\n"
#define RES_PAYLOAD_prf_fp64 "fadd d2,d0,d0\n"
#define RES_PAYLOAD_sched_int "add x9,x10,#1\n"
#define RES_PAYLOAD_sched_vector "add v2.2d,v1.2d,v0.2d\n"
#define RES_PAYLOAD_sched_fp32 "fadd s2,s4,s0\n"
#define RES_PAYLOAD_sched_fp64 "fadd d2,d3,d0\n"
#define RES_PAYLOAD_load_ready "ldr x9,[x8,#res_off%=]\n"
#define RES_PAYLOAD_load_wait "ldr x9,[x10,#res_off%=]\n"
#define RES_PAYLOAD_store_ready "str x8,[x8,#res_off%=]\n"
#define RES_PAYLOAD_store_wait "str x10,[x8,#res_off%=]\n"
#define RES_GATE(s) ".if " FE_STR(s) "\nand x11,%[a],#63\n.else\nand x11,%[scratch],#63\n.endif\nadd %[b],%[b],x11\n"
#define RES_JOIN "and x11,%[a],#63\nadd %[b],%[b],x11\nand x11,%[b],#63\nadd %[a],%[a],x11\n"
#define RES_TAIL "subs %[reps],%[reps],#1\nb.ne 8b\n"
#define RES_CLOBBERS "x8", "x9", "x10", "x11", "v0", "v1", "v2", "v3", "v4"
#endif
#define RES_FN(f,d,n,s) \
static __attribute__((noinline, aligned(64))) void res_##f##_##d##_##n##_##s(uint64_t reps, uintptr_t *pa, uintptr_t *pb, uintptr_t key, void *scratch) { \
    uintptr_t a = *pa, b = *pb; \
    __asm__ volatile(RES_INIT ".balign 64\n8:\n.rept " FE_STR(d) "\n" RES_ANCHOR_A ".endr\n" RES_WAIT \
        ".set res_off%=,0\n.rept " FE_STR(n) "\n" RES_PAYLOAD_##f ".set res_off%=,res_off%=+64\n.endr\n" \
        RES_GATE(s) ".rept " FE_STR(d) "\n" RES_ANCHOR_B ".endr\n" RES_JOIN RES_TAIL \
        : [a] "+&r" (a), [b] "+&r" (b), [reps] "+&r" (reps) \
        : [key] "r" (key), [scratch] "r" (scratch) : RES_CLOBBERS, "cc", "memory"); \
    *pa = a; *pb = b; \
}
#define RES_PAIR(f,d,n) RES_FN(f,d,n,0) RES_FN(f,d,n,1)
RES_FAMILIES(RES_PAIR,1) RES_FAMILIES(RES_PAIR,2)
typedef void (*ResourceKernel)(uint64_t, uintptr_t *, uintptr_t *, uintptr_t, void *);
typedef struct { const char *family; unsigned depth, count; ResourceKernel run[2]; } ResourceDefinition;
#define RES_ROW(f,d,n) {#f,d,n,{res_##f##_##d##_##n##_0,res_##f##_##d##_##n##_1}},
static const ResourceDefinition resource_definitions[] = {RES_FAMILIES(RES_ROW,1) RES_FAMILIES(RES_ROW,2)};
typedef struct { void *allocation; unsigned char *base; uintptr_t head; size_t bytes; } ResourceRing;
static uint64_t resource_random(uint64_t *state) {
    uint64_t x = *state; x ^= x << 13; x ^= x >> 7; x ^= x << 17; return *state = x;
}
static int resource_ring_init(ResourceRing *ring, size_t bytes, uintptr_t key, uint64_t seed) {
    if (bytes < 128 || bytes % 64 || bytes/64 > UINT32_MAX || bytes > SIZE_MAX-63) return -1;
    ring->allocation = malloc(bytes + 63);
    uint32_t *order = malloc(bytes/64 * sizeof(*order));
    if (!ring->allocation || !order) { free(order); free(ring->allocation); memset(ring, 0, sizeof(*ring)); return -1; }
    ring->base = (unsigned char *)(((uintptr_t)ring->allocation + 63) & ~(uintptr_t)63);
    ring->bytes = bytes;
    size_t nodes = bytes/64;
    for (size_t i = 0; i < nodes; ++i) order[i] = (uint32_t)i;
    for (size_t i = nodes-1; i; --i) {
        size_t j = (size_t)(resource_random(&seed) % (i+1));
        uint32_t swap = order[i]; order[i] = order[j]; order[j] = swap;
    }
    for (size_t i = 0; i < nodes; ++i) {
        uintptr_t next = (uintptr_t)(ring->base + 64*order[(i+1)%nodes]) ^ key;
        memcpy(ring->base + 64*order[i], &next, sizeof(next));
    }
    ring->head = (uintptr_t)(ring->base + 64*order[0]); free(order); return 0;
}
static int resource_ring_contains(const ResourceRing *ring, uintptr_t head) {
    uintptr_t begin = (uintptr_t)ring->base;
    return head >= begin && head-begin < ring->bytes && !(head & 63);
}
typedef struct {
    const ResourceDefinition *definition;
    ResourceRing *a, *b;
    uintptr_t key;
    void *scratch;
    unsigned calls;
    uint64_t elapsed[2][BENCH_SAMPLES];
} ResourceWork;
static int resource_work(void *context, uint64_t repetitions) {
    ResourceWork *work = context;
    for (unsigned pass = 0; pass < 2; ++pass) {
        unsigned serial = pass ^ (work->calls & 1), slot = work->calls % BENCH_SAMPLES;
        uint64_t start = get_time_ns();
        work->definition->run[serial](repetitions, &work->a->head, &work->b->head, work->key, work->scratch);
        uint64_t stop = get_time_ns();
        if (stop < start || !resource_ring_contains(work->a, work->a->head) || !resource_ring_contains(work->b, work->b->head)) return -1;
        work->elapsed[serial][slot] = stop-start;
    }
    ++work->calls; return 0;
}
static int resource_report(FILE *json, ReportContext *report) {
    const uintptr_t key = (uintptr_t)UINT64_C(0x9e3779b97f4a7c15);
    ResourceRing a = {0}, b = {0};
    _Alignas(64) unsigned char scratch[32768 + 64];
    memset(scratch, 0, sizeof(scratch));
    const uint64_t vector_input[2] = {UINT64_C(0x123456789abcdef0), UINT64_C(0x3141592653589793)};
    memcpy(scratch, vector_input, sizeof(vector_input));
    int available = !resource_ring_init(&a, ARCH_RESOURCE_RING_BYTES, key, 1234567) &&
                    !resource_ring_init(&b, ARCH_RESOURCE_RING_BYTES, key, 7654321);
    uint64_t target = BENCH_TARGET_NS < UINT64_C(2000000) ? BENCH_TARGET_NS : UINT64_C(2000000);
    uint64_t warmup = BENCH_WARMUP_NS < UINT64_C(250000) ? BENCH_WARMUP_NS : UINT64_C(250000);
    fprintf(json, ",\"resource_scan\":{\"protocol\":\"dual-ring-resource-v1\",\"status\":\"%s\","
        "\"ring_bytes\":%u,\"line_bytes\":64,\"scratch_bytes\":%zu,\"sample_target_ns\":%llu,\"records\":[",
        available ? "measured" : "allocation_failed", (unsigned)ARCH_RESOURCE_RING_BYTES, sizeof(scratch), (unsigned long long)target);
    report_section(report, "Backend Resource Windows");
    report_write(report, "- Dual encoded random rings: %u B each; 1/2 dependent loads per anchor; AB/BA serial controls.\n"
        "- Available speculative filler windows; NOP coalescing, dispatch, scheduler and memory queues may share a limit.\n"
        "- 32/64-bit integer and 128-bit vector destinations; vector payload is SSE2 shuffle / AArch64 SIMD add.\n"
        "- AArch64 adds independent scalar FP32/FP64 windows; baseline x86 SSE2 scalar PRF windows are unsupported.\n"
        "- Pending memory: address-dependent loads / data-dependent stores, distinct 64-byte locations.\n"
        "- Raw wall-time pairs retained. Physical capacities require matching controls and independent hardware validation.\n\n", (unsigned)ARCH_RESOURCE_RING_BYTES);
    int failed = 0;
    if (available) for (unsigned index = 0; index < sizeof(resource_definitions)/sizeof(resource_definitions[0]); ++index) {
        /* Reverse the second strength's size/family order to expose drift. */
        unsigned half = sizeof(resource_definitions)/sizeof(resource_definitions[0])/2;
        unsigned selected = index < half ? index : 3*half-1-index;
        const ResourceDefinition *def = &resource_definitions[selected];
        ResourceWork work = {.definition = def, .a = &a, .b = &b, .key = key, .scratch = scratch};
        BenchMeasurement result;
        printf("Resource window: %s %u depth %u\n", def->family, def->count, def->depth); fflush(stdout);
        if (bench_measure_target(resource_work, &work, &result, target, warmup)) { failed = 1; break; }
        for (unsigned serial = 0; serial < 2; ++serial) {
            fprintf(json, "%s{\"family\":\"%s\",\"depth\":%u,\"count\":%u,\"serial\":%u,\"blocks\":%llu,\"elapsed_ns\":[",
                index || serial ? "," : "", def->family, def->depth, def->count, serial, (unsigned long long)result.repetitions);
            for (unsigned i = 0; i < BENCH_SAMPLES; ++i) {
                uint64_t ns = work.elapsed[serial][(work.calls+i)%BENCH_SAMPLES];
                if (!ns) failed = 1;
                fprintf(json, "%s%llu", i ? "," : "", (unsigned long long)ns);
            }
            fputs("]}", json);
        }
        if (failed || ferror(json)) { failed = 1; break; }
    }
    fputs("]}", json); free(a.allocation); free(b.allocation);
    return failed || ferror(json) ? -1 : 0;
}
#endif
