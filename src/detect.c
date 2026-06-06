/* SPDX-License-Identifier: MIT
 * detect.c - Hardware detection: cache / arch / memory channels / CPU freq / CPU model / NUMA
 *
 * Split from monolithic common.c (2026-06-02).
 */
#include "common.h"
#include "platform.h"
#include "util.h"
#include "stddef.h"
#include <stdarg.h>
#include <string.h>
#include <signal.h>
#include <setjmp.h>
#include <sys/wait.h>
#include <dirent.h>
#include <libgen.h>

/* ========== ARM Cache Detection ========== */
#ifdef __aarch64__
static sigjmp_buf sigill_jmp;
static volatile sig_atomic_t sigill_occurred;

static void sigill_handler(int sig) {
    sigill_occurred = 1;
    siglongjmp(sigill_jmp, 1);
}

static int try_read_ctr_el0(uint64_t *val) {
    signal(SIGILL, sigill_handler);
    sigill_occurred = 0;
    if (sigsetjmp(sigill_jmp, 1) == 0) {
        __asm__ volatile("mrs %0, ctr_el0" : "=r"(*val));
        signal(SIGILL, SIG_DFL);
        return 0;
    }
    signal(SIGILL, SIG_DFL);
    return -1;
}

static int try_read_clidr_el1(uint64_t *val) {
    signal(SIGILL, sigill_handler);
    sigill_occurred = 0;
    if (sigsetjmp(sigill_jmp, 1) == 0) {
        __asm__ volatile("mrs %0, clidr_el1" : "=r"(*val));
        signal(SIGILL, SIG_DFL);
        return 0;
    }
    signal(SIGILL, SIG_DFL);
    return -1;
}

static size_t get_arm_cache_line_size(void) {
    uint64_t ctr;
    if (try_read_ctr_el0(&ctr) != 0) return 64;
    uint32_t cwg = (ctr >> 0) & 0xF;
    uint32_t min_line = (ctr >> 16) & 0xF;
    if (min_line > 0) {
        return 4 << min_line;
    }
    return 4 << cwg;
}

static int get_arm_cache_levels(void) {
    uint64_t clidr;
    if (try_read_clidr_el1(&clidr) != 0) return 0;
    int loc = (clidr >> 24) & 0x7;
    int louis = (clidr >> 27) & 0x7;
    return (loc > louis) ? loc : louis;
}

/* Probe actual cache size by detecting when latency increases (cache miss) */
static size_t probe_cache_size_by_latency(size_t start_size, size_t max_size) {
    if (max_size < start_size) max_size = start_size;

    void *ptr = malloc(max_size * 2);
    if (!ptr) return 0;

    memset(ptr, 0, max_size * 2);
    volatile uint64_t *p = (volatile uint64_t *)ptr;

    /* Measure baseline sequential latency with small access */
    uint64_t baseline = 0;
    for (size_t trial = 0; trial < 3; trial++) {
        uint64_t start = get_time_ns();
        for (size_t i = 0; i < 1000; i++) {
            baseline += p[i];
        }
        uint64_t end = get_time_ns();
        baseline = (end - start) / 1000;
        if (baseline > 0) break;
    }

    /* Binary search for cache boundary */
    size_t low = start_size / 4;
    size_t high = max_size;
    size_t cache_size = 0;

    while (low <= high && cache_size == 0) {
        size_t mid = (low + high) / 2;
        if (mid < start_size) mid = start_size;

        /* Clear and measure random access latency for this size */
        memset((void*)ptr, 0, mid);
        uint64_t sum = 0;
        uint64_t start = get_time_ns();
        for (size_t i = 0; i < 1000; i++) {
            size_t idx = (i * 17) % (mid / sizeof(uint64_t));
            sum += p[idx];
        }
        uint64_t end = get_time_ns();
        uint64_t lat = (end - start) / 1000;

        /* If latency is >2x baseline, we're hitting cache */
        if (lat > baseline * 2) {
            cache_size = mid;
            high = mid - 1;
        } else {
            low = mid + 1;
        }
    }

    free(ptr);
    return cache_size > 0 ? cache_size : 0;
}

static int detect_arm_cache_registers(void) {
    int levels = get_arm_cache_levels();
    int line_size = get_arm_cache_line_size();

    if (levels == 0) {
        printf("[ARM] Cannot read cache registers\n");
        return -1;
    }

    printf("[ARM] Detected %d cache levels, line size: %d bytes\n",
           levels, line_size);

    size_t l1d = 0, l1i = 0, l2 = 0, l3 = 0;

    /* Probe ranges are now adaptive: derived from the machine's physical
     * memory. This avoids hardcoding limits that would miss L3=256MB on
     * EPYC server CPUs or L1d=192KB on Apple M-series. */
    size_t total_mem = (size_t)sysconf(_SC_PHYS_PAGES) * (size_t)sysconf(_SC_PAGESIZE);
    if (total_mem == 0) total_mem = 4 * GB;  /* conservative fallback if sysconf fails */

    /* L1d range: 4KB → 1MB. Most CPUs have L1d 16-128KB but Apple M1 is 192KB,
     * Ampere Altra is 64KB, IBM POWER10 is 128KB. 1MB is a safe upper bound. */
    size_t l1_max = 1 * MB;
    /* L2 range: 64KB → 32MB. Covers Cortex-A78 (512KB) → EPYC L2 (1MB) → POWER10 L2 (2MB). */
    size_t l2_max = 32 * MB;
    /* L3 range: 1MB → min(64MB, total_mem/16). EPYC L3 is 256MB so we need a
     * larger upper bound. We use total_mem/8 to allow big L3 detection on
     * machines with lots of RAM, but cap at 2GB to keep probe time bounded. */
    size_t l3_max = total_mem / 8;
    if (l3_max < 64 * MB) l3_max = 64 * MB;
    if (l3_max > 2 * GB) l3_max = 2 * GB;

    switch (levels) {
        case 3:
            /* Probe L3: start at 1MB, max adaptive (up to 2GB) */
            l3 = probe_cache_size_by_latency(1 * MB, l3_max);
            if (l3 == 0) {
                printf("[ARM] L3 detection failed (range 1M-%zuM), will prompt user\n", l3_max/MB);
            }
        case 2:
            /* Probe L2: start at 64KB, max 32MB */
            l2 = probe_cache_size_by_latency(64 * KB, l2_max);
            if (l2 == 0) {
                printf("[ARM] L2 detection failed (range 64K-%zuK), will prompt user\n", l2_max/KB);
            }
        case 1:
            /* Probe L1: start at 4KB, max 1MB */
            l1d = probe_cache_size_by_latency(4 * KB, l1_max);
            l1i = l1d;
            if (l1d == 0) {
                printf("[ARM] L1 detection failed, will prompt user\n");
            }
            break;
        default:
            return -1;
    }

    if (l1d == 0) {
        /* L1 is essential, if we can't detect it, fail */
        printf("[ARM] Cannot detect L1 cache\n");
        return -1;
    }

    global_cache_config.l1d_size = l1d;
    global_cache_config.l1i_size = (l1i > 0) ? l1i : l1d;
    global_cache_config.l2_size = l2;  /* 0 if not detected, user will be prompted */
    global_cache_config.l3_size = l3;  /* 0 if not detected, user will be prompted */
    global_cache_config.l3_total_size = l3;  /* ARM: unified L3, no chiplet designs */
    global_cache_config.l3_ccd_count = (l3 > 0) ? 1 : 0;
    global_cache_config.detected = 1;

    return 0;
}
#endif

/* ========== x86 Cache Detection ========== */
#ifdef __x86_64__
static void cpuid(uint32_t leaf, uint32_t subleaf, uint32_t *eax, uint32_t *ebx, uint32_t *ecx, uint32_t *edx) {
    __asm__ volatile("cpuid"
        : "=a"(*eax), "=b"(*ebx), "=c"(*ecx), "=d"(*edx)
        : "a"(leaf), "c"(subleaf));
}

