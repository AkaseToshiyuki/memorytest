#define _GNU_SOURCE
#ifndef _DARWIN_C_SOURCE
#define _DARWIN_C_SOURCE 1
#endif
#include "common.h"
#include "util.h"
#include "platform.h"
#include "architecture_kernels.h"
#ifndef BENCH_TARGET_NS
#define BENCH_TARGET_NS UINT64_C(50000000)
#endif
#ifndef BENCH_WARMUP_NS
#define BENCH_WARMUP_NS UINT64_C(10000000)
#endif
#define BENCH_DEFAULT_SAMPLES 10
#include "bench_protocol.h"
#include "memory_latency.h"
#include <errno.h>
#ifdef __linux__
#include <sched.h>
#endif
#ifdef __APPLE__
#include "darwin_helpers.h"
#include <pthread/qos.h>
#endif

#ifndef ARCH_KERNEL_ISA
int main(void) {
    fputs("UNSUPPORTED: architecture instruction probes require native x86-64 or AArch64.\n", stderr);
    return 77;
}
#else
#include "architecture_frontend.h"
#include "architecture_widths.h"
#include "architecture_btb_layout.h"
#include "architecture_resources.h"
#include "architecture_competition.h"
enum { ARCH_MEASUREMENTS = 12 };
static const char *arch_names_v1[] = {"nop_stream", "iadd_dep", "iadd_ind8", "imul_dep", "imul_ind8",
    "fadd_dep", "fadd_ind8", "fmul_dep", "fmul_ind8", "load_chain_32k", "write_ordered_32k", "copy_ordered_64k"};
typedef struct {
    unsigned kind;
    uint64_t integer[8];
    double floating[8];
    MemoryLatency memory;
} ArchitectureWork;
static volatile uint64_t arch_integer_sink;
static volatile double arch_float_sink;
static int architecture_work(void *context, uint64_t blocks) {
    ArchitectureWork *work = context;
    switch (work->kind) {
    case 0: arch_nop(blocks); break;
    case 1: arch_iadd_dep(blocks, work->integer); break;
    case 2: arch_iadd_ind8(blocks, work->integer); break;
    case 3: arch_imul_dep(blocks, work->integer); break;
    case 4: arch_imul_ind8(blocks, work->integer); break;
    case 5: arch_fadd_dep(blocks, work->floating); break;
    case 6: arch_fadd_ind8(blocks, work->floating); break;
    case 7: arch_fmul_dep(blocks, work->floating); break;
    case 8: arch_fmul_ind8(blocks, work->floating); break;
    default: return memory_latency_work(&work->memory, blocks);
    }
    arch_integer_sink = work->integer[0];
    arch_float_sink = work->floating[0];
    return 0;
}
static int architecture_validate(ArchitectureWork *work, uint64_t blocks) {
    unsigned kind = work->kind;
    if (!kind) return 0;
    if (kind >= 9) return memory_latency_validate(&work->memory);
    int parallel = kind % 2 == 0;
    uint64_t steps = blocks * (parallel ? 4 : 32);
    for (unsigned i = 0; i < 8; ++i) {
        uint64_t n = parallel || !i ? steps : 0;
        if (kind <= 4) {
            uint64_t expected = kind <= 2 ? i + 1 + 3 * n : (i + 1) * arch_pow_u64(3, n);
            if (work->integer[i] != expected) return -1;
        } else {
            double expected = kind <= 6 ? i + 1 + .25 * n : (i + 1) * pow(1.0 + 0x1p-32, (double)n);
            if (!isfinite(work->floating[i]) || fabs(work->floating[i] - expected) > expected * 1e-6) return -1;
        }
    }
    return 0;
}
static int architecture_placement(int *cpu, const char **policy) {
    *cpu = -1;
#ifdef __linux__
    cpu_set_t allowed, wanted, verified;
    if (sched_getaffinity(0, sizeof(allowed), &allowed)) return -1;
    for (int i = 0; i < CPU_SETSIZE; ++i) if (CPU_ISSET(i, &allowed)) { *cpu = i; break; }
    if (*cpu < 0) return -1;
    CPU_ZERO(&wanted); CPU_SET(*cpu, &wanted);
    if (sched_setaffinity(0, sizeof(wanted), &wanted) || sched_getaffinity(0, sizeof(verified), &verified) ||
        CPU_COUNT(&verified) != 1 || !CPU_ISSET(*cpu, &verified) || sched_getcpu() != *cpu) return -1;
    *policy = "linux-pinned-one-logical-cpu";
#elif defined(__APPLE__)
    if (darwin_uint("sysctl.proc_translated")) return -1;
    if (pthread_set_qos_class_self_np(QOS_CLASS_USER_INITIATED, 0)) return -1;
    qos_class_t qos; int priority;
    if (pthread_get_qos_class_np(pthread_self(), &qos, &priority) || qos != QOS_CLASS_USER_INITIATED || priority) return -1;
    *policy = "macos-qos-user-initiated";
#else
    *policy = "os-scheduled-single-thread";
#endif
    return 0;
}
/* This diagram must never reuse the legacy detector's guessed fallback or
 * copied L1D-as-L1I values. Only explicitly exposed OS cache entries qualify. */
