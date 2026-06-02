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

    switch (ctx->format) {
        case REPORT_FORMAT_JSON:
            report_write(ctx, "{\n");
            report_write(ctx, "  \"test\": \"%s\",\n", ctx->test_name);
            report_write(ctx, "  \"timestamp\": \"%s\",\n", ctx->timestamp);
            report_write(ctx, "  \"system\": {\n");
            report_write(ctx, "    \"cpu_model\": \"%s\",\n", global_system_config.cpu_model);
            report_write(ctx, "    \"cpu_cores\": %ld,\n", num_cpus);
            report_write(ctx, "    \"cpu_freq_mhz\": %d,\n", global_system_config.cpu_freq_mhz);
            report_write(ctx, "    \"memory_channels\": %d,\n", global_system_config.memory_channels);
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
            report_write(ctx, "| CPU Model | %s |\n", global_system_config.cpu_model);
            report_write(ctx, "| CPU Cores | %ld |\n", num_cpus);
            report_write(ctx, "| CPU Frequency | %d MHz |\n", global_system_config.cpu_freq_mhz);
            report_write(ctx, "| Memory Channels | %d |\n", global_system_config.memory_channels);
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