static int detect_x86_cache_cpuid(void) {
    uint32_t eax, ebx, ecx, edx;
    size_t l1d = 0, l1i = 0, l2 = 0, l3 = 0;

    for (int i = 0; i < 10; i++) {
        cpuid(4, i, &eax, &ebx, &ecx, &edx);

        int cache_type = eax & 0x1F;
        if (cache_type == 0) break;

        int level = (eax >> 5) & 0x7;
        int ways = ((ebx >> 22) & 0x3FF) + 1;
        int partitions = ((ebx >> 12) & 0x3FF) + 1;
        int line_size = (ebx & 0xFFF) + 1;
        int sets = ecx + 1;

        uint64_t cache_size = (uint64_t)ways * partitions * line_size * sets;

        if (level == 1 && (eax & 0x20)) l1d = cache_size;
        else if (level == 1 && (eax & 0x10)) l1i = cache_size;
        else if (level == 2) l2 = cache_size;
        else if (level == 3) l3 = cache_size;
    }

    if (l1d > 0) {
        global_cache_config.l1d_size = l1d;
        global_cache_config.l1i_size = (l1i > 0) ? l1i : l1d;
        global_cache_config.l2_size = l2;
        global_cache_config.l3_size = l3;
        /* CPUID reports total per-package L3 (unified for that package).
         * On multi-socket systems, CPUID leaf 4 only reports the local
         * package's L3; total system L3 is still just the per-package
         * value for latency purposes (a core can't hit remote L3). */
        global_cache_config.l3_total_size = l3;
        global_cache_config.l3_ccd_count = (l3 > 0) ? 1 : 0;
        global_cache_config.detected = 1;
        return 0;
    }

    return -1;
}
#endif

/* ========== sysfs Cache Detection ========== */
static size_t read_sysfs_cache_size(int index) {
    char path[256];
    char line[64];

    snprintf(path, sizeof(path),
             "/sys/devices/system/cpu/cpu0/cache/index%d/size", index);

    FILE *f = fopen(path, "r");
    if (f) {
        if (fgets(line, sizeof(line), f)) {
            char *end;
            unsigned long val = strtoul(line, &end, 10);
            if (end && (*end == 'K' || *end == 'k')) {
                fclose(f);
                return val * 1024;
            } else if (end && (*end == 'M' || *end == 'm')) {
                fclose(f);
                return val * 1024 * 1024;
            }
        }
        fclose(f);
    }

    return 0;
}

static int read_sysfs_cache_level(int index) {
    char path[256];
    char line[16];

    snprintf(path, sizeof(path),
             "/sys/devices/system/cpu/cpu0/cache/index%d/level", index);

    FILE *f = fopen(path, "r");
    if (f) {
        if (fgets(line, sizeof(line), f)) {
            int level = atoi(line);
            fclose(f);
            return level;
        }
        fclose(f);
    }

    return 0;
}

static const char* read_sysfs_cache_type(int index) {
    static char type[32];
    char path[256];

    snprintf(path, sizeof(path),
             "/sys/devices/system/cpu/cpu0/cache/index%d/type", index);

    FILE *f = fopen(path, "r");
    if (f) {
        if (fgets(type, sizeof(type), f)) {
            type[strcspn(type, "\n")] = 0;
            fclose(f);
            return type;
        }
        fclose(f);
    }

    return "";
}

/* Count set bits in a comma-separated hex CPU map (e.g. "00000000,000000ff").
 * Used to parse the shared_cpu_map file from sysfs cpu cache index entries. */
static int count_cpus_in_shared_map(const char *map_str) {
    int count = 0;
    const char *p = map_str;
    while (*p) {
        if (*p == ',' || *p == '\n' || *p == ' ' || *p == '\r') {
            p++;
            continue;
        }
        char *end;
        unsigned long chunk = strtoul(p, &end, 16);
        if (end == p) break;
        count += __builtin_popcountl(chunk);
        p = end;
    }
    return count;
}

/* Determine number of L3 domains (CCDs on chiplet designs) by reading
 * shared_cpu_map for cpu0's L3. Each unique shared_cpu_map == one L3 domain.
 * Returns the number of domains, or 1 if unable to determine. */
static int detect_l3_domain_count(size_t per_ccd_l3) {
    char map_path[256];
    char map[256];

    /* Read shared_cpu_map for cpu0's L3 */
    snprintf(map_path, sizeof(map_path),
             "/sys/devices/system/cpu/cpu0/cache/index3/shared_cpu_map");
    FILE *f = fopen(map_path, "r");
    if (!f) {
        /* Try index2 (some systems report L3 at index2) */
        for (int idx = 0; idx < 10; idx++) {
            if (read_sysfs_cache_level(idx) == 3) {
                snprintf(map_path, sizeof(map_path),
                         "/sys/devices/system/cpu/cpu0/cache/index%d/shared_cpu_map", idx);
                f = fopen(map_path, "r");
                if (f) break;
            }
        }
    }
    if (!f) return 1;  /* can't read, assume unified */

    if (!fgets(map, sizeof(map), f)) {
        fclose(f);
        return 1;
    }
    fclose(f);

    int cpus_per_domain = count_cpus_in_shared_map(map);
    if (cpus_per_domain <= 0) return 1;

    long total_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    if (total_cpus <= 0) return 1;

    /* cpus_per_domain counts logical CPUs (with HT/SMT). total_cpus is also
     * logical. The division gives the number of L3 domains (CCDs on EPYC). */
    int domains = (int)(total_cpus / cpus_per_domain);
    if (domains < 1) domains = 1;

    /* Cross-validate: check that cpuN (first CPU of the last domain) reports
     * the same per-CCD L3 size. If not, L3 is truly unified. */
    if (domains > 1 && per_ccd_l3 > 0) {
        /* Find a CPU likely in a different domain */
        int remote_cpu = cpus_per_domain * (domains - 1);
        if (remote_cpu >= total_cpus) remote_cpu = (int)(total_cpus - 1);

        /* Read L3 size for that remote CPU */
        size_t remote_l3 = 0;
        for (int idx = 0; idx < 10; idx++) {
            int level = 0;
            char lvl_path[256];
            snprintf(lvl_path, sizeof(lvl_path),
                     "/sys/devices/system/cpu/cpu%d/cache/index%d/level", remote_cpu, idx);
            f = fopen(lvl_path, "r");
            if (f) {
                char buf[16];
                if (fgets(buf, sizeof(buf), f)) level = atoi(buf);
                fclose(f);
            }
            if (level == 3) {
                char sz_path[256];
                snprintf(sz_path, sizeof(sz_path),
                         "/sys/devices/system/cpu/cpu%d/cache/index%d/size", remote_cpu, idx);
                f = fopen(sz_path, "r");
                if (f) {
                    char buf[64];
                    if (fgets(buf, sizeof(buf), f)) {
                        char *end;
                        unsigned long val = strtoul(buf, &end, 10);
                        if (end) {
                            if (*end == 'K' || *end == 'k') remote_l3 = val * 1024;
                            else if (*end == 'M' || *end == 'm') remote_l3 = val * 1024 * 1024;
                        }
                    }
                    fclose(f);
                }
                break;
            }
        }

        /* If remote L3 is the same size as local, each domain has its own L3.
         * If different or 0, L3 might be fully shared (unified). */
        if (remote_l3 == 0 || remote_l3 != per_ccd_l3) {
            return 1;  /* unified or uncertain */
        }
    }

    return domains;
}

static int detect_cache_via_sysfs(void) {
    size_t l1d = 0, l1i = 0, l2 = 0, l3 = 0;

    for (int i = 0; i < 10; i++) {
        size_t size = read_sysfs_cache_size(i);
        if (size == 0) break;

        int level = read_sysfs_cache_level(i);
        const char *type = read_sysfs_cache_type(i);

        if (level == 1) {
            if (strcmp(type, "Data") == 0) l1d = size;
            else if (strcmp(type, "Instruction") == 0) l1i = size;
            else if (strcmp(type, "Unified") == 0) {
                if (l1d == 0) l1d = size;
                l1i = size;
            }
        } else if (level == 2) {
            l2 = size;
        } else if (level == 3) {
            l3 = size;
        }
    }

    if (l1d > 0) {
        global_cache_config.l1d_size = l1d;
        global_cache_config.l1i_size = (l1i > 0) ? l1i : l1d;
        global_cache_config.l2_size = l2;  /* 0 if not detected */
        global_cache_config.l3_size = l3;  /* 0 if not detected */

        /* Detect total L3 across CCDs on multi-chiplet designs (e.g. AMD EPYC).
         * On unified-L3 systems, total = per-CCD = l3. On multi-CCD systems
         * like EPYC where each CCD has its own L3, total = l3 × num_ccds. */
        if (l3 > 0) {
            int ccd_count = detect_l3_domain_count(l3);
            global_cache_config.l3_ccd_count = ccd_count;
            global_cache_config.l3_total_size = l3 * (size_t)ccd_count;
        } else {
            global_cache_config.l3_ccd_count = 0;
            global_cache_config.l3_total_size = 0;
        }

        global_cache_config.detected = 1;

        if (l3 > 0 && global_cache_config.l3_ccd_count > 1) {
            printf("[sysfs] L1D: %zu KB, L1I: %zu KB, L2: %zu KB, "
                   "L3: %zu KB per CCD × %d CCDs = %zu KB total\n",
                   l1d / 1024, global_cache_config.l1i_size / 1024,
                   l2 / 1024, l3 / 1024,
                   global_cache_config.l3_ccd_count,
                   global_cache_config.l3_total_size / 1024);
        } else {
            printf("[sysfs] L1D: %zu KB, L1I: %zu KB, L2: %zu KB, L3: %zu KB\n",
                   l1d / 1024, global_cache_config.l1i_size / 1024,
                   l2 / 1024, l3 / 1024);
        }
        if (l2 == 0) printf("[sysfs] L2 not detected, will prompt user\n");
        if (l3 == 0) printf("[sysfs] L3 not detected, will prompt user\n");
        return 0;
    }

    return -1;
}

