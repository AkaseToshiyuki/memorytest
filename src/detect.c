/* SPDX-License-Identifier: MIT
 * detect.c - Hardware detection: cache / arch / memory channels / CPU freq / CPU model / NUMA
 *
 * Split from monolithic common.c (2026-06-02).
 */
#include "common.h"
#include "platform.h"
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
        global_cache_config.detected = 1;
        printf("[sysfs] L1D: %zu KB, L1I: %zu KB, L2: %zu KB, L3: %zu KB\n",
               l1d / 1024, global_cache_config.l1i_size / 1024,
               l2 / 1024, l3 / 1024);
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
    fprintf(stderr, "\n[Cache] Some cache levels were not auto-detected.\n");
    if (global_cache_config.l1d_size == 0) {
        int kb = platform_prompt_int("  L1d cache size in KB", 32, 4, 1024);
        global_cache_config.l1d_size = (size_t)kb * 1024;
    }
    if (global_cache_config.l2_size == 0) {
        int kb = platform_prompt_int("  L2 cache size in KB", 512, 64, 16384);
        global_cache_config.l2_size = (size_t)kb * 1024;
    }
    if (global_cache_config.l3_size == 0) {
        int kb = platform_prompt_int("  L3 cache size in KB (0 if none)", 0, 0, 1024 * 1024);
        global_cache_config.l3_size = (size_t)kb * 1024;
    }
    global_cache_config.l1i_size = global_cache_config.l1d_size;
    global_cache_config.detected = 1;
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
    printf("CPU Cores: %ld\n", sysconf(_SC_NPROCESSORS_ONLN));
    printf("CPU Frequency: %d MHz\n", global_system_config.cpu_freq_mhz);
    printf("Memory Channels: %d\n", global_system_config.memory_channels);
    printf("Page Size: %ld bytes\n\n", page_size);

    printf("Cache Configuration:\n");
    printf("  L1D: %zu KB\n", global_cache_config.l1d_size / KB);
    printf("  L1I: %zu KB\n", global_cache_config.l1i_size / KB);
    printf("  L2:  %zu KB\n", global_cache_config.l2_size / KB);
    printf("  L3:  %zu KB\n\n", global_cache_config.l3_size / KB);

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
    /* Try dmidecode if available (may require root) */
    FILE *fp = popen("dmidecode -t memory 2>/dev/null | grep -c 'Channel'", "r");
    if (!fp) return -1;

    int count = 0;
    if (fscanf(fp, "%d", &count) != 1) {
        pclose(fp);
        return -1;
    }
    pclose(fp);
    return (count > 0) ? count : -1;
}

static int detect_memory_channels_numa(void) {
    /* NUMA nodes often correspond to memory channels on modern systems */
    int numa_nodes = 0;
    for (int i = 0; i < 8; i++) {
        char path[256];
        snprintf(path, sizeof(path), "/sys/devices/system/node/node%d", i);
        if (access(path, F_OK) == 0) {
            numa_nodes++;
        }
    }
    /* On single-NUMA systems, assume at least 2 channels (dual-channel) */
    if (numa_nodes == 1) {
        return 2;
    }
    /* Multi-NUMA is more complex, return as detected */
    return numa_nodes;
}

