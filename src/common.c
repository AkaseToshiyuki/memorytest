/**
 * Memory Benchmark Common Utilities
 *
 * Cache Detection Strategy:
 * 1. sysfs - most reliable on Linux
 * 2. ARM registers - CTR_EL0, CLIDR_EL1
 * 3. x86 CPUID - for Intel/AMD
 * 4. User input - last resort
 */

#include "common.h"
#include <stdarg.h>
#include <libgen.h>
#include <signal.h>
#include <setjmp.h>

#define CACHE_CONFIG_FILE ".cache_config"
#define REPORTS_DIR "reports"

CacheConfig global_cache_config;

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

static size_t probe_cache_size_by_latency(size_t hint_size) {
    size_t test_size = hint_size * 4;
    if (test_size < 1 * MB) test_size = 1 * MB;

    void *ptr = malloc(test_size);
    if (!ptr) return hint_size;

    memset(ptr, 0, test_size);
    volatile uint64_t *p = (volatile uint64_t *)ptr;
    size_t words = test_size / sizeof(uint64_t);

    uint64_t start = get_time_ns();
    volatile uint64_t sum = 0;
    for (size_t i = 0; i < words && i < 10000; i += 16) {
        sum += p[i];
    }
    uint64_t end = get_time_ns();
    uint64_t seq_lat = (end - start) / (10000 / 16);

    start = get_time_ns();
    sum = 0;
    for (size_t i = 0; i < words && i < 10000; i++) {
        size_t idx = (i * 17) % words;
        sum += p[idx];
    }
    end = get_time_ns();
    uint64_t rand_lat = (end - start) / 10000;

    free(ptr);

    if (rand_lat > seq_lat * 10) {
        return hint_size;
    }
    return hint_size;
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

    size_t l1d = 32 * KB;
    size_t l1i = 32 * KB;
    size_t l2 = 0;
    size_t l3 = 0;

    switch (levels) {
        case 3:
            l3 = probe_cache_size_by_latency(4 * MB);
        case 2:
            l2 = probe_cache_size_by_latency(512 * KB);
        case 1:
            l1d = probe_cache_size_by_latency(32 * KB);
            l1i = l1d;
            break;
        default:
            return -1;
    }

    if (l1d == 0) return -1;

    global_cache_config.l1d_size = l1d;
    global_cache_config.l1i_size = l1i;
    global_cache_config.l2_size = (l2 > 0) ? l2 : 512 * KB;
    global_cache_config.l3_size = (l3 > 0) ? l3 : 4 * MB;
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
        global_cache_config.l2_size = (l2 > 0) ? l2 : 512 * KB;
        global_cache_config.l3_size = (l3 > 0) ? l3 : 4 * MB;
        global_cache_config.detected = 1;
        printf("[sysfs] L1D: %zu KB, L1I: %zu KB, L2: %zu KB, L3: %zu KB\n",
               l1d / 1024, global_cache_config.l1i_size / 1024,
               l2 / 1024, l3 / 1024);
        return 0;
    }

    return -1;
}

/* ========== Cache Configuration ========== */
int load_cache_config(void) {
    FILE *fp = fopen(CACHE_CONFIG_FILE, "r");
    if (!fp) return 0;

    if (fscanf(fp, "%zu %zu %zu %zu",
               &global_cache_config.l1d_size,
               &global_cache_config.l1i_size,
               &global_cache_config.l2_size,
               &global_cache_config.l3_size) == 4) {
        global_cache_config.detected = 1;
        fclose(fp);
        return 1;
    }
    fclose(fp);
    return 0;
}

void save_cache_config(void) {
    FILE *fp = fopen(CACHE_CONFIG_FILE, "w");
    if (fp) {
        fprintf(fp, "%zu %zu %zu %zu\n",
                global_cache_config.l1d_size,
                global_cache_config.l1i_size,
                global_cache_config.l2_size,
                global_cache_config.l3_size);
        fclose(fp);
    }
}

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
    char input[256];

    printf("\n");
    printf("========================================\n");
    printf("  Cache detection failed - Manual input required\n");
    printf("========================================\n");
    printf("Please enter your CPU cache configuration:\n");
    printf("(Check /proc/cpuinfo or CPU specs if unsure)\n\n");

    printf("L1 Data cache size (e.g., 32K, 64K): ");
    fflush(stdout);
    if (scanf("%255s", input) == 1) {
        global_cache_config.l1d_size = parse_size(input);
    } else {
        printf("Invalid input, using default 32K\n");
        global_cache_config.l1d_size = 32 * KB;
    }

    printf("L2 cache size (e.g., 256K, 512K, 1M): ");
    fflush(stdout);
    if (scanf("%255s", input) == 1) {
        global_cache_config.l2_size = parse_size(input);
    } else {
        printf("Invalid input, using default 512K\n");
        global_cache_config.l2_size = 512 * KB;
    }

    printf("L3 cache size (e.g., 1M, 4M, 8M, 0 for none): ");
    fflush(stdout);
    if (scanf("%255s", input) == 1) {
        global_cache_config.l3_size = parse_size(input);
    } else {
        printf("Invalid input, using default 4M\n");
        global_cache_config.l3_size = 4 * MB;
    }

    global_cache_config.l1i_size = global_cache_config.l1d_size;
    global_cache_config.detected = 1;

    printf("\nConfiguration saved to " CACHE_CONFIG_FILE "\n");
    printf("Will auto-load on next run\n\n");
}

