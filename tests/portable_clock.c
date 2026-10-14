#include "../src/asm_helpers.h"

/* Independent surrounding reference, plus deliberately irrelevant CPU MHz. */
int get_cpu_freq_mhz(void) { return 1; }
int main(void) {
#ifdef _WIN32
    /* Independent Windows millisecond clock, not the QPC conversion helper. */
    uint64_t reference = GetTickCount64();
    uint64_t start = rdtsc_ns();
    Sleep(100);
    uint64_t elapsed = rdtsc_ns() - start;
    uint64_t reference_ns = (GetTickCount64() - reference) * UINT64_C(1000000);
    return elapsed >= UINT64_C(70000000) &&
           elapsed <= reference_ns + UINT64_C(40000000) &&
           elapsed + UINT64_C(40000000) >= reference_ns ? 0 : 1;
#else
    uint64_t outer_start = get_time_ns();
    uint64_t start = rdtsc_ns();
    struct timespec pause = {.tv_sec = 0, .tv_nsec = 20000000};
    nanosleep(&pause, NULL);
    uint64_t end = rdtsc_ns();
    uint64_t outer_end = get_time_ns();
    uint64_t inner = end - start, outer = outer_end - outer_start;
    return inner >= 10000000 && inner <= outer && inner >= outer * 9 / 10 ? 0 : 1;
#endif
}