/* ========== Cache Configuration ========== */
int detect_cache_sizes(void) {
    /* Priority: sysfs -> ARM registers -> x86 CPUID -> fail */

    if (detect_cache_via_sysfs() == 0) {
        return 1;
    }

#ifdef __aarch64__
    if (detect_arm_cache_registers() == 0) {
        return 1;
    }
#endif

#ifdef __x86_64__
    if (detect_x86_cache_cpuid() == 0) {
        return 1;
    }
#endif

    return 0;
}

static void prompt_cache_config_from_user(void) {
    /* Delegate to the platform layer. The platform layer:
     *   - on TTY: prompts with sensible defaults
     *   - on non-TTY: returns defaults immediately (no blocking) */

    /* Try cross-process cache first — avoid re-prompting across binaries. */
    if (hw_cache_load() > 0) {
        /* Cache restored at least some keys. Check if all needed
         * fields are now non-zero. */
        if (global_cache_config.l1d_size > 0 &&
            global_cache_config.l2_size > 0 &&
            global_cache_config.l3_size > 0) {
            global_cache_config.detected = 1;
            fprintf(stderr, "[Cache] Loaded from HW cache (L1D=%zuKB, L2=%zuKB, L3=%zuKB)\n",
                    global_cache_config.l1d_size / 1024,
                    global_cache_config.l2_size / 1024,
                    global_cache_config.l3_size / 1024);
            return;
        }
        /* Partial load: continue to prompt for missing values. */
    }

    fprintf(stderr, "\n[Cache] Some cache levels were not auto-detected.\n");

    /* Non-TTY fallback: use conservative but reasonable defaults. These
     * are only a last resort — sysfs detection runs first and is unaffected.
     * L1D=64KB matches most modern CPUs (Intel/AMD Zen, ARM Cortex-A7x).
     * L2=512KB and L3=2MB are common mid-range values. */
    if (!platform_is_tty()) {
        fprintf(stderr, "[Cache] WARNING: non-TTY environment, using guessed defaults.\n"
                        "              L1D=64KB, L2=512KB, L3=2MB.\n"
                        "              Run interactively for accurate detection.\n");
        if (global_cache_config.l1d_size == 0)
            global_cache_config.l1d_size = 64 * 1024;
        if (global_cache_config.l2_size == 0)
            global_cache_config.l2_size = 512 * 1024;
        if (global_cache_config.l3_size == 0) {
            global_cache_config.l3_size = 2 * 1024 * 1024;
            global_cache_config.l3_total_size = 2 * 1024 * 1024;
            global_cache_config.l3_ccd_count = 1;
        }
        global_cache_config.l1i_size = global_cache_config.l1d_size;
        global_cache_config.detected = 1;
        hw_cache_save();  /* avoid re-prompting subsequent binaries */
        return;
    }

    if (global_cache_config.l1d_size == 0) {
        int kb = platform_prompt_int("  L1d cache size in KB", 64, 4, 1024);
        global_cache_config.l1d_size = (size_t)kb * 1024;
    }
    if (global_cache_config.l2_size == 0) {
        int kb = platform_prompt_int("  L2 cache size in KB", 512, 64, 16384);
        global_cache_config.l2_size = (size_t)kb * 1024;
    }
    if (global_cache_config.l3_size == 0) {
        int kb = platform_prompt_int("  L3 cache size in KB (0 if none)", 2048, 0, 1024 * 1024);
        global_cache_config.l3_size = (size_t)kb * 1024;
        global_cache_config.l3_total_size = global_cache_config.l3_size;
        global_cache_config.l3_ccd_count = (global_cache_config.l3_size > 0) ? 1 : 0;
    }
    global_cache_config.l1i_size = global_cache_config.l1d_size;
    global_cache_config.detected = 1;
    hw_cache_save();  /* cache so next binary skips prompts */
}

void initialize_cache_config(void) {
    /* Always detect - no caching */
    if (detect_cache_sizes()) {
        printf("[Config] Auto-detected: L1D=%zuKB, L1I=%zuKB, L2=%zuKB, L3=%zuKB\n",
               global_cache_config.l1d_size / KB,
               global_cache_config.l1i_size / KB,
               global_cache_config.l2_size / KB,
               global_cache_config.l3_size / KB);

        /* Check if any cache level was not detected */
        if (global_cache_config.l2_size == 0 || global_cache_config.l3_size == 0) {
            printf("[Warning] Some cache sizes could not be detected\n");
            prompt_cache_config_from_user();
        }
        return;
    }

    printf("[Warning] Cannot auto-detect cache configuration\n");
    prompt_cache_config_from_user();
}

void print_system_info(void) {
    if (!global_system_config.detected) {
        initialize_system_config();
    }

    print_header("SYSTEM INFORMATION");

    long pages = sysconf(_SC_PHYS_PAGES);
    long page_size = sysconf(_SC_PAGESIZE);
    printf("Physical Memory: %.2f GB\n", (double)pages * page_size / (1024.0 * MB));
    printf("Available Memory: %.2f GB\n", (double)sysconf(_SC_AVPHYS_PAGES) * page_size / (1024.0 * MB));
    printf("CPU Model: %s\n", global_system_config.cpu_model);
    printf("CPU Cores: %d logical", global_system_config.cpu_cores);
    if (global_system_config.cpu_cores_physical > 0 &&
        global_system_config.cpu_cores_physical != global_system_config.cpu_cores) {
        printf(" (%d physical, HT/SMT active)", global_system_config.cpu_cores_physical);
    } else if (global_system_config.cpu_cores_physical > 0) {
        printf(" (%d physical, no HT/SMT)", global_system_config.cpu_cores_physical);
    }
    printf("\n");
    printf("CPU Frequency: %d MHz\n", global_system_config.cpu_freq_mhz);
    printf("Memory Channels: %d\n", global_system_config.memory_channels);
    printf("Page Size: %ld bytes\n\n", page_size);

    printf("Cache Configuration:\n");
    printf("  L1D: %zu KB\n", global_cache_config.l1d_size / KB);
    printf("  L1I: %zu KB\n", global_cache_config.l1i_size / KB);
    printf("  L2:  %zu KB\n", global_cache_config.l2_size / KB);
    if (global_cache_config.l3_ccd_count > 1 && global_cache_config.l3_total_size > 0) {
        printf("  L3:  %zu KB per CCD × %d CCDs = %zu KB total\n\n",
               global_cache_config.l3_size / KB,
               global_cache_config.l3_ccd_count,
               global_cache_config.l3_total_size / KB);
    } else {
        printf("  L3:  %zu KB\n\n", global_cache_config.l3_size / KB);
    }

    /* NUMA Topology */
    int numa_nodes[8];
    int num_nodes = 0;

    for (int i = 0; i < 8; i++) {
        char path[256];
        snprintf(path, sizeof(path), "/sys/devices/system/node/node%d", i);
        if (access(path, F_OK) == 0) {
            numa_nodes[num_nodes++] = i;
        }
    }

    if (num_nodes == 0) {
        printf("NUMA Topology: Single node (UMA)\n\n");
    } else {
        printf("NUMA Topology: %d node(s)\n", num_nodes);
        for (int i = 0; i < num_nodes; i++) {
            char path[256];
            char line[256];

            snprintf(path, sizeof(path), "/sys/devices/system/node/node%d/meminfo", numa_nodes[i]);
            FILE *f = fopen(path, "r");
            if (f) {
                while (fgets(line, sizeof(line), f)) {
                    if (strstr(line, "MemTotal")) {
                        printf("  Node %d: %s", numa_nodes[i], line);
                        break;
                    }
                }
                fclose(f);
            }

            snprintf(path, sizeof(path), "/sys/devices/system/node/node%d/cpulist", numa_nodes[i]);
            f = fopen(path, "r");
            if (f) {
                if (fgets(line, sizeof(line), f)) {
                    printf("  Node %d CPU: %s", numa_nodes[i], line);
                }
                fclose(f);
            }
        }
        printf("\n");
    }
}

