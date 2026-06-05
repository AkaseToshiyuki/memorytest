/* SPDX-License-Identifier: MIT
 * platform.c - Cross-platform runtime detection
 *
 * Detects architecture / SIMD / cache / mem-channels / CPU-freq / PMU at
 * runtime so binaries can be compiled with portable -O2 (no -march=native)
 * and still use the best available SIMD on x86_64, arm64, riscv64, ppc64.
 *
 * Detection cascade per item:
 *   1. Cheap static probe (sysfs, /proc, CPUID cache leaves, ID_*_EL1, misa)
 *   2. Latency-based runtime probe (cache sizes)
 *   3. Interactive prompt (only on TTY)
 *   4. Safe default (always — never crashes, never blocks indefinitely)
 */
#define _GNU_SOURCE
#include "platform.h"
#include "common.h"
#include "util.h"   /* for isatty_safe, sudo_request_if_needed */

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <termios.h>
#include <unistd.h>

#ifdef __linux__
  #include <sys/sysinfo.h>
#endif

/* ============================================================
 * Internal state
 * ============================================================ */
static bool g_initialized = false;
static bool g_is_tty = false;
static bool g_non_interactive = false;
static bool g_no_sudo = false;

static PMUCapabilities g_pmu_caps = {0};
static bool g_pmu_probed = false;

/* Cached "I asked the user" hints so we don't ask twice. */
static int g_user_mem_channels __attribute__((unused)) = 0;
static int g_user_cpu_freq __attribute__((unused)) = 0;
static CacheConfig g_user_cache __attribute__((unused)) = {0, 0, 0, 0, 0};

/* ============================================================
 * TTY detection
 * ============================================================ */
bool platform_is_tty(void) {
    if (g_is_tty) return true;
    g_is_tty = isatty(STDIN_FILENO);
    return g_is_tty;
}

void platform_set_non_interactive(bool force) {
    g_non_interactive = force;
}

void platform_set_no_sudo(bool no_sudo) {
    g_no_sudo = no_sudo;
}

/* ============================================================
 * Small utility: parse an integer from a string, with bounds.
 * Returns -1 on parse failure.
 * ============================================================ */
static int parse_int(const char *s, int lo, int hi) {
    if (!s || !*s) return -1;
    char *end = NULL;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno || end == s || (end && *end && !isspace((unsigned char)*end))) return -1;
    if (v < lo || v > hi) return -1;
    return (int)v;
}

/* ============================================================
 * Interactive prompt — only ever called on TTY
 * Returns the parsed integer, or `default_value` on EOF / non-TTY.
 * ============================================================ */
int platform_prompt_int(const char *question, int default_value, int lo, int hi) {
    if (g_non_interactive || !platform_is_tty()) {
        return default_value;
    }
    char buf[128];
    fprintf(stderr, "%s [%d]: ", question, default_value);
    fflush(stderr);
    if (!fgets(buf, sizeof(buf), stdin)) {
        /* EOF — use default */
        fprintf(stderr, "\n");
        return default_value;
    }
    /* Strip trailing newline/whitespace */
    size_t n = strlen(buf);
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r' || isspace((unsigned char)buf[n-1]))) {
        buf[--n] = '\0';
    }
    if (n == 0) return default_value;
    int v = parse_int(buf, lo, hi);
    return (v > 0) ? v : default_value;
}

/* ============================================================
 * Architecture detection
 * ============================================================ */
Architecture platform_arch_detect(void) {
#if defined(__x86_64__) || defined(__i386__)
    current_arch = ARCH_X86_64;
#elif defined(__aarch64__) || defined(__arm__)
    current_arch = ARCH_ARM64;
#elif defined(__riscv) && (__riscv_xlen == 64)
    current_arch = ARCH_RISCV64;
#elif defined(__powerpc64__) || defined(__powerpc__)
    current_arch = ARCH_POWERPC64;
#else
    current_arch = ARCH_UNKNOWN;
#endif
    return current_arch;
}

/* ============================================================
 * SIMD capability detection (runtime)
 *
 * This is the critical bit. The previous compile-time version used
 * #if defined(__AVX2__) which would silently disable SIMD on
 * -O2 builds. We now query the CPU itself, so a -O2 build on
 * Zen 3 still discovers AVX2.
 *
 * We own a private copy of simd_info here; the simd.c module also
 * holds a file-static one for backward compatibility with get_simd_info().
 * They are kept in sync via the platform layer on first call.
 * ============================================================ */