void initialize_cache_config(void) {
    if (load_cache_config()) {
        printf("[Config] Loaded from cache: L1D=%zuKB, L2=%zuKB, L3=%zuKB\n",
               global_cache_config.l1d_size / KB,
               global_cache_config.l2_size / KB,
               global_cache_config.l3_size / KB);
        return;
    }

    if (detect_cache_sizes()) {
        printf("[Config] Auto-detected: L1D=%zuKB, L1I=%zuKB, L2=%zuKB, L3=%zuKB\n",
               global_cache_config.l1d_size / KB,
               global_cache_config.l1i_size / KB,
               global_cache_config.l2_size / KB,
               global_cache_config.l3_size / KB);
        save_cache_config();
        return;
    }

    printf("[Warning] Cannot auto-detect cache configuration\n");
    prompt_cache_config_from_user();
    save_cache_config();
}

void print_system_info(void) {
    print_header("SYSTEM INFORMATION");

    long pages = sysconf(_SC_PHYS_PAGES);
    long page_size = sysconf(_SC_PAGESIZE);
    printf("Physical Memory: %.2f GB\n", (double)pages * page_size / (1024.0 * MB));
    printf("Available Memory: %.2f GB\n", (double)sysconf(_SC_AVPHYS_PAGES) * page_size / (1024.0 * MB));
    printf("CPU Cores: %ld\n", sysconf(_SC_NPROCESSORS_ONLN));
    printf("Page Size: %ld bytes\n\n", page_size);

    printf("Cache Configuration:\n");
    printf("  L1D: %zu KB\n", global_cache_config.l1d_size / KB);
    printf("  L1I: %zu KB\n", global_cache_config.l1i_size / KB);
    printf("  L2:  %zu KB\n", global_cache_config.l2_size / KB);
    printf("  L3:  %zu KB\n\n", global_cache_config.l3_size / KB);
}

/* ========== Report Generation ========== */
static const char *format_extension(ReportFormat format) {
    switch (format) {
        case REPORT_FORMAT_JSON: return ".json";
        case REPORT_FORMAT_MARKDOWN: return ".md";
        default: return ".txt";
    }
}

ReportContext *report_init(const char *test_name, ReportFormat format) {
    ReportContext *ctx = malloc(sizeof(ReportContext));
    if (!ctx) return NULL;

    strncpy(ctx->test_name, test_name, sizeof(ctx->test_name) - 1);
    ctx->format = format;

    time_t now = time(NULL);
    struct tm *tm_info = localtime(&now);
    strftime(ctx->timestamp, sizeof(ctx->timestamp), "%Y-%m-%d %H:%M:%S", tm_info);

    mkdir(REPORTS_DIR, 0755);

    char filename[256];
    snprintf(filename, sizeof(filename), "%s/%s_report%s",
             REPORTS_DIR, test_name, format_extension(format));

    ctx->fp = fopen(filename, "w");
    if (!ctx->fp) {
        free(ctx);
        return NULL;
    }

    return ctx;
}

void report_write(ReportContext *ctx, const char *fmt, ...) {
    if (!ctx || !ctx->fp) return;

    va_list args;
    va_start(args, fmt);
    vfprintf(ctx->fp, fmt, args);
    va_end(args);
}

void report_section(ReportContext *ctx, const char *title) {
    if (!ctx || !ctx->fp) return;

    switch (ctx->format) {
        case REPORT_FORMAT_MARKDOWN:
            fprintf(ctx->fp, "\n## %s\n\n", title);
            break;
        case REPORT_FORMAT_JSON:
            fprintf(ctx->fp, "  \"section\": \"%s\",\n", title);
            break;
        default:
            fprintf(ctx->fp, "\n%s\n", title);
            for (size_t i = 0; i < strlen(title); i++) fprintf(ctx->fp, "-");
            fprintf(ctx->fp, "\n\n");
    }
}