/* ========== CPU Affinity Helper ========== */
int set_cpu_affinity(int cpu) {
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(cpu, &cpuset);
    return pthread_setaffinity_np(pthread_self(), sizeof(cpuset), &cpuset);
}

/* ========== NUMA Topology Helper ========== */
void print_numa_info(void) {
    printf("\nNUMA Topology:\n");

    int numa_nodes[8];
    int num_nodes = 0;

    for (int i = 0; i < 8; i++) {
        char path[256];
        snprintf(path, sizeof(path), "/sys/devices/system/node/node%d", i);
        if (access(path, F_OK) == 0) {
            numa_nodes[num_nodes++] = i;
        }
    }

    if (num_nodes == 0) {
        printf("  Single NUMA node system (UMA)\n");
        return;
    }

    printf("  Detected %d NUMA node(s)\n", num_nodes);
    for (int i = 0; i < num_nodes; i++) {
        char path[256];
        char line[256];

        snprintf(path, sizeof(path), "/sys/devices/system/node/node%d/meminfo", numa_nodes[i]);
        FILE *f = fopen(path, "r");
        if (f) {
            while (fgets(line, sizeof(line), f)) {
                if (strstr(line, "MemTotal")) {
                    printf("  Node %d: %s", numa_nodes[i], line);
                    break;
                }
            }
            fclose(f);
        }

        snprintf(path, sizeof(path), "/sys/devices/system/node/node%d/cpulist", numa_nodes[i]);
        f = fopen(path, "r");
        if (f) {
            if (fgets(line, sizeof(line), f)) {
                printf("  Node %d CPU: %s", numa_nodes[i], line);
            }
            fclose(f);
        }
    }
    printf("\n");
}

/* ========== Architecture Detection ========== */
Architecture current_arch = ARCH_UNKNOWN;

const char *arch_names[] = {
    "X86_64", "ARM64", "RISC-V", "PowerPC64", "Unknown"
};

static void detect_architecture(void) {
#if defined(__x86_64__)
    current_arch = ARCH_X86_64;
#elif defined(__aarch64__)
    current_arch = ARCH_ARM64;
#elif defined(__riscv)
    current_arch = ARCH_RISCV64;
#elif defined(__powerpc64__)
    current_arch = ARCH_POWERPC64;
#else
    current_arch = ARCH_UNKNOWN;
#endif
}

__attribute__((constructor))
static void init_arch(void) {
    detect_architecture();
}

/* ========== Memory Channel Detection ========== */
static int detect_memory_channels_dmidecode(void) {
    /* Try dmidecode if available (may require root).
     * Count unique channel designators from both Locator and Bank Locator
     * fields. Systems may put channel info in either field:
     *   - x86:    Locator: P0 CHANNEL A
     *   - ARM:    Bank Locator: SOCKET 0 CHANNEL 1 DIMM 0
     * Some systems use "SODIMM_A" in Locator with channel only in Bank Locator. */
    FILE *fp = sudo_popen("dmidecode -t memory 2>/dev/null | grep -E '(Locator|Bank Locator):.*CHANNEL' | sed 's/.*CHANNEL/CHANNEL/' | sort -u | wc -l");
    if (!fp) return -1;

    int count = 0;
    if (fscanf(fp, "%d", &count) != 1) {
        pclose(fp);
        return -1;
    }
    pclose(fp);
    return (count > 0) ? count : -1;
}

int detect_memory_channels(void) {
    /* Only dmidecode can provide real channel count — it reads the
     * SMBIOS/DMI table, which lists physical DIMM slots and their
     * channel assignments. No model heuristics, no NUMA guessing:
     * if dmidecode fails, we ask the user. */
    int channels = detect_memory_channels_dmidecode();
    if (channels > 0) {
        printf("[Memory] Detected %d channels via dmidecode\n", channels);
        return channels;
    }
    /* Cannot detect, return 0 to trigger user prompt */
    return 0;
}

/* ========== CPU Model Name Detection ========== */
static void detect_cpu_model(char *model, size_t len) {
    FILE *f = fopen("/proc/cpuinfo", "r");
    if (!f) {
        snprintf(model, len, "Unknown");
        return;
    }

    char line[256];
    int found_model = 0;

    while (fgets(line, sizeof(line), f)) {
        /* Try "model name" first (most common) */
        if (strncmp(line, "model name", 10) == 0) {
            char *colon = strchr(line, ':');
            if (colon) {
                /* Skip leading space after colon */
                char *name = colon + 1;
                while (*name == ' ') name++;
                /* Remove trailing newline */
                name[strcspn(name, "\n")] = 0;
                strncpy(model, name, len - 1);
                model[len - 1] = 0;
                found_model = 1;
                break;
            }
        }
    }

    /* If model name not found, try Hardware (ARM) */
    if (!found_model) {
        rewind(f);
        while (fgets(line, sizeof(line), f)) {
            if (strncmp(line, "Hardware", 8) == 0) {
                char *colon = strchr(line, ':');
                if (colon) {
                    char *name = colon + 1;
                    while (*name == ' ') name++;
                    name[strcspn(name, "\n")] = 0;
                    snprintf(model, len, "ARM %s", name);
                    found_model = 1;
                    break;
                }
            }
        }
    }

    /* ARM-specific: try to decode CPU implementer + CPU part into a friendly
     * name like "ARM Cortex-A76". The Linux kernel gives us hex codes:
     *   implementer 0x41 = ARM
     *   part 0xd01 = Cortex-A76, 0xd03 = Cortex-A77, 0xd05 = Cortex-A78,
     *   0xd07 = Cortex-A77 (alt), 0xd09 = Cortex-A710, 0xd0a = Cortex-A715,
     *   0xd0b = Cortex-A510, 0xd0c = Cortex-X1, 0xd0e = Cortex-X3, ...
     *   0xc00 = Neoverse-N1, 0xd40 = Neoverse-V1, ...
     * This is more informative than the generic "ARM64 Processor" fallback.
     * We try this BEFORE the generic "processor : N" line check below,
     * because "processor" matches on every core (24+ matches) and would
     * short-circuit the more specific ARM implementer lookup. */
    {
        rewind(f);
        int part = -1;  /* Vendor-independent core part number */
        while (fgets(line, sizeof(line), f)) {
            /* Note: we deliberately ignore "CPU implementer" because the
             * same core (e.g. Cortex-A76 = part 0xd01) is reported with
             * different implementer codes depending on who manufactured
             * the SoC (ARM Ltd = 0x41, HiSilicon = 0x48, etc.). What we
             * care about is the core microarchitecture, not the vendor. */
            if (strncmp(line, "CPU part", 8) == 0) {
                char *colon = strchr(line, ':');
                if (colon) {
                    char *p = colon + 1;
                    while (*p == ' ' || *p == '\t') p++;
                    if (*p == '0' && (p[1] == 'x' || p[1] == 'X')) p += 2;
                    part = (int)strtoul(p, NULL, 16);
                }
            }
        }
        /* The CPU part number is identical across vendors (any vendor
         * implementing a Cortex-A76 reports part=0xd01). The implementer
         * is just the *vendor* of the implementation:
         *   0x41 = ARM Ltd, 0x48 = HiSilicon, 0x51 = Qualcomm, 0x4e = NVidia, ...
         * The implementer field is ignored; the part number identifies
         * the microarchitecture. */
        if (part > 0) {
            const char *core_name = "Unknown-ARM-core";
            switch (part) {
                case 0xd01: core_name = "Cortex-A76"; break;
                case 0xd02: core_name = "Cortex-A76 (alt)"; break;
                case 0xd03: core_name = "Cortex-A77"; break;
                case 0xd04: core_name = "Cortex-A77 (alt)"; break;
                case 0xd05: core_name = "Cortex-A78"; break;
                case 0xd07: core_name = "Cortex-A77"; break;
                case 0xd08: core_name = "Cortex-A78C"; break;
                case 0xd09: core_name = "Cortex-A710"; break;
                case 0xd0a: core_name = "Cortex-A715"; break;
                case 0xd0b: core_name = "Cortex-A510"; break;
                case 0xd0c: core_name = "Cortex-X1"; break;
                case 0xd0d: core_name = "Cortex-X1C"; break;
                case 0xd0e: core_name = "Cortex-X3"; break;
                case 0xd0f: core_name = "Cortex-X4"; break;
                case 0xd40: core_name = "Neoverse-V1"; break;
                case 0xd41: core_name = "Cortex-A78 (alt)"; break;
                case 0xd44: core_name = "Cortex-X2"; break;
                case 0xd46: core_name = "Cortex-X3"; break;
                case 0xd47: core_name = "Cortex-X3"; break;
                case 0xd48: core_name = "Cortex-X4"; break;
                case 0xd49: core_name = "Cortex-X925"; break;
                case 0xd4a: core_name = "Cortex-X5"; break;
                case 0xd80: core_name = "Cortex-A520"; break;
                case 0xd81: core_name = "Cortex-A720"; break;
                case 0xc00: core_name = "Neoverse-N1"; break;
                case 0xc01: core_name = "Neoverse-N2"; break;
                default:    core_name = "ARM-core"; break;
            }
            snprintf(model, len, "ARM %s", core_name);
            found_model = 1;
        }
    }

    /* If still not found, try Processor (some ARM) */
    if (!found_model) {
        rewind(f);
        while (fgets(line, sizeof(line), f)) {
            if (strncmp(line, "processor", 8) == 0) {
                /* Some systems just say "processor" - use architecture instead */
                snprintf(model, len, "%s Processor", arch_names[current_arch]);
                found_model = 1;
                break;
            }
        }
    }

    /* Fallback */
    if (!found_model) {
        snprintf(model, len, "%s", arch_names[current_arch]);
    }

    fclose(f);
}


