#ifndef PERFORMANCE_UNITS_H
#define PERFORMANCE_UNITS_H

/* Input: counted operations / elapsed nanoseconds == GOPS. */
static inline const char *performance_rate_unit(double gops) {
    return gops >= 100.0 ? "TOPS" : "GOPS";
}
static inline double performance_rate_value(double gops) {
    return gops >= 100.0 ? gops / 1000.0 : gops;
}

#endif
