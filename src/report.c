/* SPDX-License-Identifier: MIT
 * report.c - Report generation framework (text/JSON/Markdown)
 *
 * Split from monolithic common.c (2026-06-02).
 */
#include "common.h"
#include <stddef.h>
#include <stdarg.h>
#include <string.h>

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
    if (!global_system_config.detected) {
        initialize_system_config();
    }

    long pages = sysconf(_SC_PHYS_PAGES);
    long page_size = sysconf(_SC_PAGESIZE);
    long num_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    /* Prefer the fields populated by initialize_system_config(); fall back to
     * sysconf only if the detect layer somehow never ran. */
    int cpu_cores_log = global_system_config.cpu_cores > 0
                            ? global_system_config.cpu_cores
                            : (int)num_cpus;
    int cpu_cores_phys = global_system_config.cpu_cores_physical;  /* 0 = unknown */

    switch (ctx->format) {
        case REPORT_FORMAT_JSON:
            report_write(ctx, "{\n");
            report_write(ctx, "  \"test\": \"%s\",\n", ctx->test_name);
            report_write(ctx, "  \"timestamp\": \"%s\",\n", ctx->timestamp);
            report_write(ctx, "  \"system\": {\n");
            report_write(ctx, "    \"cpu_model\": \"%s\",\n", global_system_config.cpu_model);
            report_write(ctx, "    \"cpu_cores_logical\": %d,\n", cpu_cores_log);
            report_write(ctx, "    \"cpu_cores_physical\": %d,\n", cpu_cores_phys);
            report_write(ctx, "    \"cpu_freq_mhz\": %d,\n", global_system_config.cpu_freq_mhz);
            report_write(ctx, "    \"memory_channels\": %d,\n", global_system_config.memory_channels);
            report_write(ctx, "    \"memory_total_gb\": %.2f,\n", (double)pages * page_size / (1024.0 * MB));
            report_write(ctx, "    \"memory_available_gb\": %.2f,\n", (double)sysconf(_SC_AVPHYS_PAGES) * page_size / (1024.0 * MB));
            report_write(ctx, "    \"page_size\": %ld,\n", page_size);
            /* Cache sizes: per-core, with explicit total (per_core × physical) */
            report_write(ctx, "    \"cache_l1d_kb_per_core\": %zu,\n", global_cache_config.l1d_size / KB);
            if (cpu_cores_phys > 0) {
                report_write(ctx, "    \"cache_l1d_kb_total\": %zu,\n",
                             (global_cache_config.l1d_size / KB) * (size_t)cpu_cores_phys);
            }
            report_write(ctx, "    \"cache_l2_kb_per_core\": %zu,\n", global_cache_config.l2_size / KB);
            if (cpu_cores_phys > 0) {
                report_write(ctx, "    \"cache_l2_kb_total\": %zu,\n",
                             (global_cache_config.l2_size / KB) * (size_t)cpu_cores_phys);
            }
            report_write(ctx, "    \"cache_l3_kb\": %zu\n", global_cache_config.l3_size / KB);
            report_write(ctx, "  },\n");
            break;
        case REPORT_FORMAT_MARKDOWN:
            report_write(ctx, "# %s Test Report\n\n", ctx->test_name);
            report_write(ctx, "**Date**: %s\n\n", ctx->timestamp);
            report_write(ctx, "## System Configuration\n\n");
            report_write(ctx, "| Item | Value |\n");
            report_write(ctx, "|------|-------|\n");
            report_write(ctx, "| CPU Model | %s |\n", global_system_config.cpu_model);
            if (cpu_cores_phys > 0 && cpu_cores_phys != cpu_cores_log) {
                report_write(ctx, "| CPU Cores | %d logical (%d physical, HT/SMT) |\n",
                             cpu_cores_log, cpu_cores_phys);
            } else if (cpu_cores_phys > 0) {
                report_write(ctx, "| CPU Cores | %d (no HT/SMT) |\n", cpu_cores_phys);
            } else {
                report_write(ctx, "| CPU Cores | %d |\n", cpu_cores_log);
            }
            report_write(ctx, "| CPU Frequency | %d MHz |\n", global_system_config.cpu_freq_mhz);
            /* Cache numbers are per-core; show aggregate. Use physical when
             * known (x86 with SMT), otherwise fall back to logical (ARM/older
             * kernels without siblings line). */
            int cc_for_agg = (cpu_cores_phys > 0) ? cpu_cores_phys : cpu_cores_log;
            if (global_cache_config.l1d_size > 0 && cc_for_agg > 0) {
                report_write(ctx, "| L1 Cache (D) | %zu KB per core, %zu KB total (%d cores) |\n",
                             global_cache_config.l1d_size / KB,
                             (global_cache_config.l1d_size / KB) * (size_t)cc_for_agg,
                             cc_for_agg);
            } else {
                report_write(ctx, "| L1 Cache | %zu KB |\n", global_cache_config.l1d_size / KB);
            }
            if (global_cache_config.l2_size > 0 && cc_for_agg > 0) {
                report_write(ctx, "| L2 Cache | %zu KB per core, %zu KB total (%d cores) |\n",
                             global_cache_config.l2_size / KB,
                             (global_cache_config.l2_size / KB) * (size_t)cc_for_agg,
                             cc_for_agg);
            } else {
                report_write(ctx, "| L2 Cache | %zu KB |\n", global_cache_config.l2_size / KB);
            }
            if (global_cache_config.l3_size > 0) {
                report_write(ctx, "| L3 Cache | %zu KB (shared) |\n", global_cache_config.l3_size / KB);
            } else {
                report_write(ctx, "| L3 Cache | %zu KB |\n", global_cache_config.l3_size / KB);
            }
            report_write(ctx, "| Memory Channels | %d |\n", global_system_config.memory_channels);
            report_write(ctx, "| Memory Total | %.2f GB |\n", (double)pages * page_size / (1024.0 * MB));
            report_write(ctx, "| Memory Available | %.2f GB |\n", (double)sysconf(_SC_AVPHYS_PAGES) * page_size / (1024.0 * MB));
            report_write(ctx, "| Page Size | %ld bytes |\n", page_size);
            break;
        default:
            report_write(ctx, "%s Test Report\n", ctx->test_name);
            report_write(ctx, "Generated: %s\n\n", ctx->timestamp);
            report_write(ctx, "System Configuration:\n");
            report_write(ctx, "  CPU Model       : %s\n", global_system_config.cpu_model);
            report_write(ctx, "  CPU Cores       : %ld\n", num_cpus);
            report_write(ctx, "  CPU Frequency   : %d MHz\n", global_system_config.cpu_freq_mhz);
            report_write(ctx, "  Memory Channels : %d\n", global_system_config.memory_channels);
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