static SIMDInfo platform_simd_info = {{0}};
static void simd_detect_x86(void) {
#if defined(__x86_64__) || defined(__i386__)
    /* CPUID leaf 1: SSE family + AVX */
    unsigned int eax = 0, ebx = 0, ecx = 0, edx = 0;
    __asm__ volatile("cpuid" : "=a"(eax), "=b"(ebx), "=c"(ecx), "=d"(edx) : "a"(1) : "memory");
    platform_simd_info.has_sse    = (edx >> 25) & 1;
    platform_simd_info.has_sse2   = (edx >> 26) & 1;
    platform_simd_info.has_sse3   = (ecx >>  0) & 1;
    platform_simd_info.has_ssse3  = (ecx >>  9) & 1;
    platform_simd_info.has_sse4_1 = (ecx >> 19) & 1;
    platform_simd_info.has_sse4_2 = (ecx >> 20) & 1;
    platform_simd_info.has_avx    = (ecx >> 28) & 1;

    /* CPUID leaf 7: AVX2 / AVX-512 / FMA / etc. */
    if (eax >= 7) {
        __asm__ volatile("cpuid" : "=a"(eax), "=b"(ebx), "=c"(ecx), "=d"(edx) : "a"(7), "c"(0) : "memory");
        platform_simd_info.has_avx2   = (ebx >>  5) & 1;
        platform_simd_info.has_avx512 = (ebx >> 16) & 1;
    }
#else
    /* Non-x86 host: this function is unreachable but the compiler still
     * type-checks it. Just return; the caller's arch switch prevents entry. */
    (void)platform_simd_info;
#endif
}

static void simd_detect_arm64(void) {
#if defined(__aarch64__)
    /* NEON is mandatory on AArch64 */
    platform_simd_info.has_neon = 1;
    /* Read ID_AA64PFR0_EL1 for SVE bits [19:16] and SVE2 [35:32] */
    uint64_t pfr0 = 0;
    __asm__ volatile("mrs %0, id_aa64pfr0_el1" : "=r"(pfr0));
    uint64_t sve_field = (pfr0 >> 32) & 0xF;   /* SVE: bits [35:32] */
    platform_simd_info.has_sve = (sve_field != 0);
    /* SVE2 detection is more involved (ID_AA64ZFR0_EL1) — skip for now,
     * consumers can treat has_sve as "some SVE is available" */
#else
    (void)platform_simd_info;
#endif
}

static void simd_detect_riscv64(void) {
#if defined(__riscv)
    uint64_t misa = 0;
    __asm__ volatile("csrr %0, misa" : "=r"(misa));
    platform_simd_info.has_rvv = (misa >> 12) & 1;
    if (platform_simd_info.has_rvv) {
        uint64_t vlenb = 0;
        __asm__ volatile("csrr %0, 0xC22" : "=r"(vlenb));
        platform_simd_info.vector_width = (int)(vlenb * 8);
    }
#endif
}

static void simd_detect_ppc64(void) {
#if defined(__powerpc__) || defined(__powerpc64__)
  #if defined(__linux__)
    if (__builtin_cpu_supports("vsx")) {
        platform_simd_info.has_vsx = 1;
        platform_simd_info.has_altivec = 1;
    } else if (__builtin_cpu_supports("altivec")) {
        platform_simd_info.has_altivec = 1;
    }
  #else
    /* On non-Linux, assume modern Power (Power6+ has AltiVec) */
    platform_simd_info.has_altivec = 1;
  #endif
#endif
}

