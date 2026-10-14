/* macOS public sysctl helpers. Missing fields stay unknown, never guessed. */
#ifndef DARWIN_HELPERS_H
#define DARWIN_HELPERS_H
#ifdef __APPLE__
#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
#include <sys/sysctl.h>
#include <stdio.h>
#include <string.h>

#define DARWIN_MAX_PERF_LEVELS 16
typedef struct {
    char name[64];
    uint64_t physical, logical, l1d, l1i, l2, cpus_per_l2;
} DarwinPerfLevel;

static inline uint64_t darwin_uint(const char *name) {
    union { uint64_t u64; uint32_t u32; } value = {0};
    size_t size = sizeof(value);
    if (sysctlbyname(name, &value, &size, NULL, 0) != 0) return 0;
    if (size == sizeof(uint32_t)) return value.u32;
    return size == sizeof(uint64_t) ? value.u64 : 0;
}

static inline void darwin_string(const char *name, char *value, size_t size) {
    if (!size) return;
    size_t length = size;
    if (sysctlbyname(name, value, &length, NULL, 0) != 0) value[0] = '\0';
    value[size - 1] = '\0';
}

/* Enumerate what the OS exposes; perflevel numbers are not CPU IDs. Do not
 * assume two levels, or that every Mac has efficiency cores. */
static inline size_t darwin_perflevels(DarwinPerfLevel *levels, size_t capacity) {
    uint64_t reported = darwin_uint("hw.nperflevels");
    size_t count = reported < capacity ? (size_t)reported : capacity;
    for (size_t i = 0; i < count; ++i) {
        DarwinPerfLevel *level = &levels[i];
        memset(level, 0, sizeof(*level));
        char key[96];
        snprintf(key, sizeof(key), "hw.perflevel%zu.name", i);
        darwin_string(key, level->name, sizeof(level->name));
        if (!level->name[0]) snprintf(level->name, sizeof(level->name), "unknown (perflevel%zu)", i);
#define READ_LEVEL(field, suffix) \
        snprintf(key, sizeof(key), "hw.perflevel%zu." suffix, i); \
        level->field = darwin_uint(key)
        READ_LEVEL(physical, "physicalcpu");
        READ_LEVEL(logical, "logicalcpu");
        READ_LEVEL(l1d, "l1dcachesize");
        READ_LEVEL(l1i, "l1icachesize");
        READ_LEVEL(l2, "l2cachesize");
        READ_LEVEL(cpus_per_l2, "cpusperl2");
#undef READ_LEVEL
    }
    return count;
}
#endif
#endif
