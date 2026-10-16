"""Validate retained whole-batch retries; recompute drift from raw samples."""
from statistics import median


def previous_batches(row):
    batches = row.get("previous_batches", [])
    if not isinstance(batches, list) or len(batches) > 1 or any(not isinstance(b, dict) for b in batches):
        raise ValueError("invalid architecture retry history")
    for batch in batches:
        blocks = batch.get("blocks")
        if type(blocks) is not int or not 1 <= blocks <= 2**30:
            raise ValueError("invalid retry work count")
        elapsed = batch.get("elapsed_ns")
        if not isinstance(elapsed, list) or len(elapsed) != 10 or any(type(n) is not int or not 1 <= n < 2**64 for n in elapsed):
            raise ValueError("invalid retry timing samples")
    return batches


def batch_consistent(row, validate_counts=None, instructions_per_block=0):
    """No more than 10% normalized median drift from any retained batch.

    An unstable initial batch may be replaced only as a whole. Its medians
    still constrain the replacement; a frequency/core/phase change stays
    unstable even when the last ten samples happen to be tightly grouped.
    """
    valid = True
    for previous in previous_batches(row):
        fields = ["elapsed_ns"]
        if validate_counts:
            validate_counts(previous)
            if any(c and i < previous["blocks"]*instructions_per_block for c, i in zip(previous["cycles"], previous["instructions"])):
                raise ValueError("retry retired count below explicit work")
            if all(row["cycles"]) and all(previous["cycles"]): fields.append("cycles")
        for field in fields:
            a = median(previous[field])/previous["blocks"]
            b = median(row[field])/row["blocks"]
            valid = valid and max(a, b) <= 1.10*min(a, b)
    return valid


def paired_history(a, b):
    first, second = previous_batches(a), previous_batches(b)
    if len(first) != len(second) or any(x["blocks"] != y["blocks"] for x, y in zip(first, second)):
        raise ValueError("unpaired architecture retry history")
    for x, y in zip(first, second):
        before = median(p/q for p, q in zip(x["elapsed_ns"], y["elapsed_ns"]))
        after = median(p/q for p, q in zip(a["elapsed_ns"], b["elapsed_ns"]))
        if max(before, after) > 1.10*min(before, after): return False
    return True
