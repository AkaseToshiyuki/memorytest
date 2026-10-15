#include "../src/thread_schedule.h"
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int levels[CM_MAX_THREAD_LEVELS];
    int count = cm_thread_schedule(atoi(argv[1]), levels);
    for (int i = 0; i < count; ++i) printf("%s%d", i ? " " : "", levels[i]);
    puts("");
    return count ? 0 : 1;
}