static void simd_build_flags_string(void) {
    char *p = platform_simd_info.simd_flags;
    size_t left = sizeof(platform_simd_info.simd_flags);
    int n = 0;
    #define APPEND(flag) do {                                            \
        if (left > strlen(flag) + 1) {                                   \
            if (n > 0) { *p++ = ' '; left--; }                            \
            size_t l = strlen(flag);                                      \
            memcpy(p, flag, l); p += l; left -= l; n++;                  \
        }                                                                 \
    } while (0)
    if (platform_simd_info.has_sse)     APPEND("SSE");
    if (platform_simd_info.has_sse2)    APPEND("SSE2");
    if (platform_simd_info.has_sse3)    APPEND("SSE3");
    if (platform_simd_info.has_ssse3)   APPEND("SSSE3");
    if (platform_simd_info.has_sse4_1)  APPEND("SSE4.1");
    if (platform_simd_info.has_sse4_2)  APPEND("SSE4.2");
    if (platform_simd_info.has_avx)     APPEND("AVX");
    if (platform_simd_info.has_avx2)    APPEND("AVX2");
    if (platform_simd_info.has_avx512)  APPEND("AVX-512");
    if (platform_simd_info.has_neon)    APPEND("NEON");
    if (platform_simd_info.has_sve)     APPEND("SVE");
    if (platform_simd_info.has_rvv)     APPEND("RVV");
    if (platform_simd_info.has_altivec) APPEND("AltiVec");
    if (platform_simd_info.has_vsx)     APPEND("VSX");
    if (n == 0) APPEND("Unknown");
    #undef APPEND
    *p = '\0';
}

static void simd_compute_width(void) {
    if (platform_simd_info.vector_width == 0) {
        if (platform_simd_info.has_avx512) platform_simd_info.vector_width = 512;
        else if (platform_simd_info.has_avx2) platform_simd_info.vector_width = 256;
        else if (platform_simd_info.has_avx)  platform_simd_info.vector_width = 256;
        else if (platform_simd_info.has_sse4_2 || platform_simd_info.has_sse2 || platform_simd_info.has_sse) platform_simd_info.vector_width = 128;
        else if (platform_simd_info.has_neon || platform_simd_info.has_sve) platform_simd_info.vector_width = 128;
        else if (platform_simd_info.has_altivec || platform_simd_info.has_vsx) platform_simd_info.vector_width = 128;
        else if (platform_simd_info.has_rvv) {
            /* vector_width was already set by simd_detect_riscv64 */
            if (platform_simd_info.vector_width == 0) platform_simd_info.vector_width = 128;
        } else {
            platform_simd_info.vector_width = 64;  /* baseline */
        }
    }
}

const SIMDInfo *platform_simd_detect(void) {
    if (platform_simd_info.simd_flags[0] != '\0') return &platform_simd_info;
    memset(&platform_simd_info, 0, sizeof(platform_simd_info));
    switch (current_arch) {
        case ARCH_X86_64:    simd_detect_x86();    break;
        case ARCH_ARM64:     simd_detect_arm64();  break;
        case ARCH_RISCV64:   simd_detect_riscv64();break;
        case ARCH_POWERPC64: simd_detect_ppc64();  break;
        case ARCH_UNKNOWN:   break;
    }
    simd_compute_width();
    simd_build_flags_string();
    return &platform_simd_info;
}

/* Predicates */
bool platform_has_sse2(void)   { platform_simd_detect(); return platform_simd_info.has_sse2  ? true : false; }
bool platform_has_avx(void)    { platform_simd_detect(); return platform_simd_info.has_avx   ? true : false; }
bool platform_has_avx2(void)   { platform_simd_detect(); return platform_simd_info.has_avx2  ? true : false; }
bool platform_has_avx512(void) { platform_simd_detect(); return platform_simd_info.has_avx512? true : false; }
bool platform_has_fma(void)    { platform_simd_detect(); return (platform_simd_info.has_avx2 && platform_simd_info.has_avx) ? true : false; }
bool platform_has_neon(void)   { platform_simd_detect(); return platform_simd_info.has_neon  ? true : false; }
bool platform_has_sve(void)    { platform_simd_detect(); return platform_simd_info.has_sve   ? true : false; }
bool platform_has_rvv(void)    { platform_simd_detect(); return platform_simd_info.has_rvv   ? true : false; }
bool platform_has_altivec(void){ platform_simd_detect(); return platform_simd_info.has_altivec ? true : false; }
bool platform_has_vsx(void)    { platform_simd_detect(); return platform_simd_info.has_vsx   ? true : false; }
int  platform_vector_width(void){ platform_simd_detect(); return platform_simd_info.vector_width; }

/* ============================================================
 * Cache detection
 * ============================================================ */

