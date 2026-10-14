/* Public Windows APIs only; unknown DRAM/clock values are never invented. */
#include "common.h"
#include "platform.h"
Architecture current_arch = ARCH_UNKNOWN;
const char *arch_names[] = {"X86_64", "ARM64", "RISC-V", "PowerPC64", "Unknown"};
static MEMORYSTATUSEX memory_status(void) {
    MEMORYSTATUSEX value = {.dwLength = sizeof(value)};
    GlobalMemoryStatusEx(&value);
    return value;
}
size_t detect_total_memory(void) { return (size_t)memory_status().ullTotalPhys; }
size_t detect_available_memory(void) { return (size_t)memory_status().ullAvailPhys; }
int detect_cache_sizes(void) {
    CacheConfig *c = &global_cache_config;
    memset(c, 0, sizeof(*c));
    DWORD bytes = 0;
    GetLogicalProcessorInformationEx(RelationCache, NULL, &bytes);
    if (!bytes) return 0;
    unsigned char *buffer = malloc(bytes);
    if (!buffer) return 0;
    if (GetLogicalProcessorInformationEx(RelationCache, (void *)buffer, &bytes)) {
        for (DWORD offset = 0; offset < bytes;) {
            SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX *entry = (void *)(buffer + offset);
            if (!entry->Size || entry->Size > bytes - offset) break;
            CACHE_RELATIONSHIP *cache = &entry->Cache;
            if (cache->Level == 1 && cache->Type == CacheData && cache->CacheSize > c->l1d_size) c->l1d_size = cache->CacheSize;
            if (cache->Level == 1 && cache->Type == CacheInstruction && cache->CacheSize > c->l1i_size) c->l1i_size = cache->CacheSize;
            if (cache->Level == 2 && cache->CacheSize > c->l2_size) c->l2_size = cache->CacheSize;
            if (cache->Level == 3) {
                if (cache->CacheSize > c->l3_size) c->l3_size = cache->CacheSize;
                c->l3_total_size += cache->CacheSize;
                ++c->l3_ccd_count;
            }
            offset += entry->Size;
        }
    }
    free(buffer);
    c->detected = c->l1d_size > 0 && c->l2_size > 0;
    return c->detected;
}
void initialize_cache_config(void) { detect_cache_sizes(); }
int detect_memory_channels(void) { return 0; }
int detect_cpu_freq(void) { return 0; }
void initialize_dram_speed(void) {}
void initialize_system_config(void) {
    if (global_system_config.detected) return;
    platform_arch_detect();
    windows_cpu_model(global_system_config.cpu_model, sizeof(global_system_config.cpu_model));
    global_system_config.cpu_cores = (int)GetActiveProcessorCount(ALL_PROCESSOR_GROUPS);
    global_system_config.detected = 1;
}
void print_system_info(void) {
    initialize_system_config();
    printf("Windows CPU: %s; %d system logical CPUs; %.2f GiB RAM\n"
           "Cache sizes: largest OS-reported instance per level. DRAM speed/channels and PMU: unknown.\n",
           global_system_config.cpu_model, global_system_config.cpu_cores, (double)detect_total_memory() / GB);
}
int set_cpu_affinity(int cpu) { return windows_bind_cpu(cpu); }
void print_numa_info(void) { puts("NUMA placement: not controlled by this backend"); }
int get_memory_channels(void) { return 0; }
int get_cpu_freq_mhz(void) { return 0; }
int get_dram_speed_mt_s(void) { return 0; }
double get_theoretical_bw_mbps(void) { return 0; }
