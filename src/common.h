#ifndef COMMON_H
#define COMMON_H

#define _GNU_SOURCE

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/time.h>
#include <sys/stat.h>
#include <ctype.h>
#include <sched.h>
#include <pthread.h>
#include <stdbool.h>

#define NS_PER_SEC 1000000000ULL
#define KB (1024UL)
#define MB (1024UL * KB)
#define GB (1024UL * MB)

typedef struct {
    size_t l1d_size;
    size_t l1i_size;
    size_t l2_size;
    size_t l3_size;
    int detected;
} CacheConfig;

typedef struct {
    uint64_t min;
    uint64_t max;
    double avg;
    uint64_t p50;
    uint64_t p95;
    uint64_t p99;
} LatencyStats;

extern CacheConfig global_cache_config;

static inline uint64_t get_time_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * NS_PER_SEC + ts.tv_nsec;
}

static inline size_t parse_size(const char *str) {
    char *end;
    unsigned long val = strtoul(str, &end, 0);
    if (end && *end) {
        switch (toupper(*end)) {
            case 'K': return val * KB;
            case 'M': return val * MB;
            case 'G': return val * GB;
        }
    }
    return val;
}

static int compare_uint64(const void *a, const void *b) {
    uint64_t va = *(const uint64_t *)a;
    uint64_t vb = *(const uint64_t *)b;
    if (va < vb) return -1;
    if (va > vb) return 1;
    return 0;
}

static void print_header(const char *title) {
    printf("\n%s\n", title);
    for (size_t i = 0; i < strlen(title); i++) printf("=");
    printf("\n");
}

/* 报告生成相关函数 */
typedef enum {
    REPORT_FORMAT_TEXT,
    REPORT_FORMAT_JSON,
    REPORT_FORMAT_MARKDOWN
} ReportFormat;

typedef struct {
    char test_name[64];
    char timestamp[64];
    ReportFormat format;
    FILE *fp;
} ReportContext;

ReportContext *report_init(const char *test_name, ReportFormat format);
void report_write(ReportContext *ctx, const char *fmt, ...);
void report_section(ReportContext *ctx, const char *title);
void report_value(ReportContext *ctx, const char *key, const char *value);
void report_latency_table(ReportContext *ctx, const char *headers, const char *rows);
void report_free(ReportContext *ctx);
const char *report_get_filename(ReportContext *ctx);
void report_write_system_info(ReportContext *ctx);
void report_finalize_json(ReportContext *ctx);

/* Cache config functions */
int load_cache_config(void);
void save_cache_config(void);
int detect_cache_sizes(void);
void initialize_cache_config(void);
void print_system_info(void);

/* CPU affinity helper */
int set_cpu_affinity(int cpu);

/* NUMA topology helper */
void print_numa_info(void);

/* Architecture detection */
typedef enum {
    ARCH_X86_64,
    ARCH_ARM64,
    ARCH_RISCV64,
    ARCH_POWERPC64,
    ARCH_UNKNOWN
} Architecture;

extern Architecture current_arch;
extern const char *arch_names[];

#endif /* COMMON_H */