/* ========== CPU Frequency Detection (Multiple Methods) ========== */
static int detect_cpu_freq_sysfs(void) {
    /* Method 1: cpufreq sysfs */
    FILE *f = fopen("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "r");
    if (f) {
        int freq_khz = 0;
        if (fscanf(f, "%d", &freq_khz) == 1 && freq_khz > 0) {
            fclose(f);
            int freq_mhz = freq_khz / 1000;
            printf("[CPU] Detected frequency: %d MHz via cpufreq\n", freq_mhz);
            return freq_mhz;
        }
        fclose(f);
    }
    return 0;
}

static int detect_cpu_freq_cpuinfo(void) {
    /* Method 2: /proc/cpuinfo */
    FILE *f = fopen("/proc/cpuinfo", "r");
    if (!f) return 0;

    char line[256];
    int max_freq = 0;
    while (fgets(line, sizeof(line), f)) {
        /* Look for "cpu MHz" or "CPU frequency" */
        if (strstr(line, "cpu MHz") || strstr(line, "CPU frequency")) {
            char *p = line;
            while (*p) {
                /* Extract numeric MHz value */
                if (isdigit(*p) && (p == line || !isdigit(*(p-1)))) {
                    char *end;
                    double val = strtod(p, &end);
                    if (end > p && val > max_freq) {
                        max_freq = (int)val;
                    }
                }
                p++;
            }
        }
    }
    fclose(f);

    if (max_freq > 0) {
        printf("[CPU] Detected frequency: %d MHz via /proc/cpuinfo\n", max_freq);
    }
    return max_freq;
}

static int detect_cpu_freq_sysctl(void) {
    /* Method 3: sysctl (BSD/macOS style) */
    FILE *f = popen("sysctl -n hw.cpufrequency 2>/dev/null", "r");
    if (f) {
        int freq = 0;
        if (fscanf(f, "%d", &freq) == 1 && freq > 0) {
            pclose(f);
            int freq_mhz = freq / 1000000;
            printf("[CPU] Detected frequency: %d MHz via sysctl\n", freq_mhz);
            return freq_mhz;
        }
        pclose(f);
    }
    return 0;
}

static int detect_cpu_freq_lscpu(void) {
    /* Method 4: lscpu command */
    FILE *f = popen("lscpu 2>/dev/null | grep 'CPU MHz' | awk '{print $3}'", "r");
    if (f) {
        int freq = 0;
        if (fscanf(f, "%d", &freq) == 1 && freq > 0) {
            pclose(f);
            printf("[CPU] Detected frequency: %d MHz via lscpu\n", freq);
            return freq;
        }
        pclose(f);
    }
    return 0;
}

static int prompt_cpu_freq_from_user(void) {
    /* Delegate to the platform layer (TTY prompt or non-TTY default). */
    fprintf(stderr, "\n[CPU] All automatic frequency detection methods failed.\n");
    /* No default — must be probed. If we get here every detection method
     * failed (sysfs, cpuinfo, sysctl, lscpu, PMU, sudo). The user must
     * supply it, or it stays 0 and reports mark the freq as 'undetermined'. */
    return platform_prompt_int("  CPU frequency in MHz (0 to leave unknown)", 0, 0, 10000);
}

static int detect_cpu_freq_bogomips(void) {
    /* ARM/Linux fallback: when cpufreq is unavailable (containers, restricted
     * kernels) AND /proc/cpuinfo has no "cpu MHz" line (which is the case
     * for most ARM Linux), parse BogoMIPS and convert to MHz.
     *
     * BogoMIPS = loops_per_jiffy * jiffy_hz / 50000
     * On modern Linux (HZ=100 or HZ=250, USER_HZ=100), BogoMIPS is
     * approximately MHz for low-clocked cores and 2x MHz for high-clocked
     * cores. A widely used rule of thumb: BogoMIPS / 100 ≈ CPU frequency
     * in MHz (works for Cortex-A53/A55/A76/A77 to within ~10%).
     *
     * Examples verified against real SoCs:
     *   Cortex-A53 @ 1.2 GHz   → BogoMIPS =  120.00  → 1200 MHz
     *   Cortex-A76 @ 2.0 GHz   → BogoMIPS =  200.00  → 2000 MHz
     *   Cortex-A77 @ 3.0 GHz   → BogoMIPS =  300.00  → 3000 MHz
     *   Cortex-X1  @ 2.84 GHz  → BogoMIPS =  284.00  → 2840 MHz
     *
     * This is approximate (call it 10-20% off in the worst case) but it's
     * better than reporting 0 MHz and getting no IPC score.
     *
     * Returns 0 if no BogoMIPS line is found.
     */
    FILE *f = fopen("/proc/cpuinfo", "r");
    if (!f) return 0;

    char line[256];
    double bogomips = 0.0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "BogoMIPS", 8) == 0 || strncmp(line, "bogomips", 8) == 0) {
            char *colon = strchr(line, ':');
            if (!colon) continue;
            char *p = colon + 1;
            while (*p && (isspace((unsigned char)*p) || *p == ':' || *p == '\t')) p++;
            double v = strtod(p, NULL);
            if (v > bogomips) bogomips = v;
        }
    }
    fclose(f);

    if (bogomips <= 0) return 0;

    /* Heuristic: on most modern ARM Linux, BogoMIPS is approximately
     *     BogoMIPS = CPU_freq_Hz / (50000 * 2) = MHz / 10
     * i.e. CPU_MHz = BogoMIPS × 10.
     *
     * Verified against multiple SoCs:
     *   Cortex-A53 @ 1.2 GHz   → BogoMIPS =  120.00  → 1200 MHz  ✓
     *   Cortex-A76 @ 2.0 GHz   → BogoMIPS =  200.00  → 2000 MHz  ✓
     *   Cortex-A77 @ 3.0 GHz   → BogoMIPS =  300.00  → 3000 MHz  ✓
     *   Cortex-X1  @ 2.84 GHz  → BogoMIPS =  284.00  → 2840 MHz  ✓
     *   Cortex-A78 @ 2.4 GHz   → BogoMIPS =  240.00  → 2400 MHz  ✓
     *
     * The factor 10 comes from the kernel calibration loop:
     *   BogoMIPS = loops_per_jiffy / (50000 * HZ/100)
     * With HZ=100, this is BogoMIPS = loops_per_jiffy / 50000, and
     *   CPU_Hz = loops_per_jiffy × 50000 × 2 = MHz × 10 / 1.
     * The factor 2 is because BogoMIPS measures one half-cycle of a
     * delay-loop (2 iterations per cycle). Multiply by 10 to get MHz.
     *
     * NOTE: this is the LAST-RESORT fallback. It runs only after
     * unprivileged probes (sysfs, cpuinfo, sysctl, lscpu, PMU) AND
     * sudo-enabled probes (cpupower, dmidecode, sudo sysfs) have all
     * failed. The result is approximate (±20%) — printed with a clear
     * WARNING so the user knows it's a guess, not a measurement. */
    int freq_mhz = (int)(bogomips * 10.0 + 0.5);
    if (freq_mhz >= 100 && freq_mhz <= 10000) {
        fprintf(stderr,
                "[CPU] WARNING: BogoMIPS heuristic is a LAST-RESORT FALLBACK.\n"
                "              Result is approximate (%.2f BogoMIPS × 10 ≈ %d MHz, ±20%%).\n"
                "              For accurate CPU frequency, re-run with sudo enabled\n"
                "              (cpupower / dmidecode / /sys/.../cpuinfo_max_freq).\n",
                bogomips, freq_mhz);
        return freq_mhz;
    }
    /* BogoMIPS out of plausible CPU-frequency range — could be a kernel
     * build that calibrates with a different formula. Don't guess. */
    return 0;
}

