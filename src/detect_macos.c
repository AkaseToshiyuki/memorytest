/* macOS detection: public sysctl/Mach APIs, no sudo/private PMU/register probes. */
#include "common.h"
#include "platform.h"
#include "darwin_helpers.h"
#include <errno.h>
#include <mach/mach.h>
#include <mach/mach_host.h>
#include <inttypes.h>

Architecture current_arch = ARCH_UNKNOWN;
const char *arch_names[] = {"X86_64", "ARM64", "RISC-V", "PowerPC64", "Unknown"};

size_t detect_total_memory(void) { return (size_t)darwin_uint("hw.memsize"); }

size_t detect_available_memory(void) {
    mach_port_t host = mach_host_self();
    vm_size_t page_size = 0;
    vm_statistics64_data_t stats = {0};
    mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
    kern_return_t rc = host_page_size(host, &page_size);
    if (rc == KERN_SUCCESS)
        rc = host_statistics64(host, HOST_VM_INFO64, (host_info64_t)&stats, &count);
    mach_port_deallocate(mach_task_self(), host);
    if (rc != KERN_SUCCESS || !page_size) return 0;
    /* Conservative budget: free + inactive, not compressed/wired/purgeable
     * counts (which can overlap). This is not Linux MemAvailable. */
    uint64_t pages = (uint64_t)stats.free_count + stats.inactive_count;
    if (pages > SIZE_MAX / page_size) return 0;
    size_t available = (size_t)pages * page_size;
    size_t total = detect_total_memory();
    return total && available > total ? total : available;
}

int detect_cache_sizes(void) {
    CacheConfig *c = &global_cache_config;
    memset(c, 0, sizeof(*c));
    c->l1d_size = (size_t)darwin_uint("hw.l1dcachesize");
    c->l1i_size = (size_t)darwin_uint("hw.l1icachesize");
    c->l2_size = (size_t)darwin_uint("hw.l2cachesize");
    c->l3_size = (size_t)darwin_uint("hw.l3cachesize");
    c->l3_total_size = c->l3_size;
    c->l3_ccd_count = c->l3_size ? 1 : 0;
    c->detected = c->l1d_size > 0 && c->l2_size > 0;
    return c->detected;
}

void initialize_cache_config(void) {
    detect_cache_sizes();
    fprintf(stderr, "[macOS] Cache sizes are sysctl summaries; L2 may be cluster-shared.\n"
                    "        Zero means absent/not exposed; no invented L3 or cache totals.\n");
}

int detect_memory_channels(void) { return 0; }
int detect_cpu_freq(void) { return (int)(darwin_uint("hw.cpufrequency") / 1000000); }
void initialize_dram_speed(void) {
    global_system_config.dram_speed_mt_s = 0;
    global_system_config.dram_channels = 0;
    global_system_config.theoretical_bw_mbps = 0;
    snprintf(global_system_config.dram_standard, sizeof(global_system_config.dram_standard), "unknown");
}

void initialize_system_config(void) {
    if (global_system_config.detected) return;
    platform_arch_detect();
    darwin_string("machdep.cpu.brand_string", global_system_config.cpu_model,
                  sizeof(global_system_config.cpu_model));
    if (!global_system_config.cpu_model[0])
        darwin_string("hw.model", global_system_config.cpu_model, sizeof(global_system_config.cpu_model));
    if (!global_system_config.cpu_model[0])
        snprintf(global_system_config.cpu_model, sizeof(global_system_config.cpu_model), "unknown");
    global_system_config.cpu_cores = (int)darwin_uint("hw.logicalcpu");
    global_system_config.cpu_cores_physical = (int)darwin_uint("hw.physicalcpu");
    global_system_config.cpu_freq_mhz = detect_cpu_freq();
    global_system_config.memory_channels = 0;
    initialize_dram_speed();
    global_system_config.detected = 1;
}

void print_system_info(void) {
    initialize_system_config();
    printf("\nSYSTEM INFORMATION (macOS)\nCPU: %s; %d logical / %d physical\n"
           "Physical memory: %.2f GiB; free + inactive: %.2f GiB\n"
           "Frequency/DRAM channels/speed: unknown when not exposed by public APIs.\n"
           "CPU placement: OS scheduled; exact CPU affinity and PMU unavailable.\n",
           global_system_config.cpu_model, global_system_config.cpu_cores,
           global_system_config.cpu_cores_physical,
           (double)detect_total_memory() / GB, (double)detect_available_memory() / GB);
    DarwinPerfLevel levels[DARWIN_MAX_PERF_LEVELS];
    size_t count = darwin_perflevels(levels, DARWIN_MAX_PERF_LEVELS);
    for (size_t i = 0; i < count; ++i)
        printf("perflevel%zu %s: %" PRIu64 " physical / %" PRIu64 " logical; L1D=%" PRIu64
               " KiB, L2=%" PRIu64 " KiB shared by %" PRIu64 " CPUs\n", i, levels[i].name,
               levels[i].physical, levels[i].logical, levels[i].l1d / 1024,
               levels[i].l2 / 1024, levels[i].cpus_per_l2);
}

int set_cpu_affinity(int cpu) { (void)cpu; return ENOTSUP; }
void print_numa_info(void) { puts("NUMA topology: not exposed on macOS"); }
int get_memory_channels(void) { initialize_system_config(); return 0; }
int get_cpu_freq_mhz(void) { initialize_system_config(); return global_system_config.cpu_freq_mhz; }
int get_dram_speed_mt_s(void) { initialize_system_config(); return 0; }
double get_theoretical_bw_mbps(void) { initialize_system_config(); return 0; }
