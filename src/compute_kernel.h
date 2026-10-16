#ifndef COMPUTE_KERNEL_H
#define COMPUTE_KERNEL_H
#include <stdint.h>

/* Immutable ComputeMark v2 scalar workload. Change the version if these change.
 * Integer: 4 independent xorshift64 streams, 3 shifts + 3 XORs each = 24 ops.
 * FP64: 4 logistic recurrences, 1 subtraction + 2 multiplies each = 12 FLOPs.
 * Loop control, loads/stores and checksum work are NOT counted as payload ops.
 */
#define CM_VERSION "v2"
#define CM_WORKLOAD "u64-xorshift4-fp64-logistic4-r1"
#define CM_PROFILE "portable-scalar-v2"
#define CM_REPEATS 10
#define CM_SAMPLING "repeat10-outer-iqr3-v1"
#define CM_MIN_NS UINT64_C(100000000)
#define CM_INT_OPS 24
#define CM_FP_OPS 12
void cm_integer(uint64_t iterations, uint64_t state[4]);
void cm_float(uint64_t iterations, double state[4]);
void cm_seed_integer(uint64_t state[4]);
void cm_seed_float(double state[4]);
int cm_selftest(void);
#endif
