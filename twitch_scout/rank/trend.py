"""Trend and spike classification — pure functions over already-computed numbers.

Trend (handoff section 6): the single best "rising or dying" signal is the recent
average against the longer one. If the 7-day average sits below the 14-day, the
category is cooling — ReStory showed exactly this before the monthly numbers
confirmed the collapse.

Spike (handoff section 6): a single large streamer can move a small category 20x for
a moment. Comparing the latest reading to the category's own recent median marks
those outliers so they are not mistaken for genuine growth.
"""

from __future__ import annotations

from enum import StrEnum


class Trend(StrEnum):
    RISING = "rising"
    STABLE = "stable"
    FALLING = "falling"


def classify_trend(avg_recent: float | None, avg_long: float | None, eps: float = 0.1) -> Trend:
    """Classify recent vs long-window average. ``eps`` is the dead band (fraction of
    the long average) within which the category counts as stable.

    Returns STABLE when either input is missing or the long average is zero — not
    enough signal to call a direction.
    """
    if avg_recent is None or avg_long is None or avg_long <= 0:
        return Trend.STABLE
    delta = (avg_recent - avg_long) / avg_long
    if delta > eps:
        return Trend.RISING
    if delta < -eps:
        return Trend.FALLING
    return Trend.STABLE


def is_spike(latest: float, recent_median: float, factor: float = 3.0) -> bool:
    """Whether the latest reading is an outlier above the recent median."""
    if recent_median <= 0:
        return latest > 0
    return latest > recent_median * factor