static void architecture_cache_metadata(int cpu) {
    memset(&global_cache_config, 0, sizeof(global_cache_config));
#ifdef __linux__
    for (unsigned index = 0; index < 32; ++index) {
        char path[160], type[32], suffix = 0;
        unsigned level;
        unsigned long long size;
        snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/cache/index%u/level", cpu, index);
        FILE *file = fopen(path, "r");
        if (!file) continue;
        int valid = fscanf(file, "%u", &level) == 1;
        fclose(file);
        if (!valid) continue;
        snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/cache/index%u/type", cpu, index);
        file = fopen(path, "r");
        if (!file) continue;
        valid = fscanf(file, "%31s", type) == 1;
        fclose(file);
        if (!valid) continue;
        snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/cache/index%u/size", cpu, index);
        file = fopen(path, "r");
        if (!file) continue;
        valid = fscanf(file, "%llu%c", &size, &suffix) == 2 && suffix == 'K' && size <= SIZE_MAX / 1024;
        fclose(file);
        if (!valid) continue;
        size_t bytes = (size_t)size * 1024;
        if (level == 1 && !strcmp(type, "Instruction")) global_cache_config.l1i_size = bytes;
        if (level == 1 && !strcmp(type, "Data")) global_cache_config.l1d_size = bytes;
        if (level == 2 && !strcmp(type, "Unified")) global_cache_config.l2_size = bytes;
        if (level == 3 && !strcmp(type, "Unified")) global_cache_config.l3_size = bytes;
    }
#else
    (void)cpu;
    detect_cache_sizes(); /* macOS sysctl / Windows RelationCache; no guesses. */
#endif
}
static void architecture_cache_instances(FILE *json) {
    fputs(",\"cache_instances\":[", json);
#ifdef __APPLE__
    DarwinPerfLevel levels[DARWIN_MAX_PERF_LEVELS];
    size_t count = darwin_perflevels(levels, DARWIN_MAX_PERF_LEVELS);
    for (size_t i = 0; i < count; ++i) {
        fprintf(json, "%s{\"level_id\":%zu,\"name\":\"", i ? "," : "", i);
        for (const unsigned char *p = (const unsigned char *)levels[i].name; *p; ++p) {
            if (*p == '"' || *p == '\\') fprintf(json, "\\%c", *p);
            else if (*p < 32) fprintf(json, "\\u%04x", *p);
            else fputc(*p, json);
        }
        fprintf(json, "\",\"logical_cpus\":%llu,\"l1i\":%llu,\"l1d\":%llu,\"l2\":%llu,\"cpus_per_l2\":%llu}",
            (unsigned long long)levels[i].logical, (unsigned long long)levels[i].l1i,
            (unsigned long long)levels[i].l1d, (unsigned long long)levels[i].l2, (unsigned long long)levels[i].cpus_per_l2);
    }
#endif
    fputs("],\"sampled_cache_level_id\":null", json);
}
int main(int argc, char **argv) {
    (void)argv;
    if (argc != 1) return 2;
    platform_init();
    initialize_system_config();
    int cpu;
    const char *placement;
    if (architecture_placement(&cpu, &placement)) {
        fputs("UNSUPPORTED: native instruction-probe placement could not be established.\n", stderr); return 77;
    }
    architecture_cache_metadata(cpu);
    _Alignas(128) unsigned char source[32 * KB], destination[32 * KB];
    BenchMeasurement measurements[ARCH_MEASUREMENTS];
    for (unsigned kind = 0; kind < ARCH_MEASUREMENTS; ++kind) {
        ArchitectureWork work = {.kind = kind, .memory = {{source, sizeof(source), (uintptr_t)source}, destination, kind - 9}};
        if (kind >= 9) {
            memory_latency_initialize(&work.memory);
            if (memory_latency_work(&work.memory, sizeof(source) / 64 / CHAIN_STEPS)) return 1;
        }
        printf("Architecture probe: %s\n", arch_names_v1[kind]); fflush(stdout);
        if (bench_measure(architecture_work, &work, &measurements[kind]) ||
            architecture_validate(&work, measurements[kind].repetitions)) return 1;
    }
#ifdef __linux__
    if (sched_getcpu() != cpu) return 1;
#endif
    ReportContext *report = report_init("cpu_architecture", REPORT_FORMAT_MARKDOWN);
    if (!report) return 1;
    FILE *json = fopen(REPORTS_DIR "/cpu_architecture.json", "w");
    if (!json) { report_free(report); return 1; }
    report_write_system_info(report);
    report_section(report, "Architecture Probe Conditions");
    report_write(report, "- Protocol: architecture-probes-v4\n- Placement: %s\n- CPU ID: %d\n- ISA: " ARCH_KERNEL_ISA "\n", placement, cpu);
    bench_report_conditions(report);
    report_write(report, "- Instruction count is explicit payload work, NOT retired instructions from a PMU.\n"
        "- The twelve instruction/memory probes use wall time, including loop and result retention overhead.\n"
        "- Ordered write/copy include address loads and full fences. Working sets are 32/32/64 KiB, not guaranteed L1 hits.\n"
        "- Backend resource scans measure available speculative windows. Mac QoS does not select a P/E core.\n\n");
    report_write(report, "- Cache metadata: Linux uses the pinned CPU's sysfs entries; macOS uses global sysctl summaries plus unassociated performance levels; "
        "Windows uses the largest OS-reported instance per level. Shared caches are not per-core capacities.\n\n");
    report_section(report, "Architecture Measurements");
    report_write(report, "| Probe | ns/op | Operations | Samples | Status |\n|---|---|---|---|---|\n");
    fprintf(json, "{\"schema_version\":4,\"protocol\":\"architecture-probes-v4\",\"isa\":\"" ARCH_KERNEL_ISA "\","
        "\"placement\":\"%s\",\"cpu_id\":%d,\"sample_count\":%u,\"sample_target_ns\":%llu,"
        "\"timer\":\"monotonic-wall-time\",\"cache_source\":\"OS-reported-not-probed\","
        "\"reported_cache_bytes\":{\"l1i\":%zu,\"l1d\":%zu,\"l2\":%zu,\"l3\":%zu},\"records\":[",
        placement, cpu, BENCH_SAMPLES, (unsigned long long)BENCH_TARGET_NS, global_cache_config.l1i_size,
        global_cache_config.l1d_size, global_cache_config.l2_size, global_cache_config.l3_size);
    for (unsigned kind = 0; kind < ARCH_MEASUREMENTS; ++kind) {
        BenchMeasurement *sample = &measurements[kind];
        unsigned per_block = !kind ? 64 : kind < 9 ? 32 : CHAIN_STEPS;
        uint64_t operations = sample->repetitions * per_block;
        report_write(report, "| %s | %.9f | %llu | %u | %s |\n", arch_names_v1[kind], sample->elapsed_ns / operations,
                     (unsigned long long)operations, BENCH_SAMPLES, sample->statistics.unstable ? "unstable" : "measured");
        fprintf(json, "%s{\"probe\":\"%s\",\"blocks\":%llu,\"operations_per_block\":%u,\"validated\":true,\"elapsed_ns\":[",
                kind ? "," : "", arch_names_v1[kind], (unsigned long long)sample->repetitions, per_block);
        for (unsigned i = 0; i < BENCH_SAMPLES; ++i)
            fprintf(json, "%s%llu", i ? "," : "", (unsigned long long)sample->samples[i]);
        fputs("],\"statistics\":", json); sample_statistics_json(json, &sample->statistics); fputc('}', json);
    }
    fputs("]", json);
    architecture_cache_instances(json);
    int failed = frontend_report(json, report) || width_report(json, report) || ferror(json) ||
        btb_layout_report(json, report) || resource_report(json, report) || competition_report(json, report);
    fputs("}\n", json);
    if (fclose(json)) failed = 1;
    if (report_finish(report)) failed = 1;
    return failed;
}
#endif
