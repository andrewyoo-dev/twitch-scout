"""Tests for the one-shot collector.

A fake client (no HTTP) plus an in-memory store and a frozen clock let us assert the
assembly logic: slot skipping, per-game failure tolerance, aggregation, and the tier
written. The load-bearing behaviours are "skip an already-sampled slot" and "one
flaky category does not abort the sample".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from twitch_scout.clock import FrozenClock
from twitch_scout.collect.collector import Collector, CollectorConfig
from twitch_scout.collect.tiers import PACIFIC, Tier, resolve
from twitch_scout.store.db import connect
from twitch_scout.store.snapshots import Snapshot, read_batch, write_batch
from twitch_scout.twitch.client import StreamsResult, TwitchApiError
from twitch_scout.twitch.models import HelixGame, HelixStream

# Monday 15:00 PT -> baseline slot; Tuesday 19:15 PT -> window slot.
BASELINE_CLOCK = FrozenClock(datetime(2026, 9, 14, 15, 0, tzinfo=PACIFIC))
WINDOW_CLOCK = FrozenClock(datetime(2026, 9, 15, 19, 15, tzinfo=PACIFIC))


class FakeClient:
    def __init__(
        self,
        games: list[HelixGame],
        streams: dict[str, StreamsResult],
        *,
        fail_ids: set[str] | None = None,
    ) -> None:
        self.games = games
        self.streams = streams
        self.fail_ids = fail_ids or set()
        self.top_games_calls: list[int] = []
        self.stream_calls: list[str] = []

    def get_top_games(self, limit: int) -> list[HelixGame]:
        self.top_games_calls.append(limit)
        return self.games[:limit]

    def get_streams(self, game_id: str, *, max_pages: int) -> StreamsResult:
        self.stream_calls.append(game_id)
        if game_id in self.fail_ids:
            raise TwitchApiError(-1, "boom")
        return self.streams[game_id]


def _streams(viewers: list[int], *, truncated: bool = False, game_id: str = "g") -> StreamsResult:
    return StreamsResult(
        streams=[
            HelixStream(user_id=str(i), game_id=game_id, viewer_count=v)
            for i, v in enumerate(viewers)
        ],
        truncated=truncated,
    )


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def _three_game_client() -> FakeClient:
    games = [
        HelixGame(id="1", name="Job Sim"),
        HelixGame(id="2", name="TCG"),
        HelixGame(id="3", name="Doloc"),
    ]
    streams = {
        "1": _streams([100, 40], game_id="1"),
        "2": _streams([50, 30, 2], truncated=True, game_id="2"),
        "3": _streams([2], game_id="3"),
    }
    return FakeClient(games, streams)


def test_happy_path_writes_aggregated_rows(conn: sqlite3.Connection) -> None:
    client = _three_game_client()
    result = Collector(client, conn, BASELINE_CLOCK).run()

    assert result.skipped is False
    assert (result.games_seen, result.games_written, result.games_failed) == (3, 3, 0)

    rows = {r.game_id: r for r in read_batch(conn, result.slot.ts)}
    assert rows["1"].viewers == 140 and rows["1"].channels == 2
    assert rows["2"].truncated is True
    assert rows["3"].viewers == 2


def test_tier_is_taken_from_the_clock(conn: sqlite3.Connection) -> None:
    result = Collector(_three_game_client(), conn, WINDOW_CLOCK).run()
    assert result.slot.tier is Tier.WINDOW
    stored = {row[0] for row in conn.execute("SELECT DISTINCT tier FROM snapshots")}
    assert stored == {"window"}


def test_already_sampled_slot_is_skipped_without_api_calls(conn: sqlite3.Connection) -> None:
    slot = resolve(BASELINE_CLOCK)
    write_batch(conn, slot.ts, slot.tier, [Snapshot("x", "Existing", viewers=1, channels=1)])

    client = _three_game_client()
    result = Collector(client, conn, BASELINE_CLOCK).run()

    assert result.skipped is True
    assert client.top_games_calls == []  # never hit the API
    assert client.stream_calls == []


def test_force_overrides_skip(conn: sqlite3.Connection) -> None:
    slot = resolve(BASELINE_CLOCK)
    write_batch(conn, slot.ts, slot.tier, [Snapshot("1", "Old", viewers=1, channels=1)])

    client = _three_game_client()
    result = Collector(client, conn, BASELINE_CLOCK).run(force=True)

    assert result.skipped is False
    assert client.top_games_calls == [500]
    # The overwritten game reflects fresh data; new games are added.
    rows = {r.game_id: r for r in read_batch(conn, slot.ts)}
    assert rows["1"].viewers == 140  # overwritten, not the stale 1


def test_one_failing_category_does_not_abort_the_sample(conn: sqlite3.Connection) -> None:
    games = [HelixGame(id="1", name="A"), HelixGame(id="2", name="B"), HelixGame(id="3", name="C")]
    streams = {"1": _streams([10], game_id="1"), "3": _streams([5], game_id="3")}
    client = FakeClient(games, streams, fail_ids={"2"})

    result = Collector(client, conn, BASELINE_CLOCK).run()

    assert (result.games_seen, result.games_written, result.games_failed) == (3, 2, 1)
    written_ids = {r.game_id for r in read_batch(conn, result.slot.ts)}
    assert written_ids == {"1", "3"}  # the failing category is simply absent


def test_top_n_limits_games_considered(conn: sqlite3.Connection) -> None:
    client = _three_game_client()
    config = CollectorConfig(top_n=2)
    result = Collector(client, conn, BASELINE_CLOCK, config=config).run()

    assert client.top_games_calls == [2]
    assert result.games_written == 2


def test_get_top_games_failure_aborts_and_writes_nothing(conn: sqlite3.Connection) -> None:
    class FailingTopGames(FakeClient):
        def get_top_games(self, limit: int) -> list[HelixGame]:
            raise TwitchApiError(500, "down")

    client = FailingTopGames([], {})
    with pytest.raises(TwitchApiError):
        Collector(client, conn, BASELINE_CLOCK).run()
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0


def test_per_stream_flag_fails_loud(conn: sqlite3.Connection) -> None:
    config = CollectorConfig(per_stream=True)
    with pytest.raises(NotImplementedError, match="per-stream"):
        Collector(_three_game_client(), conn, BASELINE_CLOCK, config=config).run()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"top_n": 0}, "top_n"),
        ({"streams_max_pages": 0}, "streams_max_pages"),
    ],
)
def test_invalid_config_rejected(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CollectorConfig(**kwargs)


def test_tier_override_forces_window_on_an_off_day(conn: sqlite3.Connection) -> None:
    # BASELINE_CLOCK is a Monday afternoon (auto -> baseline), but --tier window
    # forces a window sample for manual backfill.
    result = Collector(_three_game_client(), conn, BASELINE_CLOCK).run(tier=Tier.WINDOW)
    assert result.slot.tier is Tier.WINDOW
    stored = {row[0] for row in conn.execute("SELECT DISTINCT tier FROM snapshots")}
    assert stored == {"window"}


# --- Steam candidate sampling (window tier only) ---


def _client_with_owned() -> FakeClient:
    games = [HelixGame(id="1", name="Top Game")]
    streams = {
        "1": _streams([100], game_id="1"),
        "9": _streams([40, 10], game_id="9"),  # an owned game absent from top-N
    }
    return FakeClient(games, streams)


def _owned() -> list[HelixGame]:
    return [HelixGame(id="9", name="Owned Game")]


def test_window_tier_samples_steam_candidates(conn: sqlite3.Connection) -> None:
    client = _client_with_owned()
    result = Collector(client, conn, WINDOW_CLOCK, steam_candidates=_owned).run()

    assert result.candidates_added == 1
    assert (result.games_seen, result.games_written) == (2, 2)
    assert "9" in client.stream_calls
    rows = {r.game_id for r in read_batch(conn, result.slot.ts)}
    assert rows == {"1", "9"}  # the owned game is now in the sample


def test_baseline_tier_does_not_sample_steam_candidates(conn: sqlite3.Connection) -> None:
    called = False

    def candidates() -> list[HelixGame]:
        nonlocal called
        called = True
        return _owned()

    client = _client_with_owned()
    result = Collector(client, conn, BASELINE_CLOCK, steam_candidates=candidates).run()

    assert result.candidates_added == 0
    assert called is False  # the store is not even read on a baseline slot
    assert "9" not in client.stream_calls


def test_steam_candidate_already_in_top_n_is_not_double_sampled(conn: sqlite3.Connection) -> None:
    client = _client_with_owned()  # top-N has game "1"
    result = Collector(
        client, conn, WINDOW_CLOCK, steam_candidates=lambda: [HelixGame(id="1", name="Top Game")]
    ).run()

    assert result.candidates_added == 0
    assert client.stream_calls.count("1") == 1  # sampled once, not twice


def test_slot_ts_is_the_batch_key(conn: sqlite3.Connection) -> None:
    # The written batch really lands on the resolved slot timestamp.
    result = Collector(_three_game_client(), conn, BASELINE_CLOCK).run()
    expected = resolve(BASELINE_CLOCK).ts
    assert result.slot.ts == expected
    stored = {row[0] for row in conn.execute("SELECT DISTINCT ts FROM snapshots")}
    assert stored == {expected.astimezone(UTC).isoformat()}
