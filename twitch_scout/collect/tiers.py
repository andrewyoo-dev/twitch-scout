"""Sampling-tier decisions — the brain behind ``scout collect --tier auto``.

Two tiers (handoff section 5):

  * WINDOW (Tue/Thu/Sat, 18:30-22:30 PT, sampled every ~15 min). Decision-grade.
  * BASELINE (every hour otherwise), for trend and hour-of-day profiles.

Everything here is pure over an injected clock, so the whole schedule (including
daylight-saving transitions) is testable without waiting for a real Tuesday. The
instant is converted to Pacific time via ``zoneinfo``, which knows about DST, so we
never hand-roll UTC cron arithmetic.

The tier decision also fixes the canonical *slot* timestamp for the batch. Window
fires are floored to the granularity boundary and baseline fires to the hour, so a
cron that fires late or twice within the same slot resolves to the same timestamp
and the write stays idempotent (handoff section 9). The granularity matches the
trigger cadence (15 min) so a late fire stays in its intended slot instead of
colliding with the next one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from twitch_scout.clock import Clock

PACIFIC = ZoneInfo("America/Los_Angeles")

# Python's datetime.weekday(): Monday=0 .. Sunday=6.
_WEEKDAYS = range(7)
_TUE_THU_SAT = frozenset({1, 3, 5})
_MINUTES_PER_HOUR = 60


class Tier(StrEnum):
    WINDOW = "window"
    BASELINE = "baseline"


@dataclass(frozen=True)
class StreamSchedule:
    """When the stream-window tier is active, in the streamer's local timezone.

    The window must not cross midnight (start < end); that keeps the weekday test
    unambiguous and matches the real 18:30-22:30 schedule.
    """

    weekdays: frozenset[int] = _TUE_THU_SAT
    start: time = time(18, 30)
    end: time = time(22, 30)
    tz: ZoneInfo = PACIFIC
    window_granularity_min: int = 15

    def __post_init__(self) -> None:
        if not self.weekdays:
            raise ValueError("weekdays must not be empty")
        if not all(d in _WEEKDAYS for d in self.weekdays):
            raise ValueError("weekdays must be in 0..6 (Mon=0..Sun=6)")
        if self.start >= self.end:
            raise ValueError("start must be before end (window may not cross midnight)")
        g = self.window_granularity_min
        if not 1 <= g <= _MINUTES_PER_HOUR:
            raise ValueError("window_granularity_min must be in 1..60")
        if _MINUTES_PER_HOUR % g != 0:
            raise ValueError("window_granularity_min must divide 60 for aligned slots")


# The real schedule. Used as the default across this module (referenced by name so
# it is not re-constructed in argument defaults).
DEFAULT_SCHEDULE = StreamSchedule()


@dataclass(frozen=True)
class Slot:
    """The tier due right now and the canonical UTC timestamp identifying its batch.

    ``ts`` is the idempotency key for the whole sample: reuse it as the ``ts`` on
    every snapshot row so a re-fired collection overwrites rather than duplicates.
    """

    tier: Tier
    ts: datetime


def _require_aware(now: datetime) -> datetime:
    # Validate at the boundary: a naive datetime has no defined instant, and
    # astimezone() would silently assume the host's local zone.
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def in_stream_window(now: datetime, schedule: StreamSchedule = DEFAULT_SCHEDULE) -> bool:
    """Whether ``now`` falls inside a stream window. Half-open: [start, end)."""
    local = _require_aware(now).astimezone(schedule.tz)
    if local.weekday() not in schedule.weekdays:
        return False
    return schedule.start <= local.time() < schedule.end


def _floor_to_minutes(now_utc: datetime, minutes: int) -> datetime:
    discard = timedelta(
        minutes=now_utc.minute % minutes,
        seconds=now_utc.second,
        microseconds=now_utc.microsecond,
    )
    return now_utc - discard


def _floor_to_hour(now_utc: datetime) -> datetime:
    return now_utc.replace(minute=0, second=0, microsecond=0)


def resolve_slot(now: datetime, schedule: StreamSchedule = DEFAULT_SCHEDULE) -> Slot:
    """Classify ``now`` into a tier and its canonical batch timestamp."""
    now_utc = _require_aware(now)
    if in_stream_window(now_utc, schedule):
        ts = _floor_to_minutes(now_utc, schedule.window_granularity_min)
        return Slot(Tier.WINDOW, ts)
    return Slot(Tier.BASELINE, _floor_to_hour(now_utc))


def slot_for(now: datetime, tier: Tier, schedule: StreamSchedule = DEFAULT_SCHEDULE) -> Slot:
    """Build a slot for a forced tier, flooring the timestamp by that tier's rule.

    Used for manual ``collect --tier window|baseline`` runs (backfill, testing),
    where the operator overrides the clock-driven decision but the batch timestamp
    must still normalize the same way so idempotency holds.
    """
    now_utc = _require_aware(now)
    if tier is Tier.WINDOW:
        return Slot(Tier.WINDOW, _floor_to_minutes(now_utc, schedule.window_granularity_min))
    return Slot(Tier.BASELINE, _floor_to_hour(now_utc))


def resolve(clock: Clock, schedule: StreamSchedule = DEFAULT_SCHEDULE) -> Slot:
    """Convenience wrapper: resolve the slot from an injected clock."""
    return resolve_slot(clock.now(), schedule)