/* Try sysfs on Linux. Returns 0 on success, -1 on failure. */
static int cache_detect_sysfs(CacheConfig *cfg) {
#ifdef __linux__
    /* Helper: read one int from a sysfs file */
    #define READ_SYS_INT(path, out) do {                                  \
        FILE *f = fopen(path, "r");                                       \
        if (f) { if (fscanf(f, "%zu", out) == 1) { fclose(f); } else { fclose(f); return -1; } } \
        else return -1;                                                   \
    } while (0)

    /* Per-CPU L1d / L1i / L2 / L3 sizes: aggregate across all CPUs.
     * Different CPUs can have different cache sizes on heterogeneous systems.
     * We take the *first* (most likely the boot CPU). */
    const char *base = "/sys/devices/system/cpu/cpu0/cache";
    char path[256];
    size_t sz;

    /* index0 = L1d, index1 = L1i, index2 = L2, index3 = L3 (typical) */
    /* Use the type field to be robust to ordering */
    for (int idx = 0; idx < 8; idx++) {
        snprintf(path, sizeof(path), "%s/index%d/type", base, idx);
        FILE *f = fopen(path, "r");
        if (!f) continue;
        char type[16] = {0};
        if (fscanf(f, "%15s", type) != 1) { fclose(f); continue; }
        fclose(f);

        snprintf(path, sizeof(path), "%s/index%d/size", base, idx);
        FILE *f2 = fopen(path, "r");
        if (!f2) continue;
        if (fscanf(f2, "%zu", &sz) != 1) { fclose(f2); continue; }
        fclose(f2);
        /* Sizes in sysfs are in KB */
        sz *= 1024;

        if      (strcmp(type, "Data")        == 0 && cfg->l1d_size == 0) cfg->l1d_size = sz;
        else if (strcmp(type, "Instruction") == 0 && cfg->l1i_size == 0) cfg->l1i_size = sz;
        else if (strcmp(type, "Unified")     == 0 && cfg->l2_size  == 0) cfg->l2_size  = sz;
        /* L3 will appear as Unified too, but later */
    }
    /* Second pass for L3 (first Unified we found was L2) */
    int found_unified = 0;
    for (int idx = 0; idx < 8; idx++) {
        snprintf(path, sizeof(path), "%s/index%d/type", base, idx);
        FILE *f = fopen(path, "r");
        if (!f) continue;
        char type[16] = {0};
        if (fscanf(f, "%15s", type) != 1) { fclose(f); continue; }
        fclose(f);
        if (strcmp(type, "Unified") != 0) continue;
        found_unified++;
        if (found_unified == 1 && cfg->l2_size == 0) continue;  /* L2 */
        if (cfg->l3_size == 0) {
            snprintf(path, sizeof(path), "%s/index%d/size", base, idx);
            FILE *f2 = fopen(path, "r");
            if (!f2) continue;
            if (fscanf(f2, "%zu", &sz) == 1) {
                cfg->l3_size = sz * 1024;
            }
            fclose(f2);
            break;
        }
    }

    if (cfg->l1d_size > 0 && cfg->l2_size > 0) {
        cfg->detected = 1;
        return 0;
    }
    #undef READ_SYS_INT
#endif
    return -1;
}

/* Latency-binned probe. Binary search for the L1d boundary.
 * Less accurate than sysfs but works anywhere with a memory allocator. */
static size_t cache_probe_latency(size_t start, size_t max_size) {
    /* Skip the implementation for now — sysfs is reliable on Linux.
     * On non-Linux, return 0 to signal "could not detect". */
    (void)start; (void)max_size;
    return 0;
}

int platform_cache_detect(CacheConfig *cfg) {
    if (cfg->detected) return 0;

    /* Method 1: sysfs (Linux, very reliable) */
    if (cache_detect_sysfs(cfg) == 0) {
        return 0;
    }

    /* Method 2: latency probe (cross-platform, slow) */
    if (cfg->l1d_size == 0) cfg->l1d_size = cache_probe_latency(1024, 256 * 1024);
    if (cfg->l1d_size == 0) {
        if (platform_is_tty() && !g_non_interactive) {
            fprintf(stderr, "[Cache] Auto-detection failed. Using defaults unless specified.\n");
        }
        int kb = platform_prompt_int("  L1d cache size in KB", 0, 0, 1024);
        cfg->l1d_size = (size_t)kb * 1024;
    }
    if (cfg->l2_size == 0) {
        int kb = platform_prompt_int("  L2 cache size in KB", 0, 0, 16384);
        cfg->l2_size = (size_t)kb * 1024;
    }
    if (cfg->l3_size == 0) {
        /* L3 may not exist (some ARM SoCs don't have one) */
        int kb = platform_prompt_int("  L3 cache size in KB (0 if none)", 0, 0, 1024 * 1024);
        cfg->l3_size = (size_t)kb * 1024;
    }
    return -1;
}

