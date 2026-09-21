"""Regression tests for the Steam candidate source (from an Astra review).

Each guards a specific defect found after the initial Steam pushes: a refresh not
pruning removed games, a malformed Steam response tracebacking, a sub-1 channel
ceiling aborting rank, and --limit ignoring the owned section. All HTTP is mocked
and every database is in memory.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

from twitch_scout import cli
from twitch_scout.clock import FrozenClock, SystemClock
from twitch_scout.collect.tiers import Tier
from twitch_scout.steam.client import OwnedLibrary, OwnedSteamGame, SteamClient
from twitch_scout.steam.sync import SupportsGameLookup, SupportsOwnedGames, sync_owned_games
from twitch_scout.store.db import connect
from twitch_scout.store.snapshots import Snapshot, write_batch
from twitch_scout.store.steam import SteamGame, fetch_owned, upsert_steam_games
from twitch_scout.twitch.client import HelixClient
from twitch_scout.twitch.models import HelixGame
from twitch_scout.twitch.ratelimit import TokenBucket


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        os,
        "environ",
        {
            "SCOUT_DB": ":memory:",
            "TWITCH_CLIENT_ID": "review-placeholder",
            "TWITCH_CLIENT_SECRET": "review-placeholder",
            "STEAM_API_KEY": "review-placeholder",
            "STEAM_ID": "76561190000000000",
        },
    )


def test_refresh_removes_games_no_longer_owned() -> None:
    steam = Mock(spec=SupportsOwnedGames)
    steam.resolve_steam_id.return_value = "76561190000000000"
    steam.get_owned_games.side_effect = [
        OwnedLibrary(
            [OwnedSteamGame(1, "Removed Game", 60), OwnedSteamGame(2, "Kept Game", 90)],
            skipped=0,
            complete=True,
        ),
        OwnedLibrary([OwnedSteamGame(2, "Kept Game", 100)], skipped=0, complete=True),
    ]
    lookup = Mock(spec=SupportsGameLookup)
    lookup.get_games_by_name.side_effect = [
        [HelixGame(id="1", name="Removed Game"), HelixGame(id="2", name="Kept Game")],
        [HelixGame(id="2", name="Kept Game")],
    ]
    clock = FrozenClock(datetime(2026, 9, 21, tzinfo=UTC))
    conn = connect(":memory:")
    try:
        sync_owned_games(steam, lookup, conn, clock, steam_id="review")
        result = sync_owned_games(steam, lookup, conn, clock, steam_id="review")
        assert result.owned == 1
        assert [game.twitch_game_id for game in fetch_owned(conn)] == ["2"]
    finally:
        conn.close()


@pytest.mark.parametrize("body", [b"not JSON", b'{"response":{"games":null}}'])
def test_malformed_steam_response_exits_cleanly(
    body: bytes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    steam = SteamClient(
        "review-placeholder", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(SteamClient, "create", lambda api_key: steam)
    helix = HelixClient(
        "review-placeholder",
        "review-placeholder",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=SystemClock(),
        bucket=TokenBucket.for_helix(),
    )
    monkeypatch.setattr(HelixClient, "create", lambda client_id, client_secret: helix)
    assert cli.main(["steam-sync"]) == 1
    assert "error:" in capsys.readouterr().err


def test_sub_one_channel_ceiling_does_not_abort_rank() -> None:
    assert cli.main(["rank", "--min-channels", "0", "--max-channels", "0.5"]) == 0


def test_partial_validation_does_not_delete_a_still_owned_game() -> None:
    # R5: a 200 response with a valid game and a malformed one (dropped by the parser)
    # must not prune the dropped game, which is still owned. Its item was just bad.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "response": {
                    "game_count": 2,
                    "games": [
                        {"appid": 1, "name": "Game 1", "playtime_forever": 70},
                        {"appid": 2, "playtime_forever": 60},  # no name -> dropped
                    ],
                }
            },
        )

    lookup = Mock(spec=SupportsGameLookup)
    lookup.get_games_by_name.return_value = [HelixGame(id="1", name="Game 1")]
    conn = connect(":memory:")
    try:
        upsert_steam_games(
            conn,
            [SteamGame(i, f"Game {i}", 60, str(i), f"Game {i}") for i in (1, 2)],
            datetime(2026, 9, 21, tzinfo=UTC),
        )
        client = SteamClient(
            "review-placeholder", http=httpx.Client(transport=httpx.MockTransport(handler))
        )
        result = sync_owned_games(
            client,
            lookup,
            conn,
            FrozenClock(datetime(2026, 9, 21, tzinfo=UTC)),
            steam_id="76561190000000000",  # 17 digits: skips vanity resolution
        )
        assert result.complete is False
        assert {game.twitch_game_id for game in fetch_owned(conn)} == {"1", "2"}  # 2 preserved
    finally:
        conn.close()


def test_rank_limit_bounds_owned_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(UTC)
    conn = connect(":memory:")
    try:
        upsert_steam_games(
            conn,
            [SteamGame(i, f"Review Game {i}", 60, str(i), f"Review Game {i}") for i in range(1, 4)],
            now,
        )
        write_batch(
            conn,
            now,
            Tier.WINDOW,
            [Snapshot(str(i), f"Review Game {i}", 40, 2) for i in range(1, 4)],
        )
        monkeypatch.setattr(cli, "connect", lambda *args, **kwargs: conn)
        assert cli.main(["rank", "--limit", "1"]) == 0
        rows = [line for line in capsys.readouterr().out.splitlines() if "Review Game" in line]
        assert len(rows) == 1
    finally:
        conn.close()
