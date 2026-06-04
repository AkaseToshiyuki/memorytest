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
#include <sys/ioctl.h>
#include <ctype.h>
#include <sched.h>
#include <pthread.h>
#include <stdbool.h>
#include <linux/perf_event.h>

/* SIMD/Vector intrinsics.
 *
 * CRITICAL: don't include <immintrin.h> unconditionally — its SSE/AVX
 * intrinsics require specific -march/-mavx2/-mfma flags to inline.
 * For a portable -O2 build, we only include the headers for ISA extensions
 * that the *compiler* was told to target (the user can set -march= or
 * -mavx2/-mfma/-msse4.2 via the CFLAGS). For ISA extensions that are
 * *implicit* on the platform (NEON on AArch64), we include unconditionally
 * because the architecture guarantees them.
 *
 * At runtime, the platform layer (platform.c) probes what the CPU actually
 * supports so we can fall back when compile-time flags over- or under-promise.
 */
#if defined(__aarch64__) || defined(__arm__)
  #include <arm_neon.h>
  #define HAVE_NEON_INTRINSICS 1
  /* SVE headers are not universally available; only include if explicitly
   * requested via -DHAVE_SVE_INTRINSICS. */
  #if defined(HAVE_SVE_INTRINSICS)
    #include <arm_sve.h>
  #endif
#elif defined(__x86_64__) || defined(__i386__)
  /* Only enable SSE2 baseline by default — always available on x86_64. */
  #define HAVE_SSE2_INTRINSICS 1
  /* Higher intrinsics require explicit -march / -m flags.
   * The user opts in via CFLAGS="-msse4.2 -mavx -mavx2 -mfma". */
  #if defined(__SSE4_2__)
    #include <immintrin.h>
    #define HAVE_SSE42_INTRINSICS 1
  #endif
  #if defined(__AVX__) && defined(__SSE4_2__)
    /* AVX is enabled in addition to SSE4.2 */
    #define HAVE_AVX_INTRINSICS 1
  #endif
  #if defined(__AVX2__) && defined(__FMA__)
    /* Full AVX2 + FMA — the usual "modern x86" target. */
    #define HAVE_AVX2_FMA_INTRINSICS 1
  #endif
  #if defined(__AVX512F__)
    #define HAVE_AVX512_INTRINSICS 1
  #endif
#endif

#define NS_PER_SEC 1000000000ULL
#define KB (1024UL)
#define MB (1024UL * KB)
#define GB (1024UL * MB)

/* Output directory for all generated reports */
#define REPORTS_DIR "reports"

typedef struct {
    size_t l1d_size;
    size_t l1i_size;
    size_t l2_size;
    size_t l3_size;
    int detected;
} CacheConfig;

typedef struct {
    int memory_channels;
    int cpu_freq_mhz;
    char cpu_model[128];
    int detected;
    /* DRAM speed (in MT/s, e.g. 3200 for DDR4-3200) and standard. These are
     * read from sysfs / dmidecode / lscpu at runtime; 0 = unknown. Used to
     * compute theoretical bandwidth without hardcoding 4800 (DDR5-4800). */
    int dram_speed_mt_s;      /* e.g. 2400, 3200, 4800, 6400 */
    char dram_standard[32];   /* "DDR3", "DDR4", "DDR5", "LPDDR4", "LPDDR5", or "unknown" */
    int dram_channels;        /* Often == memory_channels; kept separate for clarity */
    /* Computed: bytes_per_sec_per_channel = dram_speed_mt_s * 8 (DDR transfers 8B/clock
     * per channel: 64-bit bus / 8 bits = 8 bytes per transfer, but transfers per
     * second = MT/s, so 8 bytes × MT/s = MB/s). 0 if undetermined. */
    double theoretical_bw_mbps;
} SystemConfig;

typedef struct {
    uint64_t min;
    uint64_t max;
    double avg;
    uint64_t p50;
    uint64_t p95;
    uint64_t p99;
} LatencyStats;

extern CacheConfig global_cache_config;
extern SystemConfig global_system_config;

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
/* Report Generation */
void report_free(ReportContext *ctx);
const char *report_get_filename(ReportContext *ctx);

void report_write_system_info(ReportContext *ctx);
void report_finalize_json(ReportContext *ctx);

/* Sudo credential query (lives in util.c) */
int is_sudo_available(void);
FILE *sudo_popen(const char *command);

/* Cache config functions */
int detect_cache_sizes(void);
void initialize_cache_config(void);
void print_system_info(void);

/* System config functions */
int detect_memory_channels(void);
int detect_cpu_freq(void);
void initialize_system_config(void);
int get_memory_channels(void);
int get_cpu_freq_mhz(void);
int request_sudo_password(void);
int pmu_init_cache_counters(void);
/* DRAM speed + theoretical bandwidth (computed at runtime, not hardcoded) */
void initialize_dram_speed(void);
int get_dram_speed_mt_s(void);
double get_theoretical_bw_mbps(void);

/* SIMD/Vectorization detection */
typedef struct {
    char simd_flags[256];
    int has_sse, has_sse2, has_sse3, has_ssse3, has_sse4_1, has_sse4_2;
    int has_avx, has_avx2, has_avx512;
    int has_neon, has_sve, has_sve2;
    int has_rvv;            /* RISC-V Vector extension (RVV) */
    int has_altivec;        /* PowerPC AltiVec / VMX */
    int has_vsx;            /* PowerPC Vector-Scalar Extension (VSX) */
    int vector_width;       /* Max vector width in bits (128 for NEON, 256 for AVX2, ...) */
} SIMDInfo;
const SIMDInfo* get_simd_info(void);

/* CPU Performance Counter types */
typedef struct {
    uint64_t cycles;
    uint64_t instructions;
    uint64_t branch_mispred;
    uint64_t mem_accesses;
    uint64_t l1d_misses;
    uint64_t l2_misses;
    double cpi;
    double ipc;
    double branch_mispred_rate;
} PerfResult;

typedef struct {
    int cycles_fd;
    int inst_retired_fd;
    int branch_mispred_fd;
    int mem_access_fd;
    int l1d_miss_fd;
    int l2_miss_fd;
} PerfCounters;

/* CPU Performance Counter functions */
int perf_init_counters(void);
int perf_measure(void (*func)(void*), void *arg, PerfResult *result);
void perf_print_result(const PerfResult *r, int show_pmu);

/* PMU-based cache latency measurement (lives in pmu.c, used by latency.c) */
uint64_t pmu_measure_cache_latency(void *ptr, int level);
/* PMU helper functions (lives in pmu.c, used by pmu_measure_cache_latency) */
void pmu_reset_counters(void);
void pmu_disable_counters(void);
uint64_t pmu_read_counter(int fd);

/* PMU cache counter state (read by pmu_measure_cache_latency) */
typedef struct {
    int l1d_refill_fd;
    int l1d_access_fd;
    int l2_refill_fd;
    int l2_access_fd;
    int l3_refill_fd;
    int l3_access_fd;
    int cycles_fd;
} PMUCacheCounters;
extern PMUCacheCounters pmu_cache_counters;


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