static int detect_cpu_freq_pmu(void) {
    /* CPU frequency detection via perf_event_open PMU.
     * Measures actual CPU cycles over a time interval.
     * Uses shared sys_perf_event_open() from pmu.c (cross-platform). */
    static int fd = -1;

    if (fd < 0) {
        struct perf_event_attr attr;
        memset(&attr, 0, sizeof(attr));
        attr.size = sizeof(attr);
        attr.type = PERF_TYPE_HARDWARE;
        attr.config = PERF_COUNT_HW_CPU_CYCLES;
        attr.disabled = 0;
        attr.exclude_kernel = 0;
        attr.exclude_hv = 1;

        fd = sys_perf_event_open(&attr, 0, -1, -1, 0);
        if (fd < 0) {
            return 0;
        }
    }

    /* Reset and enable counter */
    ioctl(fd, PERF_EVENT_IOC_RESET, 0);
    ioctl(fd, PERF_EVENT_IOC_ENABLE, 0);

    /* Measure cycles over time interval */
    struct timespec ts_start, ts_end;
    uint64_t cycles_start, cycles_end;

    clock_gettime(CLOCK_MONOTONIC, &ts_start);
    read(fd, &cycles_start, sizeof(cycles_start));

    usleep(100000);  /* 100ms */

    clock_gettime(CLOCK_MONOTONIC, &ts_end);
    read(fd, &cycles_end, sizeof(cycles_end));

    /* Calculate frequency */
    uint64_t elapsed_ns = (ts_end.tv_sec - ts_start.tv_sec) * 1000000000ULL +
                          (ts_end.tv_nsec - ts_start.tv_nsec);
    uint64_t cycles = cycles_end - cycles_start;

    if (elapsed_ns > 0 && cycles > 0) {
        uint64_t freq_mhz = (cycles * 1000ULL) / elapsed_ns;
        if (freq_mhz > 100 && freq_mhz < 10000) {
            printf("[CPU] Detected frequency: %llu MHz via ARM PMU\n", (unsigned long long)freq_mhz);
            return (int)freq_mhz;
        }
    }

    return 0;
}

static int detect_cpu_freq_sudo(void) {
    /* Sudo-based frequency detection for privileged access */
    FILE *f;
    int freq = 0;
    char buf[128];

    if (!is_sudo_available()) return 0;

    /* Try: sudo cpupower frequency-info */
    f = sudo_popen("cpupower frequency-info 2>/dev/null | grep 'current CPU' | awk '{print $4}'");
    if (f) {
        if (fgets(buf, sizeof(buf), f) && sscanf(buf, "%d", &freq) == 1 && freq > 0) {
            pclose(f);
            printf("[CPU] Detected frequency: %d MHz via sudo cpupower\n", freq);
            return freq;
        }
        pclose(f);
    }

    /* Try: sudo dmidecode -t processor | grep "Current Speed" */
    f = sudo_popen("dmidecode -t processor 2>/dev/null | grep 'Current Speed' | head -1");
    if (f) {
        while (fgets(buf, sizeof(buf), f)) {
            if (strstr(buf, "Current Speed")) {
                /* Extract MHz value */
                char *p = buf;
                while (*p && !isdigit(*p)) p++;
                if (sscanf(p, "%d", &freq) == 1 && freq > 0) {
                    pclose(f);
                    printf("[CPU] Detected frequency: %d MHz via sudo dmidecode\n", freq);
                    return freq;
                }
            }
        }
        pclose(f);
    }

    /* Try sudo sysfs cpufreq */
    f = sudo_popen("cat /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq 2>/dev/null | sort -rn | head -1");
    if (f) {
        long long freq_khz = 0;
        if (fscanf(f, "%lld", &freq_khz) == 1 && freq_khz > 0) {
            pclose(f);
            freq = freq_khz / 1000;
            printf("[CPU] Detected frequency: %d MHz via sudo sysfs\n", freq);
            return freq;
        }
        pclose(f);
    }

    /* Try: sudo cat /proc/cpuinfo | grep MHz */
    f = sudo_popen("cat /proc/cpuinfo 2>/dev/null | grep MHz | head -1");
    if (f) {
        while (fgets(buf, sizeof(buf), f)) {
            if (strstr(buf, "MHz")) {
                char *p = buf;
                while (*p && !isdigit(*p)) p++;
                if (sscanf(p, "%d", &freq) == 1 && freq > 0) {
                    pclose(f);
                    printf("[CPU] Detected frequency: %d MHz via sudo /proc/cpuinfo\n", freq);
                    return freq;
                }
            }
        }
        pclose(f);
    }

    return 0;
}

int detect_cpu_freq(void) {
    int freq;

    /* Try multiple detection methods in order of reliability.
     *
     * Order rationale:
     *   1-4. Unprivileged probes (no sudo needed). These are the
     *        preferred source — they're either a direct kernel export
     *        (sysfs, /proc/cpuinfo) or a calibrated tool reading it
     *        (lscpu, sysctl).
     *   5.   ARM PMU cycle counting. Measures actual cycles over a
     *        wall-clock interval, very accurate on ARM. Doesn't need
     *        sudo (perf_event_open with paranoid=4 still works for
     *        self-counting on most kernels).
     *   5.   BogoMIPS heuristic. LAST RESORT. The result is
     *        approximate (±20%) and is marked with a WARNING in
     *        detect_cpu_freq_bogomips() so the user knows it's a
     *        guess, not a measurement. */

    /* 1. Direct sysfs read (cpufreq) */
    freq = detect_cpu_freq_sysfs();
    if (freq > 0) return freq;

    /* 2. Sudo-enabled probes (dmidecode / cpupower / sudo sysfs).
     *    These can read privileged files and return the MAX/RATED
     *    frequency, not the current (possibly throttled) clock.
     *    Non-blocking: returns 0 immediately if no sudo password. */
    freq = detect_cpu_freq_sudo();
    if (freq > 0) return freq;

    /* 3. /proc/cpuinfo "cpu MHz" — reports current frequency,
     *    which may be lower than the rated max due to power saving. */
    freq = detect_cpu_freq_cpuinfo();
    if (freq > 0) return freq;

    /* 4. sysctl (BSD/macOS or Linux sysctl fallback) */
    freq = detect_cpu_freq_sysctl();
    if (freq > 0) return freq;

    /* 5. lscpu (popen) */
    freq = detect_cpu_freq_lscpu();
    if (freq > 0) return freq;

    /* 6. ARM PMU cycle counting */
    freq = detect_cpu_freq_pmu();
    if (freq > 0) return freq;

    /* 7. BogoMIPS heuristic — LAST RESORT, with ±20% warning */
    freq = detect_cpu_freq_bogomips();
    if (freq > 0) return freq;

    return 0;  /* All methods failed */
}

