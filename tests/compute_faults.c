/* LD_PRELOAD fault injection for fail-closed tests; never linked into benchmarks. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdlib.h>

int pthread_setaffinity_np(pthread_t thread, size_t size, const cpu_set_t *set) {
    if (getenv("CM_TEST_AFFINITY_FAIL")) return EINVAL;
    int (*real_bind)(pthread_t, size_t, const cpu_set_t *) = dlsym(RTLD_NEXT, "pthread_setaffinity_np");
    return real_bind(thread, size, set);
}