/* ============================================================
 * Memory channel detection
 * ============================================================ */
int platform_mem_channels_detect(int user_known) {
    if (user_known > 0) return user_known;

#ifdef __linux__
    /* Method 1: count populated DIMMs from /sys/devices/system/memory/
     * This works on most server hardware. */
    {
        DIR *d = opendir("/sys/devices/system/memory");
        if (d) {
            struct dirent *de;
            int populated = 0;
            while ((de = readdir(d)) != NULL) {
                if (strncmp(de->d_name, "memory", 6) == 0) {
                    char path[512];
                    snprintf(path, sizeof(path),
                             "/sys/devices/system/memory/%s/online",
                             de->d_name);
                    FILE *f = fopen(path, "r");
                    if (f) {
                        int v = 0;
                        if (fscanf(f, "%d", &v) == 1 && v == 1) {
                            populated++;
                        }
                        fclose(f);
                    }
                }
            }
            closedir(d);
            if (populated > 0) return populated;
        }
    }

    /* Method 2: NUMA node count (rough — usually >= channels) */
    {
        DIR *d = opendir("/sys/devices/system/node");
        if (d) {
            struct dirent *de;
            int nodes = 0;
            while ((de = readdir(d)) != NULL) {
                if (strncmp(de->d_name, "node", 4) == 0 && isdigit((unsigned char)de->d_name[4])) {
                    nodes++;
                }
            }
            closedir(d);
            if (nodes > 0) return nodes;
        }
    }
#endif

    /* Method 3: ask user (TTY) or use default (non-TTY).
     * Non-TTY default is 0 (unknown), NOT a guessed number. */
    fprintf(stderr, "[Mem] Auto-detection of memory channels failed.\n");
    return platform_prompt_int("  Memory channels", 0, 0, 16);
}

/* ============================================================
 * CPU frequency detection
 * ============================================================ */
static int freq_read_sysfs(void) {
#ifdef __linux__
    FILE *f = fopen("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "r");
    if (!f) {
        /* Some systems (e.g. Apple M-series, Xeon with hardware P-state) don't
         * have cpufreq. Try the scaling driver fallback. */
        f = fopen("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq", "r");
    }
    if (f) {
        long khz = 0;
        if (fscanf(f, "%ld", &khz) == 1 && khz > 0) {
            fclose(f);
            return (int)(khz / 1000);
        }
        fclose(f);
    }
#endif
    return 0;
}

static int freq_read_cpuinfo(void) {
#ifdef __linux__
    FILE *f = fopen("/proc/cpuinfo", "r");
    if (!f) return 0;
    char line[512];
    int mhz = 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "cpu MHz", 7) == 0) {
            char *p = strchr(line, ':');
            if (p && sscanf(p + 1, "%d", &mhz) == 1 && mhz > 0) {
                fclose(f);
                return mhz;
            }
        }
    }
    fclose(f);
#endif
    return 0;
}

static int freq_read_lscpu(void) {
#ifdef __linux__
    FILE *f = popen("lscpu 2>/dev/null | awk '/^CPU MHz:/ {print $3; exit}'", "r");
    if (!f) return 0;
    float mhz = 0;
    int got = fscanf(f, "%f", &mhz);
    pclose(f);
    if (got == 1 && mhz > 0) return (int)mhz;

    /* Try "Model name:" line for max frequency on Intel */
    f = popen("lscpu 2>/dev/null | awk -F'@ *' '/^Model name:/ {print $2; exit}'", "r");
    if (!f) return 0;
    char buf[64] = {0};
    if (fgets(buf, sizeof(buf), f)) {
        float ghz = 0;
        if (sscanf(buf, "%fGHz", &ghz) == 1 && ghz > 0) {
            pclose(f);
            return (int)(ghz * 1000);
        }
    }
    pclose(f);
#endif
    return 0;
}

