"""
Shared inter-core cluster detection from latency matrices.

Used by report_score.py, report_html.py, and test_sanity.py.
All three previously had copy-pasted Union-Find implementations
(see P2-20 audit finding). This module provides the single source of truth.

Algorithm: auto-detect natural core clusters from latency gaps.
Adjacent cores with latency < 2× min_adjacency are grouped together.
Works universally — no CPU model tables, no architecture assumptions.
"""

from typing import Optional, Union


def find_clusters(matrix, n: int) -> dict:
    """Find natural core clusters from an N×N latency matrix.

    Args:
        matrix: N×N 2D list. Missing values may be None or NaN.
        n: number of cores.

    Returns:
        {
            "intra": [...],      # sorted list of intra-cluster latencies
            "cross": [...],      # sorted list of cross-cluster latencies
            "clusters": {cid: [core_indices]},
        }
        Returns empty dicts/lists if no valid data found.

    Red Lines: no hardcoding — clusters are detected from latency gaps,
    not from CPU topology tables.
    """
    # Normalize sentinel values: both None and NaN mean "no data"
    def _has(v) -> bool:
        return v is not None and not (v != v)  # v != v catches NaN

    # Step 1: find minimum adjacency latency
    min_adj = float('inf')
    for i in range(n):
        for j in (i - 1, i + 1):
            if 0 <= j < n and _has(matrix[i][j]) and matrix[i][j] < min_adj:
                min_adj = matrix[i][j]
    if min_adj == float('inf'):
        for i in range(n):
            for j in range(n):
                if i != j and _has(matrix[i][j]) and matrix[i][j] < min_adj:
                    min_adj = matrix[i][j]
    if min_adj == float('inf'):
        return {"intra": [], "cross": [], "clusters": {}}

    # Step 2: Union-Find to group cores by connectivity
    threshold = min_adj * 2.0
    parent = list(range(n))

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if _has(matrix[i][j]) and matrix[i][j] < threshold:
                _union(i, j)
            elif _has(matrix[j][i]) and matrix[j][i] < threshold:
                _union(i, j)

    # Step 3: build cluster groups
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        cid = _find(i)
        clusters.setdefault(cid, []).append(i)

    # Step 4: split latencies into intra- and cross-cluster
    intra, cross = [], []
    for i in range(n):
        for j in range(n):
            if i == j or not _has(matrix[i][j]):
                continue
            (intra if _find(i) == _find(j) else cross).append(matrix[i][j])

    intra.sort()
    cross.sort()
    return {"intra": intra, "cross": cross, "clusters": clusters}


def median(vals: list[float]) -> Optional[float]:
    """Median of a sorted list. Returns None if empty."""
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2.0
