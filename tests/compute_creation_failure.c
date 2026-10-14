/* Exercise the exact partial-creation case: one waiting worker, then EAGAIN. */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>

static int calls;
static int fail_second_create(pthread_t *id, const pthread_attr_t *attr,
                              void *(*fn)(void *), void *arg) {
    if (++calls == 2) return EAGAIN;
    return pthread_create(id, attr, fn, arg);
}
#define pthread_create fail_second_create
#define main compute_program_main
#include "../src/test_cpu_multi.c"
#undef main
#undef pthread_create

int main(void) {
    int *cpus = NULL; size_t slots = 0;
    int count = allowed_cpus(&cpus, &slots);
    if (count < 2) { free(cpus); return 77; }
    Sample sample;
    int rc = measure(0, 2, cpus, slots, 32768, &sample);
    free(cpus);
    return rc == EAGAIN && calls == 2 ? 0 : 1;
}
