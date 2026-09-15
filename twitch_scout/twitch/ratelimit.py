"""Client-side token bucket for Helix rate limits.

Helix meters app tokens at ~800 points/minute, most endpoints costing one point per
request. This bucket paces requests *before* they go out so the collector stays
under the limit proactively; the client still handles a 429 reactively as a backstop.

Monotonic time and sleep are injected so the pacing logic is testable without real
waiting (coding standard 9).
"""

from __future__ import annotations

import time
from collections.abc import Callable

# Helix default app-token allowance.
HELIX_POINTS_PER_MINUTE = 800
_SECONDS_PER_MINUTE = 60

# Guard against an unbounded wait loop (coding standard 1). One refill wait should
# always suffice; more than a handful of iterations means something is misconfigured.
_MAX_WAITS = 16


class TokenBucket:
    """A refilling token bucket. Not thread-safe: one collector, one bucket."""

    def __init__(
        self,
        capacity: float,
        refill_per_sec: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill_per_sec <= 0:
            raise ValueError("refill_per_sec must be > 0")
        self._capacity = capacity
        self._refill_per_sec = refill_per_sec
        self._monotonic = monotonic
        self._sleep = sleep
        self._tokens = capacity
        self._last = monotonic()

    @classmethod
    def for_helix(
        cls,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> TokenBucket:
        rate = HELIX_POINTS_PER_MINUTE / _SECONDS_PER_MINUTE
        return cls(HELIX_POINTS_PER_MINUTE, rate, monotonic=monotonic, sleep=sleep)

    def _refill(self) -> None:
        now = self._monotonic()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_sec)
        self._last = now

    def acquire(self, cost: float = 1.0) -> None:
        """Block until ``cost`` tokens are available, then consume them."""
        if cost <= 0:
            raise ValueError("cost must be > 0")
        if cost > self._capacity:
            # Would never be satisfiable; a bug, not something to wait out forever.
            raise ValueError(f"cost {cost} exceeds bucket capacity {self._capacity}")
        for _ in range(_MAX_WAITS):
            self._refill()
            if self._tokens >= cost:
                self._tokens -= cost
                return
            wait = (cost - self._tokens) / self._refill_per_sec
            self._sleep(wait)
        raise RuntimeError("token bucket exceeded max wait iterations")

    @property
    def available(self) -> float:
        """Current token count after refilling. Primarily for tests/inspection."""
        self._refill()
        return self._tokens
