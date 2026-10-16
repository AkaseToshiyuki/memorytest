#define main architecture_program_main
#include "../src/test_cpu_architecture.c"
#undef main
#ifdef ARCH_KERNEL_ISA
static int resource_reference_check(void) {
    ResourceRing a = {0}, b = {0};
    uintptr_t key = (uintptr_t)UINT64_C(0x9e3779b97f4a7c15);
    if (resource_ring_init(&a, 65536, key, 1234567) || resource_ring_init(&b, 65536, key, 7654321)) return 30;
    /* Independently walk every initialized node: one Hamiltonian cycle, no
     * duplicates, all aligned and contained. Kernels use XOR-encoded loads. */
    for (unsigned r = 0; r < 2; ++r) {
        ResourceRing *ring = r ? &b : &a;
        unsigned char visited[1024] = {0}; uintptr_t at = ring->head;
        for (unsigned i = 0; i < 1024; ++i) {
            if (!resource_ring_contains(ring, at)) return 31;
            unsigned index = (unsigned)((at-(uintptr_t)ring->base)/64);
            if (visited[index]++) return 32;
            uintptr_t next; memcpy(&next, (void *)at, sizeof(next)); at = next ^ key;
        }
        if (at != ring->head) return 33;
    }
    _Alignas(64) unsigned char storage[32768+192];
    unsigned char *scratch = storage+64;
    unsigned checked = 0;
    for (unsigned i = 0; i < sizeof(resource_definitions)/sizeof(resource_definitions[0]); ++i) {
        const ResourceDefinition *def = &resource_definitions[i];
        for (unsigned serial = 0; serial < 2; ++serial) {
            memset(storage, 0xa5, sizeof(storage));
            uintptr_t expected_a = a.head, expected_b = b.head;
            for (unsigned step = 0; step < 3*def->depth; ++step) {
                uintptr_t next;
                memcpy(&next, (void *)expected_a, sizeof(next)); expected_a = next ^ key;
                memcpy(&next, (void *)expected_b, sizeof(next)); expected_b = next ^ key;
            }
            def->run[serial](3, &a.head, &b.head, key, scratch);
            if (a.head != expected_a || b.head != expected_b) return 34;
            int stores = !strcmp(def->family, "store_ready") || !strcmp(def->family, "store_wait");
            for (size_t byte = 0; byte < sizeof(storage); ++byte) {
                unsigned char expected = 0xa5;
                if (stores && byte >= 128 && byte < 128+64*def->count && (byte-128)%64 < sizeof(uintptr_t)) {
                    uintptr_t value = (uintptr_t)(scratch+64);
                    expected = ((unsigned char *)&value)[(byte-128)%64];
                }
                if (storage[byte] != expected) return 35;
            }
            ++checked;
        }
    }
    free(a.allocation); free(b.allocation); printf("%u\n", checked); return 0;
}
static int btb_layout_reference_check(void) {
    unsigned count = sizeof(btb_layout_definitions)/sizeof(btb_layout_definitions[0]);
    for (unsigned i = 0; i < count; ++i) {
        BtbLayoutWork work = {.definition = &btb_layout_definitions[i]};
        if (btb_layout_work(&work, 3)) return 40;
        const unsigned char *bytes = (const unsigned char *)work.begin;
        for (unsigned n = 0; n < work.definition->count; ++n) {
            unsigned stride = work.definition->stride;
#if defined(__x86_64__)
            if (bytes[n*stride] != 0xeb || bytes[n*stride+1] != stride-2) return 41;
            for (unsigned pad = 2; pad < stride; ++pad) if (bytes[n*stride+pad] != 0x90) return 42;
#else
            uint32_t instruction; memcpy(&instruction, bytes+n*stride, 4);
            if (instruction != UINT32_C(0x14000000) + stride/4) return 41;
            for (unsigned pad = 4; pad < stride; ++pad) if (bytes[n*stride+pad]) return 42;
#endif
        }
    }
    printf("%u\n", count); return 0;
}
static int competition_reference_output(void) {
    for (unsigned i = 0; i < sizeof(competition_definitions)/sizeof(competition_definitions[0]); ++i) {
        _Alignas(64) uint64_t memory[64];
        competition_initialize(memory);
        const CompetitionDefinition *def = &competition_definitions[i];
        def->run(3, memory);
        printf("%s %u", def->family, def->unroll);
        for (unsigned j = 0; j < 64; ++j) printf(" %016llx", (unsigned long long)memory[j]);
        putchar('\n');
    }
    return 0;
}
#endif
int main(int argc, char **argv) {
#ifndef ARCH_KERNEL_ISA
    return 77;
#else
    if (argc > 1 && !strcmp(argv[1], "resources")) return resource_reference_check();
    if (argc > 1 && !strcmp(argv[1], "btb-layout")) return btb_layout_reference_check();
    if (argc > 1 && !strcmp(argv[1], "competition")) return competition_reference_output();
    if (argc > 1 && !strcmp(argv[1], "layout")) {
        const unsigned indices[] = {0, 12, 60};
        for (unsigned i = 0; i < 3; ++i) for (unsigned c = 0; c < 2; ++c) {
            const WidthDefinition *d = &width_definitions[indices[i]];
            uintptr_t first, last; d->run[c](3, &first, &last);
            printf("%u %u %llu ", d->count, c, (unsigned long long)(first % WIDTH_ALIGNMENT));
            for (uintptr_t at = first; at < last; ++at) printf("%02x", *(const unsigned char *)at);
            putchar('\n');
        }
        return 0;
    }
    if (argc > 1) return architecture_program_main(1, argv);
    ArchCounts start = {100, 200, 300, 300}, end = {140, 280, 350, 350}, delta;
    if (arch_counter_delta(&start, &end, &delta) || delta.cycles != 40 || delta.instructions != 80) return 3;
    --end.running;
    if (!arch_counter_delta(&start, &end, &delta)) return 4;
    end = start;
    if (!arch_counter_delta(&start, &end, &delta)) return 5;
    for (unsigned kind = 1; kind < 9; ++kind) {
        ArchitectureWork work = {.kind = kind};
        if (architecture_work(&work, 3) || architecture_validate(&work, 3)) return 1;
        printf("%u", kind);
        for (unsigned i = 0; i < 8; ++i) {
            if (kind <= 4) printf(" %llu", (unsigned long long)work.integer[i]);
            else printf(" %.17g", work.floating[i]);
        }
        putchar('\n');
        if (kind <= 4) work.integer[3] ^= 1; else work.floating[3] += 1;
        if (!architecture_validate(&work, 3)) return 2;
    }
    return 0;
#endif
}