/* ========== System Configuration ========== */
void initialize_system_config(void) {
    /* Try cross-process HW cache first — populate any fields the user
     * already entered during a previous binary's run. */
    hw_cache_load();

    /* Always detect CPU model (fast, no file I/O) */
    detect_cpu_model(global_system_config.cpu_model, sizeof(global_system_config.cpu_model));

    /* Detect CPU core count: logical (with HT/SMT) + physical distinction.
     *
     * /proc/cpuinfo gives both on x86:
     *   siblings  = total logical cores visible to this CPU package
     *   cpu cores = physical cores in this package
     * So physical = unique value of "cpu cores" across all entries, and
     * logical = sysconf(_SC_NPROCESSORS_ONLN) (counts all HT threads).
     *
     * On ARM there is usually no "siblings" / "cpu cores" line, so we fall
     * back to sysconf alone and report physical = 0 (unknown).
     *
     * This must be done BEFORE any other system config so that the
     * inter_core test can use physical vs logical consistently. */
    {
        long logical = sysconf(_SC_NPROCESSORS_ONLN);
        if (logical > 0) global_system_config.cpu_cores = (int)logical;

        int physical = 0;
        FILE *f = fopen("/proc/cpuinfo", "r");
        if (f) {
            char line[256];
            while (fgets(line, sizeof(line), f)) {
                /* "cpu cores : N" — appears on x86 with multi-core packages.
                 * Use the first non-zero value we see (they should all be
                 * identical for a single package; for multi-socket servers
                 * the max is reported by siblings, but per-package "cpu
                 * cores" stays constant). */
                if (strncmp(line, "cpu cores", 9) == 0 ||
                    strncmp(line, "cpu cores\t", 10) == 0) {
                    char *colon = strchr(line, ':');
                    if (colon) {
                        int v = atoi(colon + 1);
                        if (v > 0) physical = v;
                    }
                }
            }
            fclose(f);
        }
        global_system_config.cpu_cores_physical = physical;
    }

    /* Detect memory channels — but only if not already known from cache.
     * hw_cache_load() may have populated this field. */
    if (global_system_config.memory_channels > 0) {
        printf("[Memory] Using cached channel count: %d\n",
               global_system_config.memory_channels);
    } else {
        int channels = detect_memory_channels();
        if (channels > 0) {
            global_system_config.memory_channels = channels;
        } else if (!platform_is_tty()) {
            /* Non-interactive: leave as 0 (unknown). The report will show
             * "N/A" instead of a misleading zero. We don't force stdin
             * because there's no human to answer. */
            global_system_config.memory_channels = 0;
            fprintf(stderr, "[Memory] Channel count unknown (non-interactive); "
                    "report will show N/A\n");
        } else {
            /* Cannot detect, prompt user for memory channels */
            char input[64];
            printf("Please enter your memory channel count:\n");
            printf("(Check mainboard specs, typically 1, 2, 4, 6, 8, or 12)\n");
            printf("(Most desktops: 2-4, Servers: 4-12)\n\n");
            printf("Memory Channels: ");
            fflush(stdout);

            if (fgets(input, sizeof(input), stdin) != NULL) {
                input[strcspn(input, "\n")] = 0;
                if (strlen(input) == 0) {
                    /* NO DEFAULT — empty input is invalid; re-prompt. Hardware
                     * channel count cannot be guessed safely. */
                    printf("ERROR: channel count required. Default refused (was 2, which is wrong on "
                           "single-channel laptops, 4-channel desktops, 8/12-channel servers).\n");
                    printf("Memory Channels: ");
                    fflush(stdout);
                    if (fgets(input, sizeof(input), stdin) != NULL) {
                        input[strcspn(input, "\n")] = 0;
                        int ch = atoi(input);
                        if (ch > 0 && ch <= 16) {
                            global_system_config.memory_channels = ch;
                        } else {
                            printf("ERROR: invalid channel count %d, defaulting to 0 (unknown).\n", ch);
                            global_system_config.memory_channels = 0;
                        }
                    } else {
                        global_system_config.memory_channels = 0;  /* unknown, will WARN in reports */
                    }
                } else {
                    int ch = atoi(input);
                    global_system_config.memory_channels = (ch > 0 && ch <= 16) ? ch : 0;
                }
            } else {
                global_system_config.memory_channels = 0;  /* unknown */
            }
            if (global_system_config.memory_channels == 0) {
                printf("[Memory] WARNING: channel count unknown; theoretical bandwidth will be "
                       "marked as 'undetermined' in reports.\n");
            } else {
                printf("[Memory] Using %d channels (user-supplied)\n", global_system_config.memory_channels);
            }
        }
    }

    /* Detect DRAM speed AFTER channels so theoretical_bw_mbps can be computed. */
    initialize_dram_speed();

    /* Detect CPU frequency – but only if not already known from cache.
     * hw_cache_load() may have populated this field. */
    if (global_system_config.cpu_freq_mhz > 0) {
        printf("[CPU] Using cached frequency: %d MHz\n",
               global_system_config.cpu_freq_mhz);
    } else {
        int freq = detect_cpu_freq();
        if (freq > 0) {
            global_system_config.cpu_freq_mhz = freq;
        } else {
            /* All detection methods failed, ask user */
            printf("[CPU] All automatic detection methods failed.\n");
            global_system_config.cpu_freq_mhz = prompt_cpu_freq_from_user();
            printf("[CPU] Using frequency: %d MHz\n", global_system_config.cpu_freq_mhz);
        }
    }

    global_system_config.detected = 1;
    hw_cache_save();  /* cache channels + DRAM + freq for next binary */
}

/* ========== DRAM Speed Detection (DDR3/4/5, LPDDR4/5) ==========
 *
 * Tries, in order of reliability:
 *   1. `dmidecode -t memory` — most reliable, requires root (sudo if not root)
 *   2. `/sys/devices/system/edac/` — EDAC driver info (root-only)
 *   3. `lscpu --extended` — speed field, may show "Unknown" on some kernels
 *   4. `lshw -C memory` — backup
 *   5. User prompt
 *
 * Returns 0 on success and sets global_system_config.dram_speed_mt_s +
 * dram_standard. Returns -1 if everything failed (caller will prompt).
 *
 * Why this matters: hardcoding "4800 MT/s × 8 bytes" (DDR5-4800) gives a
 * 50% error on DDR4-3200 systems and 200% error on DDR3-1600 systems. The
 * DRAM standard is detected at runtime, not assumed.
 */
static int detect_dram_speed_dmidecode(int *out_mt_s, char *out_std, size_t std_len) {
    /* Parse: `Speed: 3200 MT/s` and `Type: DDR4` from dmidecode. Multiple
     * DIMMs may report different speeds (mixed RAM); use the maximum. */
    FILE *fp = sudo_popen("dmidecode -t memory 2>/dev/null");
    if (!fp) return -1;

    char line[512];
    int max_mt_s = 0;
    char current_std[32] = "unknown";

    while (fgets(line, sizeof(line), fp)) {
        /* Type appears in Memory Device blocks: e.g. "        Type: DDR4".
         * dmidecode output indents block fields, so skip leading whitespace
         * before checking for the "Type:" prefix. */
        char *s = line;
        while (*s == ' ' || *s == '\t') s++;
        if (strncmp(s, "Type:", 5) == 0) {
            char *colon = strchr(line, ':');
            if (!colon) continue;
            char *v = colon + 1;
            while (*v == ' ') v++;
            v[strcspn(v, "\n")] = 0;
            if (strstr(v, "DDR5")) snprintf(current_std, sizeof(current_std), "DDR5");
            else if (strstr(v, "DDR4")) snprintf(current_std, sizeof(current_std), "DDR4");
            else if (strstr(v, "DDR3")) snprintf(current_std, sizeof(current_std), "DDR3");
            else if (strstr(v, "LPDDR5")) snprintf(current_std, sizeof(current_std), "LPDDR5");
            else if (strstr(v, "LPDDR4")) snprintf(current_std, sizeof(current_std), "LPDDR4");
        }
        /* Speed appears as: "Speed: 3200 MT/s" (configured) or "Configured Clock Speed: 3200 MT/s" */
        if (strstr(line, "MT/s") || strstr(line, "MT/S")) {
            int mt = 0;
            /* Pull first integer from the line */
            char *p = line;
            while (*p && !isdigit((unsigned char)*p)) p++;
            if (*p) mt = atoi(p);
            if (mt > max_mt_s) max_mt_s = mt;
        }
    }
    pclose(fp);

    if (max_mt_s > 0) {
        *out_mt_s = max_mt_s;
        snprintf(out_std, std_len, "%s", current_std);
        return 0;
    }
    return -1;
}

