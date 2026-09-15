"""Tests for the noise guards.

The fixtures are the actual failure cases from the handoff — the one-off events
that topped a naive viewers-per-channel ranking (Ratatouille, Iron Man 2) and the
below-floor games (Doloc Town) — plus the legitimate deferred pick (TCG Card Shop
Simulator). If a change lets any of the garbage cases through, these fail.
"""

from __future__ import annotations

import pytest

from twitch_scout.rank.guards import (
    CandidateStats,
    GuardConfig,
    evaluate,
    partition,
)

# --- fixtures drawn from TWITCH_SCOUT_HANDOFF.md sections 6 and 7 ---

# Trap 1: one big streamer live briefly. Absurd ratio, ~1 channel, few samples.
RATATOUILLE = CandidateStats(
    game_id="ratatouille",
    game_name="Ratatouille",
    avg_viewers=1200.0,
    avg_channels=1.0,
    sample_count=1,
)

IRON_MAN_2 = CandidateStats(
    game_id="ironman2",
    game_name="Iron Man 2",
    avg_viewers=800.0,
    avg_channels=1.0,
    sample_count=2,
)

# Trap 2: genuinely enjoyable, but too few people browse the category to matter.
# Doloc Town drew ~2 average viewers and 0 new followers in practice.
DOLOC_TOWN = CandidateStats(
    game_id="doloc",
    game_name="Doloc Town",
    avg_viewers=2.0,
    avg_channels=1.0,
    sample_count=40,
)

# The legitimate deferred pick: high floor, workable channel band, persistent.
TCG_CARD_SHOP = CandidateStats(
    game_id="tcg",
    game_name="TCG Card Shop Simulator",
    avg_viewers=82.0,  # 7-day average from the handoff
    avg_channels=12.0,
    sample_count=300,
)

# A clean pass: comfortably above floor, in-band, well-sampled.
GOOD_CANDIDATE = CandidateStats(
    game_id="good",
    game_name="Job Simulator",
    avg_viewers=140.0,
    avg_channels=8.0,
    sample_count=200,
)


def test_ratatouille_rejected_as_one_off() -> None:
    result = evaluate(RATATOUILLE, GuardConfig())
    assert not result.passed
    # Fails on both too-few-channels and too-few-samples — the two Trap 1 teeth.
    assert any("channels" in f for f in result.failures)
    assert any("one-off" in f for f in result.failures)


def test_iron_man_2_rejected_as_one_off() -> None:
    result = evaluate(IRON_MAN_2, GuardConfig())
    assert not result.passed


def test_doloc_town_rejected_below_floor() -> None:
    result = evaluate(DOLOC_TOWN, GuardConfig())
    assert not result.passed
    assert any("below floor" in f for f in result.failures)


def test_good_candidate_passes() -> None:
    result = evaluate(GOOD_CANDIDATE, GuardConfig())
    assert result.passed
    assert result.failures == ()


def test_tcg_at_default_floor_is_below_hundred() -> None:
    # TCG's 7-day average (82) sits just under the default 100 floor. That is the
    # handoff's point: it was deferred, not disqualified. Lowering the tunable
    # floor lets it back in, which is exactly the intended knob.
    assert not evaluate(TCG_CARD_SHOP, GuardConfig()).passed
    lenient = GuardConfig(min_viewer_floor=60.0)
    assert evaluate(TCG_CARD_SHOP, lenient).passed


def test_all_failures_are_collected_not_short_circuited() -> None:
    # A candidate that fails multiple guards should report all of them.
    awful = CandidateStats(
        game_id="awful",
        game_name="Awful",
        avg_viewers=5.0,  # below floor
        avg_channels=1.0,  # below min channels
        sample_count=1,  # one-off
    )
    result = evaluate(awful, GuardConfig())
    assert not result.passed
    assert len(result.failures) == 3


def test_upper_band_filter_when_configured() -> None:
    crowded = CandidateStats(
        game_id="crowded",
        game_name="Crowded",
        avg_viewers=5000.0,
        avg_channels=200.0,
        sample_count=300,
    )
    assert evaluate(crowded, GuardConfig()).passed  # off by default
    banded = GuardConfig(max_avg_channels=30.0)
    result = evaluate(crowded, banded)
    assert not result.passed
    assert any("above band" in f for f in result.failures)


def test_distinct_streamers_guard_when_data_present() -> None:
    config = GuardConfig(min_distinct_streamers=5)
    too_few = CandidateStats(
        game_id="few",
        game_name="Few Streamers",
        avg_viewers=200.0,
        avg_channels=4.0,
        sample_count=100,
        distinct_streamers=3,
    )
    enough = CandidateStats(
        game_id="many",
        game_name="Many Streamers",
        avg_viewers=200.0,
        avg_channels=4.0,
        sample_count=100,
        distinct_streamers=20,
    )
    assert not evaluate(too_few, config).passed
    assert evaluate(enough, config).passed


def test_distinct_streamers_guard_raises_when_data_missing() -> None:
    # Asking for a guard the data cannot support must fail loud, not pass silently.
    config = GuardConfig(min_distinct_streamers=5)
    with pytest.raises(ValueError, match="distinct_streamers"):
        evaluate(GOOD_CANDIDATE, config)  # GOOD_CANDIDATE has distinct_streamers=None


def test_partition_splits_and_preserves_order() -> None:
    candidates = [RATATOUILLE, GOOD_CANDIDATE, DOLOC_TOWN, TCG_CARD_SHOP]
    eligible, rejected = partition(candidates, GuardConfig())
    assert [r.game_id for r in eligible] == ["good"]
    assert [r.game_id for r in rejected] == ["ratatouille", "doloc", "tcg"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_viewer_floor": -1.0}, "min_viewer_floor"),
        ({"min_avg_channels": -1.0}, "min_avg_channels"),
        ({"min_sample_count": 0}, "min_sample_count"),
        ({"max_avg_channels": 1.0, "min_avg_channels": 3.0}, "max_avg_channels"),
        ({"min_distinct_streamers": 0}, "min_distinct_streamers"),
    ],
)
def test_invalid_config_rejected(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        GuardConfig(**kwargs)
