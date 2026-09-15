"""Tests for the sampling-tier decision.

Dates are chosen against the real calendar and each test asserts the weekday it
relies on, so a wrong date fails loudly instead of silently testing the wrong day.
Reference: 2026-09-13 is a Sunday, so that week's Tue/Thu/Sat are 09-15/17/19, all
in Pacific *Daylight* time (UTC-7). December dates land in Pacific *Standard* time
(UTC-8); the DST test leans on that difference.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from twitch_scout.clock import FrozenClock
from twitch_scout.collect.tiers import (
    DEFAULT_SCHEDULE,
    PACIFIC,
    Slot,
    StreamSchedule,
    Tier,
    in_stream_window,
    resolve,
    resolve_slot,
    slot_for,
)


def _pt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=PACIFIC)


# --- weekday sanity: the calendar assumptions the tests below depend on ---


def test_calendar_assumptions() -> None:
    assert _pt(2026, 9, 15, 19).weekday() == 1  # Tuesday
    assert _pt(2026, 9, 17, 19).weekday() == 3  # Thursday
    assert _pt(2026, 9, 19, 19).weekday() == 5  # Saturday
    assert _pt(2026, 9, 14, 19).weekday() == 0  # Monday (off day)
    assert _pt(2026, 12, 15, 19).weekday() == 1  # Tuesday, in PST


# --- in_stream_window ---


def test_tuesday_evening_is_in_window() -> None:
    assert in_stream_window(_pt(2026, 9, 15, 19, 15)) is True


def test_monday_evening_is_not_in_window() -> None:
    assert in_stream_window(_pt(2026, 9, 14, 19, 15)) is False


def test_window_start_is_inclusive() -> None:
    assert in_stream_window(_pt(2026, 9, 15, 18, 30)) is True


def test_window_end_is_exclusive() -> None:
    assert in_stream_window(_pt(2026, 9, 15, 22, 30)) is False


def test_just_before_start_is_out() -> None:
    assert in_stream_window(_pt(2026, 9, 15, 18, 29)) is False


def test_dst_and_standard_time_both_resolve_by_wall_clock() -> None:
    # The same wall-clock 7:15 PM PT is in-window in both September (PDT, UTC-7)
    # and December (PST, UTC-8). If we were doing fixed UTC math one of these
    # would fall outside the window.
    summer = _pt(2026, 9, 15, 19, 15)
    winter = _pt(2026, 12, 15, 19, 15)
    assert in_stream_window(summer) is True
    assert in_stream_window(winter) is True
    # And the underlying instants really do differ by the DST hour.
    assert summer.astimezone(UTC).hour == 2  # 19:15 PDT -> 02:15 UTC
    assert winter.astimezone(UTC).hour == 3  # 19:15 PST -> 03:15 UTC


# --- resolve_slot: tier + canonical timestamp ---


def test_window_slot_floors_to_ten_minutes() -> None:
    slot = resolve_slot(_pt(2026, 9, 15, 19, 17))
    assert slot.tier is Tier.WINDOW
    # 19:17 PDT == 02:17 UTC, floored to the 10-min boundary -> 02:10 UTC.
    assert slot.ts == datetime(2026, 9, 16, 2, 10, tzinfo=UTC)


def test_baseline_slot_floors_to_the_hour() -> None:
    slot = resolve_slot(_pt(2026, 9, 14, 15, 47))  # Monday afternoon -> baseline
    assert slot.tier is Tier.BASELINE
    assert slot.ts == datetime(2026, 9, 14, 15, 47).astimezone(PACIFIC).astimezone(UTC).replace(
        minute=0, second=0, microsecond=0
    )
    assert slot.ts.minute == 0 and slot.ts.second == 0


def test_two_fires_in_the_same_window_slot_share_a_timestamp() -> None:
    # Idempotency: a jittered re-fire inside the same 10-min slot maps to the same
    # batch ts, so the snapshot write overwrites rather than duplicates.
    a = resolve_slot(
        _pt(
            2026,
            9,
            15,
            19,
            12,
        )
    )
    b = resolve_slot(_pt(2026, 9, 15, 19, 18))
    assert a.ts == b.ts == datetime(2026, 9, 16, 2, 10, tzinfo=UTC)


def test_window_takes_priority_over_baseline_on_the_hour() -> None:
    # 7:00 PM PT on a Tuesday is both top-of-hour and in-window: window wins.
    slot = resolve_slot(_pt(2026, 9, 15, 19, 0))
    assert slot.tier is Tier.WINDOW


# --- resolve() via injected clock ---


def test_resolve_uses_injected_clock() -> None:
    clock = FrozenClock(_pt(2026, 9, 17, 20, 5))  # Thursday, in window
    slot = resolve(clock)
    assert slot.tier is Tier.WINDOW
    assert slot.ts == datetime(2026, 9, 18, 3, 0, tzinfo=UTC)  # 20:05 PDT -> 03:00 UTC slot


# --- boundary validation ---


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_slot(datetime(2026, 9, 15, 19, 15))  # noqa: DTZ001 -- intentionally naive


# --- schedule validation ---


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"weekdays": frozenset()}, "weekdays must not be empty"),
        ({"weekdays": frozenset({7})}, "0..6"),
        ({"start": time(22, 30), "end": time(18, 30)}, "start must be before end"),
        ({"window_granularity_min": 0}, "1..60"),
        ({"window_granularity_min": 7}, "divide 60"),
    ],
)
def test_invalid_schedule_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        StreamSchedule(**kwargs)  # type: ignore[arg-type]


def test_custom_schedule_changes_the_window() -> None:
    # A Monday schedule makes Monday evening in-window and Tuesday out.
    mondays = StreamSchedule(weekdays=frozenset({0}))
    assert in_stream_window(_pt(2026, 9, 14, 19, 15), mondays) is True
    assert in_stream_window(_pt(2026, 9, 15, 19, 15), mondays) is False


def test_default_schedule_is_the_real_one() -> None:
    assert DEFAULT_SCHEDULE.weekdays == frozenset({1, 3, 5})
    assert DEFAULT_SCHEDULE.start == time(18, 30)
    assert DEFAULT_SCHEDULE.end == time(22, 30)


def test_slot_is_hashable_and_frozen() -> None:
    slot = Slot(Tier.WINDOW, datetime(2026, 9, 16, 2, 10, tzinfo=UTC))
    assert slot in {slot}  # frozen dataclass -> hashable, usable as a key


# --- slot_for: forced tier for manual/backfill runs ---


def test_slot_for_forces_window_with_ten_minute_floor() -> None:
    # A Monday (never a real window) forced to WINDOW still floors to 10 minutes.
    slot = slot_for(_pt(2026, 9, 14, 15, 47), Tier.WINDOW)
    assert slot.tier is Tier.WINDOW
    assert slot.ts.minute % 10 == 0
    assert slot.ts.second == 0


def test_slot_for_forces_baseline_with_hour_floor() -> None:
    # A Tuesday evening (a real window) forced to BASELINE floors to the hour.
    slot = slot_for(_pt(2026, 9, 15, 19, 47), Tier.BASELINE)
    assert slot.tier is Tier.BASELINE
    assert slot.ts.minute == 0