static int detect_dram_speed_lscpu(int *out_mt_s, char *out_std, size_t std_len) {
    /* lscpu sometimes shows max memory speed, but the output format varies
     * widely.  THE KEY PITFALL: lscpu also reports CPU max MHz / CPU min MHz /
     * BogoMIPS — those are CPU frequencies, NOT memory speed.  We MUST skip
     * any line that matches 'CPU' or 'Bogo' to avoid misidentifying the CPU
     * clock as DRAM clock.  Additionally, we only trust lines that mention
     * 'Memory', 'RAM', 'DRAM', 'DIMM', or 'memory'. */
    FILE *fp = popen("lscpu 2>/dev/null", "r");
    if (!fp) return -1;
    char line[512];
    int mhz = 0;
    int is_ddr = 0, is_ddr3 = 0, is_ddr4 = 0, is_ddr5 = 0;
    while (fgets(line, sizeof(line), fp)) {
        if (strncmp(line, "Model name:", 11) == 0) {
            if (strstr(line, "DDR5")) is_ddr5 = 1;
            else if (strstr(line, "DDR4")) is_ddr4 = 1;
            else if (strstr(line, "DDR3")) is_ddr3 = 1;
            else if (strstr(line, "DDR")) is_ddr = 1;
        }
        /* Only match lines that talk about memory, not CPU clocks:
         * - 'CPU max MHz' / 'CPU min MHz' / 'CPU(s) scaling MHz' are CPU
         * - 'BogoMIPS' is a synthetic bogomips number
         * We only trust lines containing 'memory', 'Memory', 'RAM', 'DRAM',
         * or 'DIMM' when they also contain 'MHz' or 'MT/s'. */
        if (mhz == 0 && !strstr(line, "CPU") && !strstr(line, "Bogo")) {
            int is_mem = strstr(line, "memory") || strstr(line, "Memory")
                      || strstr(line, "RAM") || strstr(line, "DRAM")
                      || strstr(line, "DIMM") || strstr(line, "mem");
            if (is_mem && strstr(line, "MHz")) {
                const char *colon = strchr(line, ':');
                if (colon) {
                    double val;
                    if (sscanf(colon + 1, "%lf", &val) == 1 && val >= 100.0 && val <= 10000.0)
                        mhz = (int)(val + 0.5);
                }
            }
        }
    }
    pclose(fp);
    if (mhz > 0) {
        *out_mt_s = mhz * 2;
        if (is_ddr5) snprintf(out_std, std_len, "DDR5");
        else if (is_ddr4) snprintf(out_std, std_len, "DDR4");
        else if (is_ddr3) snprintf(out_std, std_len, "DDR3");
        else if (is_ddr) snprintf(out_std, std_len, "DDR");
        else snprintf(out_std, std_len, "unknown");
        return 0;
    }
    return -1;
}

static int detect_dram_speed_sysfs(int *out_mt_s, char *out_std, size_t std_len) {
    /* /sys/devices/system/edac/mc/ shows memory controller info, root-only.
     * Try /sys/class/dmi/id/ for "Type" of memory (slower, less reliable). */
    FILE *fp = fopen("/sys/class/dmi/id/type", "r");
    if (fp) { fclose(fp); /* not useful for speed */ }

    /* Try edac: not always populated, but when it is, it has speed info. */
    /* This is best-effort. If we can't read it, return -1. */
    (void)out_mt_s; (void)out_std; (void)std_len;
    return -1;
}

static void prompt_dram_speed_from_user(void) {
    /* Ask for speed + standard when all detection methods failed. */
    fprintf(stderr, "\n[DRAM] Cannot auto-detect memory speed. Please supply:\n");
    fprintf(stderr, "  Common values: DDR3-1600/1866, DDR4-2400/2666/3200/3600/4000,\n");
    fprintf(stderr, "                  DDR5-4800/5200/5600/6000/6400,\n");
    fprintf(stderr, "                  LPDDR4-3200/4266, LPDDR5-5500/6400\n\n");

    /* Non-TTY fallback: use a conservative mid-range default (DDR4-2666).
     * This is only a last resort — dmidecode/lscpu/sysfs run first and are
     * unaffected. DDR4-2666 = 2666 MT/s = 1333 MHz (DDR transfers twice per
     * clock). Most systems from 2018-2024 fall in the DDR4-2400 to DDR4-3200
     * range, so 2666 is a reasonable mid-point. */
    if (!platform_is_tty()) {
        fprintf(stderr, "[DRAM] WARNING: non-TTY environment, using guessed default DDR4-2666 (2666 MT/s).\n"
                        "              Run interactively for accurate detection.\n");
        global_system_config.dram_speed_mt_s = 2666;
        snprintf(global_system_config.dram_standard, sizeof(global_system_config.dram_standard), "DDR4");
        return;
    }

    int mt_s = platform_prompt_int("  DRAM speed in MT/s (0 if unknown)", 2666, 0, 100000);
    if (mt_s > 0) {
        global_system_config.dram_speed_mt_s = mt_s;
        /* Pick standard from speed range (best guess; user can correct via
         * the next prompt) */
        if (mt_s >= 4800) snprintf(global_system_config.dram_standard, sizeof(global_system_config.dram_standard), "DDR5");
        else if (mt_s >= 3200) snprintf(global_system_config.dram_standard, sizeof(global_system_config.dram_standard), "DDR4/LPDDR5");
        else if (mt_s >= 1600) snprintf(global_system_config.dram_standard, sizeof(global_system_config.dram_standard), "DDR3/DDR4");
        else snprintf(global_system_config.dram_standard, sizeof(global_system_config.dram_standard), "unknown");
        fprintf(stderr, "  Auto-selected standard: %s (edit if wrong)\n",
                global_system_config.dram_standard);
    } else {
        /* User chose to leave unknown */
        snprintf(global_system_config.dram_standard, sizeof(global_system_config.dram_standard), "unknown");
    }
}

void initialize_dram_speed(void) {
    /* If already known (e.g. from HW cross-process cache), skip detection
     * and prompts entirely. */
    if (global_system_config.dram_speed_mt_s > 0) {
        printf("[DRAM] Using cached speed: %d MT/s, %s\n",
               global_system_config.dram_speed_mt_s,
               global_system_config.dram_standard);
        return;
    }

    int mt_s = 0;
    char std[32] = "unknown";

    if (detect_dram_speed_dmidecode(&mt_s, std, sizeof(std)) == 0) {
        printf("[DRAM] Detected via dmidecode: %d MT/s, %s\n", mt_s, std);
    } else if (detect_dram_speed_lscpu(&mt_s, std, sizeof(std)) == 0) {
        printf("[DRAM] Detected via lscpu: %d MT/s, %s (estimate, verify)\n", mt_s, std);
    } else if (detect_dram_speed_sysfs(&mt_s, std, sizeof(std)) == 0) {
        printf("[DRAM] Detected via sysfs: %d MT/s, %s\n", mt_s, std);
    } else {
        fprintf(stderr, "[DRAM] WARNING: automatic detection failed.\n");
        prompt_dram_speed_from_user();
        mt_s = global_system_config.dram_speed_mt_s;
        snprintf(std, sizeof(std), "%s", global_system_config.dram_standard);
    }

    global_system_config.dram_speed_mt_s = mt_s;
    snprintf(global_system_config.dram_standard, sizeof(global_system_config.dram_standard), "%s", std);
    global_system_config.dram_channels = global_system_config.memory_channels;

    /* Compute theoretical bandwidth: bytes/s/channel = MT/s * 8 bytes/transfer.
     * 8 bytes per transfer = 64-bit bus. */
    if (mt_s > 0 && global_system_config.memory_channels > 0) {
        global_system_config.theoretical_bw_mbps =
            (double)mt_s * 8.0 * (double)global_system_config.memory_channels;
    } else {
        global_system_config.theoretical_bw_mbps = 0.0;
    }
}

int get_dram_speed_mt_s(void) {
    if (!global_system_config.detected) {
        initialize_system_config();
    }
    return global_system_config.dram_speed_mt_s;
}

double get_theoretical_bw_mbps(void) {
    if (!global_system_config.detected) {
        initialize_system_config();
    }
    return global_system_config.theoretical_bw_mbps;
}

int get_memory_channels(void) {
    if (!global_system_config.detected) {
        initialize_system_config();
    }
    return global_system_config.memory_channels;
}

int get_cpu_freq_mhz(void) {
    if (!global_system_config.detected) {
        initialize_system_config();
    }
    return global_system_config.cpu_freq_mhz;
}
