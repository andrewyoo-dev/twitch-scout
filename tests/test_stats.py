"""Tests for the statistics helpers."""

from __future__ import annotations

import pytest

from twitch_scout.rank.stats import median, percentile


def test_median_odd() -> None:
    assert median([3, 1, 2]) == 2


def test_median_even() -> None:
    assert median([1, 2, 3, 4]) == 2.5


def test_median_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        median([])


def test_percentile_bounds() -> None:
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 0) == 10
    assert percentile(values, 100) == 50


def test_percentile_interpolates() -> None:
    assert percentile([0, 100], 10) == pytest.approx(10.0)


def test_percentile_single_value() -> None:
    assert percentile([42], 10) == 42


def test_percentile_low_ignores_high_outlier() -> None:
    # A robust floor: p10 sits near the low readings, not dragged by a spike.
    values = [60, 62, 65, 70, 1800]
    assert percentile(values, 10) < 70


@pytest.mark.parametrize("pct", [-1, 101])
def test_percentile_out_of_range(pct: float) -> None:
    with pytest.raises(ValueError, match="0..100"):
        percentile([1, 2, 3], pct)
