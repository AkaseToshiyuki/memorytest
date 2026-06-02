/* SPDX-License-Identifier: MIT
 * simd.c - SIMD/Vectorization capability detection (SSE/AVX/NEON/SVE)
 *
 * Split from monolithic common.c (2026-06-02).
 */
#include "common.h"
#include <stddef.h>
#include <string.h>

/* ========== SIMD/Vectorization Detection ========== */
static SIMDInfo simd_info = {{0}, 0};

static void detect_simd_capabilities(void) {
    memset(&simd_info, 0, sizeof(simd_info));

#if defined(__x86_64__) || defined(__i386__)
    /* x86/x86_64: Detect SIMD via CPUID */
    unsigned int eax, ebx, ecx, edx;

    /* Check max CPUID level */
    __asm__ volatile("cpuid" : "=a"(eax), "=b"(ebx), "=c"(ecx), "=d"(edx) : "a"(0));
    unsigned int max_level = eax;

    /* Check for SSE */
    if (max_level >= 1) {
        __asm__ volatile("cpuid" : "=a"(eax), "=b"(ebx), "=c"(ecx), "=d"(edx) : "a"(1));
        simd_info.has_sse = (edx >> 25) & 1;
        simd_info.has_sse2 = (edx >> 26) & 1;
        simd_info.has_sse3 = (ecx >> 0) & 1;
        simd_info.has_ssse3 = (ecx >> 9) & 1;
        simd_info.has_sse4_1 = (ecx >> 19) & 1;
        simd_info.has_sse4_2 = (ecx >> 20) & 1;

        /* AVX detection */
        simd_info.has_avx = (ecx >> 28) & 1;

        /* AVX2 detection (requires leaf 7) */
        if (max_level >= 7) {
            __asm__ volatile("cpuid" : "=a"(eax), "=b"(ebx), "=c"(ecx), "=d"(edx) : "a"(7), "c"(0));
            simd_info.has_avx2 = (ebx >> 5) & 1;

            /* AVX-512 detection */
            simd_info.has_avx512 = (ebx >> 16) & 1;
        }
    }

    /* Build flags string */
    char *p = simd_info.simd_flags;
    if (simd_info.has_sse) { strcpy(p, "SSE"); p += 4; }
    if (simd_info.has_sse2) { strcpy(p, " SSE2"); p += 5; }
    if (simd_info.has_sse3) { strcpy(p, " SSE3"); p += 5; }
    if (simd_info.has_ssse3) { strcpy(p, " SSSE3"); p += 6; }
    if (simd_info.has_sse4_1) { strcpy(p, " SSE4.1"); p += 7; }
    if (simd_info.has_sse4_2) { strcpy(p, " SSE4.2"); p += 7; }
    if (simd_info.has_avx) { strcpy(p, " AVX"); p += 4; }
    if (simd_info.has_avx2) { strcpy(p, " AVX2"); p += 5; }
    if (simd_info.has_avx512) { strcpy(p, " AVX-512"); p += 9; }

    /* Calculate max vector width */
    if (simd_info.has_avx512) simd_info.vector_width = 512;
    else if (simd_info.has_avx2) simd_info.vector_width = 256;
    else if (simd_info.has_sse4_2) simd_info.vector_width = 128;
    else if (simd_info.has_sse2) simd_info.vector_width = 128;
    else if (simd_info.has_sse) simd_info.vector_width = 128;
    else simd_info.vector_width = 64; /* Baseline 64-bit */

#elif defined(__aarch64__)
    /* ARM64: Detect NEON and SVE via registers */
    /* Read ID_AA64PFR0_EL1 for processor features */
    uint64_t id_aa64pfr0;
    __asm__ volatile("mrs %0, id_aa64pfr0_el1" : "=r"(id_aa64pfr0));

    /* Bits 12-15: SVE (Scalable Vector Extension) */
    simd_info.has_sve = ((id_aa64pfr0 >> 12) & 0xF) != 0;

    /* NEON is always present on ARM64 (we can check ID_AA64ISAR0) */
    uint64_t id_aa64isar0;
    __asm__ volatile("mrs %0, id_aa64isar0_el1" : "=r"(id_aa64isar0));

    /* Check for AES, SHA etc for NEON presence */
    simd_info.has_neon = 1; /* ARM64 always has NEON */

    if (simd_info.has_sve) {
        strcpy(simd_info.simd_flags, "NEON SVE");
        simd_info.vector_width = 128; /* SVE is variable, minimum 128 */
    } else {
        strcpy(simd_info.simd_flags, "NEON");
        simd_info.vector_width = 128;
    }

#elif defined(__riscv)
    /* RISC-V: detect V extension via misa CSR (bit 12, 'V').
     * misa is at CSR 0x301, single 64-bit register on RV64. */
    {
        uint64_t misa = 0;
#if (__riscv_xlen == 64)
        __asm__ volatile("csrr %0, misa" : "=r"(misa));
        /* misa bit V = bit 12 ('V' = 0x2 'V' - 0x41 'A' = ...). Actually
         * misa bit 12 is 'V'. We test the whole field. */
        simd_info.has_rvv = (misa >> 12) & 1;
#else
        /* RV32: misa is split across two 32-bit CSRs (misa + miah) or read
         * via two csrrs. For simplicity, mark has_rvv as 0 here. */
        simd_info.has_rvv = 0;
#endif
        if (simd_info.has_rvv) {
            strcpy(simd_info.simd_flags, "RVV (Vector)");
            /* RVV VLEN is variable; report 128 as a conservative default.
             * Real VLEN can be probed via CSR 0xC22 (vlenb) * 8. */
            uint64_t vlenb = 0;
            __asm__ volatile("csrr %0, 0xC22" : "=r"(vlenb));
            simd_info.vector_width = (int)(vlenb * 8);
        } else {
            strcpy(simd_info.simd_flags, "RISC-V (no V ext)");
            simd_info.vector_width = 64;
        }
    }

#elif defined(__powerpc__) || defined(__powerpc64__)
    /* PowerPC: detect AltiVec (VMX) and VSX via CPU features.
     * AltiVec is mandatory on PowerPC since Power6 (2007).
     * On modern PPC, use the cpu_has_feature() sysctl (Linux) or the
     * HWCAP bits from getauxval(). */
    {
#if defined(__linux__)
        /* Use __builtin_cpu_supports() if available (gcc 4.8+/clang). */
        if (__builtin_cpu_supports("altivec")) {
            simd_info.has_altivec = 1;
            strcpy(simd_info.simd_flags, "AltiVec");
            simd_info.vector_width = 128;
        }
        if (__builtin_cpu_supports("vsx")) {
            simd_info.has_vsx = 1;
            /* Append " VSX" to flags */
            char *p = simd_info.simd_flags;
            while (*p) p++;
            strcpy(p, " VSX");
            /* VSX is 128-bit register pair = 256 effective bits, but
             * vector_width is conventionally reported as 128. */
            simd_info.vector_width = 128;
        }
        if (!simd_info.has_altivec && !simd_info.has_vsx) {
            strcpy(simd_info.simd_flags, "PowerPC (no SIMD)");
            simd_info.vector_width = 64;
        }
#else
        /* On non-Linux (e.g. AIX), assume AltiVec present on Power6+ */
        simd_info.has_altivec = 1;
        simd_info.has_vsx = 1;
        strcpy(simd_info.simd_flags, "AltiVec VSX (assumed)");
        simd_info.vector_width = 128;
#endif
    }

#else
    strcpy(simd_info.simd_flags, "Unknown");
    simd_info.vector_width = 64;  /* Baseline 64-bit */
#endif
}

/* Get SIMD info - initializes on first call */
const SIMDInfo* get_simd_info(void) {
    if (simd_info.simd_flags[0] == '\0') {
        detect_simd_capabilities();
    }
    return &simd_info;
}
