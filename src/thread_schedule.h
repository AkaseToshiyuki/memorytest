#ifndef MEMORYTEST_THREAD_SCHEDULE_H
#define MEMORYTEST_THREAD_SCHEDULE_H

#define CM_SCALING "powers-of-two-plus-max-v1"
#define CM_MAX_THREAD_LEVELS 19

/* Capacity is a logical-thread limit from the existing placement policy.
 * At most 19 levels for the supported [1, 262144] range; no duplicates. */
static int cm_thread_schedule(int maximum, int levels[CM_MAX_THREAD_LEVELS]) {
    if (maximum < 1 || maximum > 262144) return 0;
    int count = 0;
    for (int n = 1; n < maximum; n *= 2) levels[count++] = n;
    levels[count++] = maximum;
    return count;
}

#endif
