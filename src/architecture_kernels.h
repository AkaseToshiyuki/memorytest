#ifndef ARCHITECTURE_KERNELS_H
#define ARCHITECTURE_KERNELS_H
#include <stdint.h>
#include <math.h>

/* Explicit baseline-ISA instruction streams. 32 payload instructions per
 * block, either one dependency chain or eight independent chains. Loop,
 * register setup and result retention remain in wall time. No cycle/frequency
 * inference, port counts, queue capacities or peak-width claims. */
#if defined(__x86_64__)
#define ARCH_KERNEL_ISA "x86_64-sse2"
#define ARCH_INT(op, reg) op "q %[c], %[" reg "]\n"
#define ARCH_FP(op, reg) op "sd %[c], %[" reg "]\n"
#define ARCH_FP_REG "+&x"
#define ARCH_FP_INPUT "x"
#elif defined(__aarch64__) || defined(__arm64__)
#define ARCH_KERNEL_ISA "aarch64-fp64"
#define ARCH_INT(op, reg) op " %[" reg "], %[" reg "], %[c]\n"
#define ARCH_FP(op, reg) "f" op " %d[" reg "], %d[" reg "], %d[c]\n"
#define ARCH_FP_REG "+&w"
#define ARCH_FP_INPUT "w"
#endif

#ifdef ARCH_KERNEL_ISA
#define ARCH_REPEAT1(op) ".rept 32\n" op("a") ".endr\n"
#define ARCH_REPEAT8(op) ".rept 4\n" op("a") op("b") op("d") op("e") op("f") op("g") op("h") op("k") ".endr\n"
#define ARCH_IADD(reg) ARCH_INT("add", reg)
#ifdef __x86_64__
#define ARCH_IMUL(reg) ARCH_INT("imul", reg)
#else
#define ARCH_IMUL(reg) ARCH_INT("mul", reg)
#endif
#define ARCH_FADD(reg) ARCH_FP("add", reg)
#define ARCH_FMUL(reg) ARCH_FP("mul", reg)
#define ARCH_OUTPUTS(constraint) [a] constraint(a), [b] constraint(b), [d] constraint(d), [e] constraint(e), \
    [f] constraint(f), [g] constraint(g), [h] constraint(h), [k] constraint(k)
#define ARCH_STORE(out) out[0] = a; out[1] = b; out[2] = d; out[3] = e; out[4] = f; out[5] = g; out[6] = h; out[7] = k
#define ARCH_INTEGER(name, instructions) \
static __attribute__((noinline)) void name(uint64_t blocks, uint64_t out[8]) { \
    uint64_t a=1, b=2, d=3, e=4, f=5, g=6, h=7, k=8, c=3; \
    for (uint64_t i=0; i<blocks; ++i) \
        __asm__ volatile(instructions : ARCH_OUTPUTS("+&r") : [c] "r"(c) : "cc"); \
    ARCH_STORE(out); \
}
#define ARCH_FLOAT(name, instructions, constant) \
static __attribute__((noinline)) void name(uint64_t blocks, double out[8]) { \
    double a=1, b=2, d=3, e=4, f=5, g=6, h=7, k=8, c=constant; \
    for (uint64_t i=0; i<blocks; ++i) \
        __asm__ volatile(instructions : ARCH_OUTPUTS(ARCH_FP_REG) : [c] ARCH_FP_INPUT(c)); \
    ARCH_STORE(out); \
}
ARCH_INTEGER(arch_iadd_dep, ARCH_REPEAT1(ARCH_IADD))
ARCH_INTEGER(arch_iadd_ind8, ARCH_REPEAT8(ARCH_IADD))
ARCH_INTEGER(arch_imul_dep, ARCH_REPEAT1(ARCH_IMUL))
ARCH_INTEGER(arch_imul_ind8, ARCH_REPEAT8(ARCH_IMUL))
ARCH_FLOAT(arch_fadd_dep, ARCH_REPEAT1(ARCH_FADD), .25)
ARCH_FLOAT(arch_fadd_ind8, ARCH_REPEAT8(ARCH_FADD), .25)
ARCH_FLOAT(arch_fmul_dep, ARCH_REPEAT1(ARCH_FMUL), 1.0 + 0x1p-32)
ARCH_FLOAT(arch_fmul_ind8, ARCH_REPEAT8(ARCH_FMUL), 1.0 + 0x1p-32)
static __attribute__((noinline)) void arch_nop(uint64_t blocks) {
    for (uint64_t i=0; i<blocks; ++i) __asm__ volatile(".rept 64\n nop\n .endr\n" : : : "memory");
}
static uint64_t arch_pow_u64(uint64_t value, uint64_t exponent) {
    uint64_t result = 1;
    for (; exponent; exponent >>= 1, value *= value) if (exponent & 1) result *= value;
    return result;
}
#endif
#endif
