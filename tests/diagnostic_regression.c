/* Small, deterministic numerical fixtures, plus shortened production protocol. */
#define main diagnostic_program_main
#ifdef CHECK_BRANCH
#include "../src/test_cpu_branch.c"
#elif defined(CHECK_ALU)
#include "../src/test_cpu_alu.c"
#else
#include "../src/test_cpu_float.c"
#endif
#undef main

int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "report")) {
        global_system_config.detected = 1;
        global_system_config.cpu_cores = global_system_config.cpu_cores_physical = 1;
        strcpy(global_system_config.cpu_model, "bounded regression fixture");
        global_cache_config.detected = 1;
#ifdef CHECK_BRANCH
        return run_cpu_branch_test();
#elif defined(CHECK_ALU)
        return run_cpu_alu_test();
#else
        return run_cpu_float_test();
#endif
    }
#ifdef CHECK_BRANCH
    unsigned char input[4099];
    for (size_t i = 0; i < sizeof(input); ++i) input[i] = (i * 17 + 11) % 7 == 0 ? 0 : 255;
    printf("%llu %llu %llu\n", (unsigned long long)branch_count(input, 0),
           (unsigned long long)branch_count(input, 3), (unsigned long long)branch_count(input, sizeof(input)));
    for (unsigned i = 0; i < PATTERNS; ++i) {
        uint64_t expected = make_pattern(input, sizeof(input), i);
        printf("%u %llu %llu ", i, (unsigned long long)expected,
               (unsigned long long)branch_count(input, sizeof(input)));
        for (unsigned j = 0; j < 32; ++j) printf("%u", input[j]);
        putchar('\n');
    }
#elif defined(CHECK_ALU)
    for (unsigned i = 0; i < 1024; ++i) test_array[i] = i;
    for (unsigned i = 0; i < sizeof(tests) / sizeof(*tests); ++i) {
        if (alu_work(&tests[i], 3)) return 1;
        printf("%s %llu\n", tests[i].name, (unsigned long long)g_result);
    }
#else
    for (unsigned i = 0; i < sizeof(float_tests) / sizeof(*float_tests); ++i) {
        const FloatTest *test = &float_tests[i];
        test->kernel(3);
        printf("%s|%u|%.17g", test->name, test->operations, fp_result);
        for (unsigned j = 0; j < test->lanes; ++j) printf("|%.17g", fp_lanes[j]);
        putchar('\n');
    }
#endif
    return 0;
}
