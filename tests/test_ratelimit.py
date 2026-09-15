"""Tests for the token bucket.

A fake monotonic clock advances only when the injected sleep is called, so pacing
is verified deterministically without real time passing.
"""

from __future__ import annotations

import pytest

from twitch_scout.twitch.ratelimit import TokenBucket


class FakeTime:
    """Monotonic time that only moves when sleep() is called."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def _bucket(capacity: float, rate: float) -> tuple[TokenBucket, FakeTime]:
    clock = FakeTime()
    bucket = TokenBucket(capacity, rate, monotonic=clock.monotonic, sleep=clock.sleep)
    return bucket, clock


def test_starts_full() -> None:
    bucket, _ = _bucket(10, 1)
    assert bucket.available == 10


def test_acquire_consumes_without_sleeping_when_tokens_available() -> None:
    bucket, clock = _bucket(10, 1)
    for _ in range(10):
        bucket.acquire()
    assert clock.slept == []
    assert bucket.available == 0


def test_acquire_waits_when_empty_then_refills() -> None:
    bucket, clock = _bucket(2, rate=1.0)  # 1 token/sec
    bucket.acquire()
    bucket.acquire()  # bucket now empty
    bucket.acquire()  # must wait ~1s for one token
    assert clock.slept == [pytest.approx(1.0)]
    assert clock.t == pytest.approx(1.0)


def test_refills_over_elapsed_time_up_to_capacity() -> None:
    bucket, clock = _bucket(5, rate=1.0)
    for _ in range(5):
        bucket.acquire()
    clock.t += 100  # plenty of time passes
    assert bucket.available == 5  # capped at capacity, not 100


def test_cost_greater_than_capacity_is_rejected() -> None:
    bucket, _ = _bucket(5, 1)
    with pytest.raises(ValueError, match="exceeds bucket capacity"):
        bucket.acquire(6)


@pytest.mark.parametrize(
    ("capacity", "rate"),
    [(0, 1), (-1, 1), (5, 0), (5, -1)],
)
def test_invalid_construction_rejected(capacity: float, rate: float) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        TokenBucket(capacity, rate)


def test_zero_cost_rejected() -> None:
    bucket, _ = _bucket(5, 1)
    with pytest.raises(ValueError, match="cost must be > 0"):
        bucket.acquire(0)


def test_for_helix_defaults() -> None:
    bucket = TokenBucket.for_helix()
    assert bucket.available == pytest.approx(800)
