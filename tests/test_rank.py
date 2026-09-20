"""Integration tests for ranking over seeded snapshots.

An in-memory store is seeded with window-tier samples across the evaluation window,
then ranked. The load-bearing assertions: the noise cases (Trap 1/2 from the
handoff) are filtered, and the survivors carry the right trend/spike signals.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from twitch_scout.clock import FrozenClock
from twitch_scout.collect.tiers import Tier
from twitch_scout.rank.rank import RankConfig, rank_candidates
from twitch_scout.rank.trend import Trend
from twitch_scout.store.db import connect
from twitch_scout.store.snapshots import Snapshot, write_batch

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
CLOCK = FrozenClock(NOW)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def _seed(
    conn: sqlite3.Connection,
    game_id: str,
    name: str,
    samples: list[tuple[int, int, int]],  # (days_ago, viewers, channels)
) -> None:
    for days_ago, viewers, channels in samples:
        ts = NOW - timedelta(days=days_ago)
        write_batch(conn, ts, Tier.WINDOW, [Snapshot(game_id, name, viewers, channels)])


def _recent(viewers: int, channels: int) -> list[tuple[int, int, int]]:
    return [(d, viewers, channels) for d in (1, 2, 3, 4, 5, 6)]


def _older(viewers: int, channels: int) -> list[tuple[int, int, int]]:
    return [(d, viewers, channels) for d in (8, 9, 10, 11, 12, 13)]


def _seed_all(conn: sqlite3.Connection) -> None:
    # rising, in-band, well-sampled -> eligible, RISING
    _seed(conn, "good", "Job Simulator", _recent(160, 8) + _older(120, 8))
    # one big streamer, 1 channel, 2 samples -> Trap 1, rejected
    _seed(conn, "noise", "Ratatouille", [(1, 800, 1), (2, 900, 1)])
    # persistent but tiny audience -> Trap 2 (below floor), rejected
    _seed(conn, "low", "Doloc Town", _recent(20, 5))
    # cooling: recent below older -> eligible, FALLING
    _seed(conn, "fall", "ReStory", _recent(100, 6) + _older(200, 6))
    # a single inflated latest reading -> eligible, spiking
    _seed(conn, "spike", "TCG Card Shop", [(1, 1500, 5), *[(d, 120, 5) for d in (2, 3, 4, 5, 6)]])


def test_ranking_filters_noise_and_keeps_real_categories(conn: sqlite3.Connection) -> None:
    _seed_all(conn)
    result = rank_candidates(conn, CLOCK, RankConfig())

    eligible = {c.game_id for c in result.candidates}
    assert eligible == {"good", "fall", "spike"}
    rejected = {r.game_id for r in result.rejected}
    assert rejected == {"noise", "low"}


def test_rejection_reasons(conn: sqlite3.Connection) -> None:
    _seed_all(conn)
    result = rank_candidates(conn, CLOCK, RankConfig())
    reasons = {r.game_id: r.failures for r in result.rejected}
    assert any("one-off" in f or "channels" in f for f in reasons["noise"])
    assert any("below floor" in f for f in reasons["low"])


def test_trend_and_spike_signals(conn: sqlite3.Connection) -> None:
    _seed_all(conn)
    by_id = {c.game_id: c for c in rank_candidates(conn, CLOCK, RankConfig()).candidates}
    assert by_id["good"].trend is Trend.RISING
    assert by_id["fall"].trend is Trend.FALLING
    assert by_id["spike"].spiking is True
    assert by_id["good"].spiking is False


def test_hide_falling_drops_cooling_categories(conn: sqlite3.Connection) -> None:
    _seed_all(conn)
    result = rank_candidates(conn, CLOCK, RankConfig(hide_falling=True))
    ids = {c.game_id for c in result.candidates}
    assert "fall" not in ids
    assert "good" in ids


def test_ranked_by_score_descending(conn: sqlite3.Connection) -> None:
    _seed_all(conn)
    candidates = rank_candidates(conn, CLOCK, RankConfig()).candidates
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)


def test_score_prefers_spread_over_single_giant(conn: sqlite3.Connection) -> None:
    # Two eligible categories with the SAME total viewers: one spread across many
    # channels (a 2-6 viewer channel can surface), one carried by a few big streams
    # (it would be buried). The spread category must rank higher.
    _seed(conn, "spread", "The Plant Shop", _recent(600, 30) + _older(600, 30))
    _seed(conn, "giant", "Last Pirates", _recent(600, 3) + _older(600, 3))
    candidates = rank_candidates(conn, CLOCK, RankConfig()).candidates
    order = [c.game_id for c in candidates]
    assert order.index("spread") < order.index("giant")
    # The old ratio sort would have inverted this: the giant has the bigger ratio.
    by_id = {c.game_id: c for c in candidates}
    assert by_id["giant"].ratio > by_id["spread"].ratio


def test_default_penalty_flips_concentrated_below_spread(conn: sqlite3.Connection) -> None:
    # A concentrated category (8000 viewers over 5 channels) and a spread one (1000
    # over 20). These are chosen so the winner flips between c=0.5 and the shipped
    # default 0.75: at 0.5 the concentrated giant scores higher, at 0.75 the spread
    # field does. This pins the actual default, not merely "any c > 0".
    _seed(conn, "giant", "Casino Night", _recent(8000, 5) + _older(8000, 5))
    _seed(conn, "field", "Cozy Cove", _recent(1000, 20) + _older(1000, 20))

    at_050 = [
        c.game_id
        for c in rank_candidates(conn, CLOCK, RankConfig(concentration_penalty=0.5)).candidates
    ]
    assert at_050.index("giant") < at_050.index("field")  # concentrated wins at 0.5

    at_default = [c.game_id for c in rank_candidates(conn, CLOCK, RankConfig()).candidates]  # 0.75
    assert at_default.index("field") < at_default.index("giant")  # spread wins at the default


def test_rank_config_rejects_nonpositive_days() -> None:
    with pytest.raises(ValueError, match="eval_days"):
        RankConfig(eval_days=0)


def test_concentration_penalty_zero_falls_back_to_viewers(conn: sqlite3.Connection) -> None:
    # penalty=0 => score is raw viewers, so the single-giant category (same viewers,
    # fewer channels) is no longer down-ranked below the spread one.
    _seed(conn, "spread", "The Plant Shop", _recent(600, 30) + _older(600, 30))
    _seed(conn, "giant", "Last Pirates", _recent(600, 3) + _older(600, 3))
    config = RankConfig(concentration_penalty=0.0)
    by_id = {c.game_id: c for c in rank_candidates(conn, CLOCK, config).candidates}
    assert by_id["spread"].score == pytest.approx(by_id["giant"].score)


def test_rank_config_rejects_out_of_range_penalty() -> None:
    with pytest.raises(ValueError, match="concentration_penalty"):
        RankConfig(concentration_penalty=1.5)


def test_floor_is_robust_to_the_spike(conn: sqlite3.Connection) -> None:
    _seed_all(conn)
    by_id = {c.game_id: c for c in rank_candidates(conn, CLOCK, RankConfig()).candidates}
    # spike category's floor reflects the sustained ~120, not the 1500 outlier.
    assert by_id["spike"].floor < 300


def test_empty_store_returns_nothing(conn: sqlite3.Connection) -> None:
    result = rank_candidates(conn, CLOCK, RankConfig())
    assert result.candidates == []
    assert result.rejected == []
    assert result.owned == []


# --- owned section (Steam library) ---


def _mark_owned(conn: sqlite3.Connection, twitch_game_id: str, name: str, playtime: int) -> None:
    from twitch_scout.store.steam import SteamGame, upsert_steam_games

    upsert_steam_games(
        conn,
        [
            SteamGame(
                appid=(hash(twitch_game_id) & 0xFFFFFF) + 1,
                name=name,
                playtime_minutes=playtime,
                twitch_game_id=twitch_game_id,
                twitch_game_name=name,
            )
        ],
        NOW,
    )


def test_owned_section_surfaces_a_low_ranked_owned_game(conn: sqlite3.Connection) -> None:
    # An owned game below the strict floor (100) but above the relaxed one (10): it is
    # absent from the main list yet present in the owned section, with playtime.
    _seed(conn, "mine", "Cozy Cove", _recent(40, 4) + _older(40, 4))
    _mark_owned(conn, "mine", "Cozy Cove", playtime=600)
    result = rank_candidates(conn, CLOCK, RankConfig())

    assert "mine" not in {c.game_id for c in result.candidates}  # strict floor drops it
    owned = {c.game_id: c for c in result.owned}
    assert "mine" in owned
    assert owned["mine"].playtime_minutes == 600


def test_owned_dead_game_is_excluded_by_relaxed_floor(conn: sqlite3.Connection) -> None:
    # A resolved owned game with no live streams reads as 0 viewers -> below even the
    # relaxed floor -> excluded (not a useful streaming target).
    _seed(conn, "dead", "Obscure Owned", _recent(0, 0) + _older(0, 0))
    _mark_owned(conn, "dead", "Obscure Owned", playtime=5)
    result = rank_candidates(conn, CLOCK, RankConfig())
    assert "dead" not in {c.game_id for c in result.owned}


def test_owned_section_empty_without_steam_data(conn: sqlite3.Connection) -> None:
    _seed_all(conn)  # no steam_games rows
    assert rank_candidates(conn, CLOCK, RankConfig()).owned == []


def test_main_list_candidates_have_no_playtime(conn: sqlite3.Connection) -> None:
    _seed_all(conn)
    for c in rank_candidates(conn, CLOCK, RankConfig()).candidates:
        assert c.playtime_minutes is None


def test_lenient_floor_admits_more(conn: sqlite3.Connection) -> None:
    _seed_all(conn)
    from twitch_scout.rank.guards import GuardConfig

    lenient = RankConfig(guards=GuardConfig(min_viewer_floor=10.0))
    ids = {c.game_id for c in rank_candidates(conn, CLOCK, lenient).candidates}
    assert "low" in ids  # Doloc Town now clears the lowered floor
