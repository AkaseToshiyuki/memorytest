#define main architecture_program_main
#include "../src/test_cpu_architecture.c"
#undef main
int main(int argc, char **argv) {
#ifndef ARCH_KERNEL_ISA
    return 77;
#else
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
