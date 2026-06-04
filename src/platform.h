/* SPDX-License-Identifier: MIT
 * platform.h - Cross-platform runtime detection (arch / simd / cache / mem / freq / pmu)
 *
 * Replaces compile-time-only SIMD detection in common.h. Detects at runtime
 * which ISA extensions are actually available on the running CPU, regardless
 * of how the binary was compiled. Falls back to user interaction on TTY
 * when static detection fails, or to safe defaults on non-TTY.
 *
 * The point: a binary compiled with -O2 (no -march=native) should still
 * run correctly on x86_64 / arm64 / riscv64 / ppc64 and use the best
 * available SIMD. No -march=native required.
 */
#ifndef PLATFORM_H
#define PLATFORM_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "common.h"   /* for Architecture enum, SIMDInfo */

/* ============================================================
 * 1. Architecture & SIMD
 * ============================================================ */

/* Detect the running architecture. result is also written to
 * current_arch (extern in common.h). Returns the same value. */
Architecture platform_arch_detect(void);

/* Detect actual SIMD capabilities at runtime.
 * x86: parses CPUID (no -march required)
 * arm: ID_AA64PFR0_EL1 / id_aa64isar0_el1
 * riscv: misa.V + vlenb
 * ppc: __builtin_cpu_supports("altivec"/"vsx")
 * Result is cached in the same SIMDInfo struct returned by get_simd_info(). */
const SIMDInfo *platform_simd_detect(void);

/* Specific predicates for use in #if-avoiding code paths. */
bool platform_has_sse2(void);
bool platform_has_avx(void);
bool platform_has_avx2(void);
bool platform_has_avx512(void);
bool platform_has_fma(void);
bool platform_has_neon(void);
bool platform_has_sve(void);
bool platform_has_rvv(void);
bool platform_has_altivec(void);
bool platform_has_vsx(void);

/* Best-available vector width in bits, fallback 128. */
int platform_vector_width(void);

/* ============================================================
 * 2. Cache detection (runtime probe)
 * ============================================================ */

/* Probe L1d / L2 / L3 sizes by latency. Result is in bytes.
 * If static info is available (sysfs on Linux, CPUID cache leaves on x86,
 * CTR_EL0/CLIDR_EL1 on arm), it is used as a fast path. Otherwise we fall
 * back to a latency-binned probe.
 *
 * `user_known` is the cached config from previous runs (or all-zero on first run).
 * On TTY with detection failure, the user is asked; on non-TTY, the safe default
 * is used.
 *
 * Return: 0 on success, -1 if defaults were used.
 */
int platform_cache_detect(CacheConfig *cfg);

/* ============================================================
 * 3. Memory channel detection
 * ============================================================ */

/* Detect memory channels from sysfs + NUMA topology.
 * On failure, asks the user on TTY or uses a safe default on non-TTY.
 *
 * `user_known` is the cached config from previous runs.
 * Return: detected channel count (>= 1). */
int platform_mem_channels_detect(int user_known);

/* ============================================================
 * 4. CPU frequency
 * ============================================================ */

/* Detect CPU base frequency in MHz from sysfs, /proc/cpuinfo, lscpu, or
 * ARM PMU cycles. Asks user on TTY on failure; safe default on non-TTY.
 *
 * `user_known` is the cached config from previous runs.
 * Return: detected freq in MHz (>= 1). */
int platform_cpu_freq_detect(int user_known);

/* ============================================================
 * 5. PMU (perf_event_open) availability
 * ============================================================ */

typedef struct {
    bool available;          /* true if any perf counter can be opened */
    int paranoia;            /* /proc/sys/kernel/perf_event_paranoid (0..4) */
    bool can_profile_self;   /* perf_event_open(PERF_TYPE_SOFTWARE) always works */
    bool can_profile_hw;     /* hardware counters work (paranoia <= 2) */
} PMUCapabilities;

/* Probe PMU by actually trying to open a perf_event fd and closing it.
 * This is the *correct* check: paranoia may be 4 even if cpuid has PMU.
 * Caches result on first call. */
const PMUCapabilities *platform_pmu_probe(void);

/* ============================================================
 * 6. Interactive helpers
 * ============================================================ */

/* True if stdin is a TTY (we can ask the user).
 * Cached on first call. */
bool platform_is_tty(void);

/* Force the platform layer to skip user interaction. Useful for
 * non-interactive benchmarks (CI, smoke tests). When `force_non_interactive`
 * is set, all platform_detect_* functions return safe defaults without
 * asking. */
void platform_set_non_interactive(bool force);

/* Force the platform layer to skip sudo entirely (no password prompt).
 * Detection that requires sudo returns defaults. */
void platform_set_no_sudo(bool no_sudo);

/* Interactive prompt for an integer in [lo, hi]. Public so detect.c
 * and other modules can use the same prompting convention.
 * - On TTY: prints `question [default_value]: `, reads a line, parses int.
 *   Empty input → default. Out-of-range → default.
 * - On non-TTY (or when platform_set_non_interactive(true) was called):
 *   returns default_value without reading stdin. */
int platform_prompt_int(const char *question, int default_value, int lo, int hi);

/* ============================================================
 * 7. Init / shutdown
 * ============================================================ */

/* Run all platform detection. Call once at program start.
 * Idempotent. Subsequent calls return cached results.
 * After this returns, you can use the typed predicates above safely. */
void platform_init(void);

/* Cleanup (free any allocated buffers). */
void platform_shutdown(void);

#endif /* PLATFORM_H */
