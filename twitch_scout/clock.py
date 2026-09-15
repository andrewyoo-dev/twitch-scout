"""Injectable clock.

Time is a nondeterministic dependency, so it is injected rather than read directly
(coding standard 9). Production uses SystemClock; tests use FrozenClock to pin an
exact instant. Every clock yields timezone-aware UTC, so downstream code never has
to guess what a naive datetime means.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class FrozenClock:
    """A fixed instant, for tests. Rejects naive datetimes at construction so a
    test can never accidentally pin an ambiguous local time."""

    fixed: datetime

    def __post_init__(self) -> None:
        if self.fixed.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")

    def now(self) -> datetime:
        return self.fixed.astimezone(UTC)
