"""Small statistics helpers.

SQLite/libSQL have no percentile function and portable median is awkward in SQL, so
these are computed in Python over the (small) per-candidate series instead.
"""

from __future__ import annotations

from collections.abc import Sequence

_MAX_PERCENTILE = 100.0


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (``pct`` in 0..100).

    Used for a robust viewer floor: the low percentile of a category's readings is
    the audience it sustains, ignoring the occasional zero when it briefly went dark.
    """
    if not values:
        raise ValueError("percentile of empty sequence")
    if not 0.0 <= pct <= _MAX_PERCENTILE:
        raise ValueError("pct must be in 0..100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac
