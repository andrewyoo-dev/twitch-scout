"""Tests for the Steam library store (the candidate-source table)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from twitch_scout.store.db import connect
from twitch_scout.store.steam import (
    OwnedGame,
    SteamGame,
    fetch_candidates,
    fetch_owned,
    upsert_steam_games,
)

SYNCED = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
SYNCED2 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def test_upsert_and_fetch_resolved(conn: sqlite3.Connection) -> None:
    upsert_steam_games(
        conn,
        [
            SteamGame(
                appid=1,
                name="Cozy Cove",
                playtime_minutes=600,
                twitch_game_id="t1",
                twitch_game_name="Cozy Cove",
            ),
            SteamGame(appid=2, name="Unmatched Game", playtime_minutes=30),  # unresolved
        ],
        SYNCED,
    )
    owned = fetch_owned(conn)
    assert owned == [
        OwnedGame(twitch_game_id="t1", twitch_game_name="Cozy Cove", playtime_minutes=600)
    ]
    # The unresolved game is stored but never a candidate (no twitch id).
    assert all(o.twitch_game_id == "t1" for o in fetch_candidates(conn))


def test_upsert_is_idempotent_on_appid(conn: sqlite3.Connection) -> None:
    upsert_steam_games(conn, [SteamGame(appid=1, name="Cozy Cove", playtime_minutes=10)], SYNCED)
    # Same appid, updated playtime + now resolved (a later sync).
    upsert_steam_games(
        conn,
        [
            SteamGame(
                appid=1,
                name="Cozy Cove",
                playtime_minutes=90,
                twitch_game_id="t1",
                twitch_game_name="Cozy Cove",
            )
        ],
        SYNCED2,
    )
    total = conn.execute("SELECT COUNT(*) FROM steam_games").fetchone()[0]
    assert total == 1  # overwritten, not duplicated
    assert fetch_owned(conn)[0].playtime_minutes == 90


def test_playtime_summed_across_appids_mapping_to_one_category(conn: sqlite3.Connection) -> None:
    # A game and its demo can both map to the same Twitch category.
    upsert_steam_games(
        conn,
        [
            SteamGame(
                appid=1,
                name="Game",
                playtime_minutes=100,
                twitch_game_id="t1",
                twitch_game_name="Game",
            ),
            SteamGame(
                appid=2,
                name="Game Demo",
                playtime_minutes=25,
                twitch_game_id="t1",
                twitch_game_name="Game",
            ),
        ],
        SYNCED,
    )
    owned = fetch_owned(conn)
    assert len(owned) == 1
    assert owned[0].playtime_minutes == 125


def test_candidates_include_non_owned_sources_but_owned_filters(conn: sqlite3.Connection) -> None:
    upsert_steam_games(
        conn,
        [
            SteamGame(
                appid=1, name="Owned", twitch_game_id="t1", twitch_game_name="Owned", source="owned"
            ),
            SteamGame(
                appid=2,
                name="Wished",
                twitch_game_id="t2",
                twitch_game_name="Wished",
                source="wishlist",
            ),
        ],
        SYNCED,
    )
    candidate_ids = {c.twitch_game_id for c in fetch_candidates(conn)}
    owned_ids = {o.twitch_game_id for o in fetch_owned(conn)}
    assert candidate_ids == {"t1", "t2"}  # collector samples both
    assert owned_ids == {"t1"}  # rank's owned section is owned-only


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"appid": 0}, "appid"),
        ({"name": "  "}, "name"),
        ({"playtime_minutes": -1}, "playtime"),
        ({"source": ""}, "source"),
    ],
)
def test_invalid_steam_game_rejected(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {"appid": 1, "name": "Game", "playtime_minutes": 0}
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        SteamGame(**base)  # type: ignore[arg-type]