int platform_cpu_freq_detect(int user_known) {
    if (user_known > 0) return user_known;

    int mhz = freq_read_sysfs();
    if (mhz > 0) return mhz;
    mhz = freq_read_cpuinfo();
    if (mhz > 0) return mhz;
    mhz = freq_read_lscpu();
    if (mhz > 0) return mhz;

    fprintf(stderr, "[CPU] Auto-detection of CPU frequency failed.\n");
    /* Non-TTY default is 0 (unknown). Do NOT guess a number like 2000. */
    return platform_prompt_int("  CPU frequency in MHz", 0, 0, 10000);
}

/* ============================================================
 * PMU probe — actually try to open a perf_event fd
 * ============================================================ */
const PMUCapabilities *platform_pmu_probe(void) {
    if (g_pmu_probed) return &g_pmu_caps;
    g_pmu_probed = true;

    /* Read paranoia level first */
#ifdef __linux__
    FILE *f = fopen("/proc/sys/kernel/perf_event_paranoid", "r");
    if (f) {
        int p = 4;
        if (fscanf(f, "%d", &p) == 1) g_pmu_caps.paranoia = p;
        fclose(f);
    } else {
        g_pmu_caps.paranoia = 4;  /* safest default */
    }
#else
    g_pmu_caps.paranoia = 4;
#endif

    /* Try opening a software counter (always works) */
#ifdef __linux__
    #include <linux/perf_event.h>
    #include <sys/syscall.h>
    #include <sys/ioctl.h>

    static struct perf_event_attr pe;
    memset(&pe, 0, sizeof(pe));
    pe.type = PERF_TYPE_SOFTWARE;
    pe.size = sizeof(pe);
    pe.config = PERF_COUNT_SW_CPU_CLOCK;
    pe.disabled = 1;

    long fd = syscall(SYS_perf_event_open, &pe, 0, -1, -1, 0);
    if (fd >= 0) {
        g_pmu_caps.can_profile_self = true;
        close((int)fd);
    }

    /* Try opening a hardware counter (depends on paranoia + caps) */
    memset(&pe, 0, sizeof(pe));
    pe.type = PERF_TYPE_HARDWARE;
    pe.size = sizeof(pe);
    pe.config = PERF_COUNT_HW_CPU_CYCLES;
    pe.disabled = 1;

    fd = syscall(SYS_perf_event_open, &pe, 0, -1, -1, 0);
    if (fd >= 0) {
        g_pmu_caps.can_profile_hw = true;
        g_pmu_caps.available = true;
        close((int)fd);
    } else {
        /* errno: EACCES = paranoia too high; ENOENT = no PMU */
        g_pmu_caps.can_profile_hw = false;
        g_pmu_caps.available = g_pmu_caps.can_profile_self;
    }
#else
    g_pmu_caps.can_profile_self = false;
    g_pmu_caps.available = false;
#endif

    return &g_pmu_caps;
}

/* ============================================================
 * platform_init / platform_shutdown
 * ============================================================ */
void platform_init(void) {
    if (g_initialized) return;
    g_initialized = true;
    platform_arch_detect();
    platform_simd_detect();
    platform_is_tty();  /* cache the result */
    platform_pmu_probe();

    /* If we're on a TTY, eagerly offer the user a chance to enter a sudo
     * password for hardware detection. On non-TTY (CI, smoke tests) this
     * is a no-op — the user can re-run with `make test-interactive` or
     * `sudo -E make test` to get sudo-based probes.
     *
     * The reason we prompt *once* at init, rather than lazily on the
     * first sudo probe, is so the user sees the prompt BEFORE the
     * benchmark starts grinding. The 90-second ALU run is a long time
     * to wait for a question that could have been answered up-front. */
    if (platform_is_tty() && !g_non_interactive && !g_no_sudo) {
        extern int request_sudo_password(void);
        /* Non-blocking: request_sudo_password prints the prompt and
         * waits for input. It's a no-op on non-TTY (returns -1). */
        (void)request_sudo_password();
    }
}

void platform_shutdown(void) {
    /* Nothing to free right now */
    g_initialized = false;
}
