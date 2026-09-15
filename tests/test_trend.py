"""Tests for trend and spike classification."""

from __future__ import annotations

from twitch_scout.rank.trend import Trend, classify_trend, is_spike


def test_falling_when_recent_below_long() -> None:
    # The ReStory pattern: 7-day average sits below the 14-day.
    assert classify_trend(avg_recent=80, avg_long=120) is Trend.FALLING


def test_rising_when_recent_above_long() -> None:
    assert classify_trend(avg_recent=150, avg_long=100) is Trend.RISING


def test_stable_within_dead_band() -> None:
    assert classify_trend(avg_recent=104, avg_long=100, eps=0.1) is Trend.STABLE


def test_stable_when_data_missing() -> None:
    assert classify_trend(avg_recent=None, avg_long=100) is Trend.STABLE
    assert classify_trend(avg_recent=100, avg_long=None) is Trend.STABLE
    assert classify_trend(avg_recent=100, avg_long=0) is Trend.STABLE


def test_spike_detected() -> None:
    # TCG Card Shop: 1809 live against an ~82 median — one big streamer, briefly.
    assert is_spike(latest=1809, recent_median=82, factor=3.0) is True


def test_no_spike_within_factor() -> None:
    assert is_spike(latest=90, recent_median=82, factor=3.0) is False


def test_spike_with_zero_median() -> None:
    assert is_spike(latest=5, recent_median=0) is True
    assert is_spike(latest=0, recent_median=0) is False