void report_value(ReportContext *ctx, const char *key, const char *value) {
    if (!ctx || !ctx->fp) return;

    switch (ctx->format) {
        case REPORT_FORMAT_JSON:
            fprintf(ctx->fp, "  \"%s\": \"%s\",\n", key, value);
            break;
        default:
            fprintf(ctx->fp, "  %-20s : %s\n", key, value);
    }
}

void report_latency_table(ReportContext *ctx, const char *headers, const char *rows) {
    if (!ctx || !ctx->fp) return;
    fprintf(ctx->fp, "%s\n%s\n", headers, rows);
}

void report_free(ReportContext *ctx) {
    if (ctx) {
        if (ctx->fp) fclose(ctx->fp);
        free(ctx);
    }
}

const char *report_get_filename(ReportContext *ctx) {
    if (!ctx) return "";
    static char filename[256];
    snprintf(filename, sizeof(filename), "reports/%s_report.md", ctx->test_name);
    return filename;
}

void report_write_system_info(ReportContext *ctx) {
    if (!ctx) return;

    long pages = sysconf(_SC_PHYS_PAGES);
    long page_size = sysconf(_SC_PAGESIZE);
    long num_cpus = sysconf(_SC_NPROCESSORS_ONLN);

    switch (ctx->format) {
        case REPORT_FORMAT_JSON:
            report_write(ctx, "{\n");
            report_write(ctx, "  \"test\": \"%s\",\n", ctx->test_name);
            report_write(ctx, "  \"timestamp\": \"%s\",\n", ctx->timestamp);
            report_write(ctx, "  \"system\": {\n");
            report_write(ctx, "    \"cpu_cores\": %ld,\n", num_cpus);
            report_write(ctx, "    \"memory_total_gb\": %.2f,\n", (double)pages * page_size / (1024.0 * MB));
            report_write(ctx, "    \"memory_available_gb\": %.2f,\n", (double)sysconf(_SC_AVPHYS_PAGES) * page_size / (1024.0 * MB));
            report_write(ctx, "    \"page_size\": %ld,\n", page_size);
            report_write(ctx, "    \"cache_l1_kb\": %zu,\n", global_cache_config.l1d_size / KB);
            report_write(ctx, "    \"cache_l2_kb\": %zu,\n", global_cache_config.l2_size / KB);
            report_write(ctx, "    \"cache_l3_kb\": %zu\n", global_cache_config.l3_size / KB);
            report_write(ctx, "  },\n");
            break;
        case REPORT_FORMAT_MARKDOWN:
            report_write(ctx, "# %s Test Report\n\n", ctx->test_name);
            report_write(ctx, "**Date**: %s\n\n", ctx->timestamp);
            report_write(ctx, "## System Configuration\n\n");
            report_write(ctx, "| Item | Value |\n");
            report_write(ctx, "|------|-------|\n");
            report_write(ctx, "| CPU Cores | %ld |\n", num_cpus);
            report_write(ctx, "| Memory Total | %.2f GB |\n", (double)pages * page_size / (1024.0 * MB));
            report_write(ctx, "| Memory Available | %.2f GB |\n", (double)sysconf(_SC_AVPHYS_PAGES) * page_size / (1024.0 * MB));
            report_write(ctx, "| Page Size | %ld bytes |\n", page_size);
            report_write(ctx, "| L1 Cache | %zu KB |\n", global_cache_config.l1d_size / KB);
            report_write(ctx, "| L2 Cache | %zu KB |\n", global_cache_config.l2_size / KB);
            report_write(ctx, "| L3 Cache | %zu KB |\n", global_cache_config.l3_size / KB);
            break;
        default:
            report_write(ctx, "%s Test Report\n", ctx->test_name);
            report_write(ctx, "Generated: %s\n\n", ctx->timestamp);
            report_write(ctx, "System Configuration:\n");
            report_write(ctx, "  CPU Cores       : %ld\n", num_cpus);
            report_write(ctx, "  Memory Total    : %.2f GB\n", (double)pages * page_size / (1024.0 * MB));
            report_write(ctx, "  Memory Available: %.2f GB\n", (double)sysconf(_SC_AVPHYS_PAGES) * page_size / (1024.0 * MB));
            report_write(ctx, "  Page Size       : %ld bytes\n", page_size);
            report_write(ctx, "  L1 Cache        : %zu KB\n", global_cache_config.l1d_size / KB);
            report_write(ctx, "  L2 Cache        : %zu KB\n", global_cache_config.l2_size / KB);
            report_write(ctx, "  L3 Cache        : %zu KB\n\n", global_cache_config.l3_size / KB);
    }
}

void report_finalize_json(ReportContext *ctx) {
    if (!ctx || !ctx->fp || ctx->format != REPORT_FORMAT_JSON) return;
    fprintf(ctx->fp, "  \"status\": \"completed\"\n");
    fprintf(ctx->fp, "}\n");
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