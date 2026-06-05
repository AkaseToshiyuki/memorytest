/* SPDX-License-Identifier: MIT
 * simd.c - SIMD/Vectorization capability detection (SSE/AVX/NEON/SVE)
 *
 * Split from monolithic common.c (2026-06-02).
 */
#include "common.h"
#include "platform.h"

/* ========== SIMD/Vectorization Detection ========== */

/* Get SIMD info - delegates to the platform layer (which performs actual
 * runtime detection). The platform layer owns the canonical SIMDInfo. */
const SIMDInfo* get_simd_info(void) {
    return platform_simd_detect();
}
