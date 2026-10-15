"""Recompute the declared sampling policy from raw, ordered measurements.

The C measurement backend has its own implementation. Neither consumes a
precomputed score or trusts the other implementation's filtered summary.
"""
import math
import statistics

POLICY = "outer-iqr3-median-drift10-cap10-v1"


def summarize(values):
    values = list(values)
    count = len(values)
    if not 3 <= count <= 200 or any(isinstance(value, bool) or not isinstance(value, (int, float))
                                   or not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("invalid timing sample set")
    q1, raw_median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    half = count // 2
    drift = 100 * (statistics.median(values[-half:]) - statistics.median(values[:half])) / raw_median
    iqr = q3 - q1
    candidates = [i for i, value in enumerate(values)
                  if count >= 10 and iqr > 0 and (value < q1 - 3 * iqr or value > q3 + 3 * iqr)]
    unstable = abs(drift) > 10 or len(candidates) > count // 10
    excluded = [] if unstable else candidates
    kept = [value for i, value in enumerate(values) if i not in excluded]
    return dict(policy=POLICY, count=count, kept=len(kept), outlier_candidates=len(candidates),
                rejected=len(excluded), unstable=unstable, raw_median=raw_median,
                median=statistics.median(kept), q1=q1, q3=q3, raw_min=min(values), raw_max=max(values),
                drift_pct=drift, excluded_indices=excluded)