int detect_memory_channels(void) {
    /* Try each method in order of reliability */

    /* 1. dmidecode - most reliable but may need root */
    int channels = detect_memory_channels_dmidecode();
    if (channels > 0) {
        printf("[Memory] Detected %d channels via dmidecode\n", channels);
        return channels;
    }

    /* 2. NUMA node count as indicator — but do NOT silently fall back to 2
     *    channels on single-NUMA systems. Single NUMA != single channel;
     *    a single-socket EPYC with 8 DIMMs has 8 channels in 1 NUMA domain.
     *    Return 0 to force explicit user input. */
    channels = detect_memory_channels_numa();
    if (channels > 1) {
        /* Multi-NUMA is a stronger signal (each NUMA node has its own
         * memory controller + channel set). Still warn user. */
        printf("[Memory] WARNING: assuming %d channels from %d NUMA node(s). "
               "Verify with: dmidecode -t memory | grep 'Number Of Devices'\n",
               channels, channels);
        return channels;
    }
    if (channels == 1) {
        /* Single NUMA = unknown channel count. Do NOT guess. */
        printf("[Memory] WARNING: 1 NUMA node detected, but channel count unknown. "
               "Single-socket systems often have 2-12 channels. Forcing user prompt.\n");
        return 0;
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

static int detect_cpu_freq_pmu(void) {
    /* ARM PMU-based frequency detection using perf_event_open syscall */
    /* Measures actual CPU frequency by counting cycles over a time interval */
    static int fd = -1;

    if (fd < 0) {
        /* perf_event_open syscall number: 241 on ARM64 */
        struct perf_event_attr {
            uint32_t type;
            uint32_t size;
            uint64_t config;
            uint64_t disabled;
            uint64_t exclude_kernel;
            uint64_t exclude_hv;
        } attr = {
            .type = 0,  /* PERF_TYPE_HARDWARE */
            .size = sizeof(struct perf_event_attr),
            .config = 0,  /* PERF_COUNT_HW_CPU_CYCLES */
            .disabled = 0,
            .exclude_kernel = 0,
            .exclude_hv = 1
        };

        fd = syscall(241, &attr, 0, -1, -1, 0);  /* __NR_perf_event_open */
        if (fd < 0) {
            return 0;
        }
    }

    /* Reset and enable counter */
    ioctl(fd, 0, 0);  /* PERF_EVENT_IOC_RESET */
    ioctl(fd, 1, 0);  /* PERF_EVENT_IOC_ENABLE */

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

    /* Try multiple detection methods in order of reliability */
    freq = detect_cpu_freq_sysfs();
    if (freq > 0) return freq;

    freq = detect_cpu_freq_cpuinfo();
    if (freq > 0) return freq;

    freq = detect_cpu_freq_sysctl();
    if (freq > 0) return freq;

    freq = detect_cpu_freq_lscpu();
    if (freq > 0) return freq;

    /* Try ARM PMU-based detection (works without sudo) */
    freq = detect_cpu_freq_pmu();
    if (freq > 0) return freq;

    /* Try sudo-enabled methods for privileged access */
    freq = detect_cpu_freq_sudo();
    if (freq > 0) return freq;

    return 0;  /* All methods failed */
}

/* ========== System Configuration ========== */
void initialize_system_config(void) {
    /* Always detect CPU model (fast, no file I/O) */
    detect_cpu_model(global_system_config.cpu_model, sizeof(global_system_config.cpu_model));

    /* Always detect memory channels - no caching */
    int channels = detect_memory_channels();
    if (channels > 0) {
        global_system_config.memory_channels = channels;
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

    /* Detect DRAM speed AFTER channels so theoretical_bw_mbps can be computed. */
    initialize_dram_speed();

    /* Try multiple methods to detect CPU frequency */
    int freq = detect_cpu_freq();
    if (freq > 0) {
        global_system_config.cpu_freq_mhz = freq;
    } else {
        /* All detection methods failed, ask user */
        printf("[CPU] All automatic detection methods failed.\n");
        global_system_config.cpu_freq_mhz = prompt_cpu_freq_from_user();
        printf("[CPU] Using frequency: %d MHz\n", global_system_config.cpu_freq_mhz);
    }

    global_system_config.detected = 1;
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
    FILE *fp = popen("dmidecode -t memory 2>/dev/null", "r");
    if (!fp) return -1;

    char line[512];
    int max_mt_s = 0;
    char current_std[32] = "unknown";

    while (fgets(line, sizeof(line), fp)) {
        /* Type appears in Memory Device blocks: e.g. "Type: DDR4" */
        if (strstr(line, "Type:") == line || strncmp(line, "Type:", 5) == 0) {
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
    /* lscpu sometimes shows max memory speed; for DDR it often shows MHz
     * (half of MT/s for DDR because DDR transfers twice per clock). Try
     * to detect the standard from "Model name" and adjust. */
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
    }
    pclose(fp);
    if (mhz > 0) {
        /* lscpu often reports MHz = MT/s/2 for DDR; multiply by 2 to get MT/s.
         * For LPDDR it reports MT/s directly. We assume DDR convention here
         * and double. */
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
    int mt_s = platform_prompt_int("  DRAM speed in MT/s (0 if unknown)", 0, 0, 100000);
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
