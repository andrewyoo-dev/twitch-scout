"""Tests for the steam-sync orchestration (owned games -> Twitch ids -> store)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from twitch_scout.clock import FrozenClock
from twitch_scout.steam.client import OwnedLibrary, OwnedSteamGame
from twitch_scout.steam.sync import load_candidates, sync_owned_games
from twitch_scout.store.db import connect
from twitch_scout.store.steam import fetch_owned
from twitch_scout.twitch.models import HelixGame

CLOCK = FrozenClock(datetime(2026, 9, 20, 12, 0, tzinfo=UTC))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


class FakeSteam:
    def __init__(
        self,
        games: list[OwnedSteamGame],
        *,
        steamid: str = "76561190000000000",
        complete: bool = True,
    ) -> None:
        self._games = games
        self._steamid = steamid
        self._complete = complete
        self.resolved_with: str | None = None

    def resolve_steam_id(self, id_or_vanity: str) -> str:
        self.resolved_with = id_or_vanity
        return self._steamid

    def get_owned_games(self, steam_id: str) -> OwnedLibrary:
        assert steam_id == self._steamid
        skipped = 0 if self._complete else 1
        return OwnedLibrary(games=self._games, skipped=skipped, complete=self._complete)


class FakeLookup:
    """Resolves a fixed set of names (matched case-insensitively, trademark-agnostic)."""

    def __init__(self, name_to_id: dict[str, str]) -> None:
        self._by_key = {self._key(name): (gid, name) for name, gid in name_to_id.items()}
        self.queried: list[str] = []

    @staticmethod
    def _key(name: str) -> str:
        stripped = name.translate(dict.fromkeys(map(ord, "™®©")))
        return " ".join(stripped.split()).casefold()

    def get_games_by_name(self, names: list[str]) -> list[HelixGame]:
        self.queried = names
        out: list[HelixGame] = []
        for name in names:
            hit = self._by_key.get(self._key(name))
            if hit:
                out.append(HelixGame(id=hit[0], name=hit[1]))
        return out


def test_sync_resolves_and_stores(conn: sqlite3.Connection) -> None:
    steam = FakeSteam(
        [
            OwnedSteamGame(appid=1, name="Cozy Cove", playtime_minutes=600),
            OwnedSteamGame(appid=2, name="Unknown Indie", playtime_minutes=15),
        ]
    )
    lookup = FakeLookup({"Cozy Cove": "t1"})

    result = sync_owned_games(steam, lookup, conn, CLOCK, steam_id="kamagui")

    assert steam.resolved_with == "kamagui"
    assert (result.owned, result.resolved, result.unresolved, result.written) == (2, 1, 1, 2)
    owned = fetch_owned(conn)
    assert [o.twitch_game_id for o in owned] == ["t1"]
    assert owned[0].playtime_minutes == 600


def test_sync_matches_despite_trademark_and_case(conn: sqlite3.Connection) -> None:
    steam = FakeSteam([OwnedSteamGame(appid=1, name="Game™", playtime_minutes=5)])
    lookup = FakeLookup({"game": "t9"})  # different case, no trademark mark

    result = sync_owned_games(steam, lookup, conn, CLOCK, steam_id="id")

    assert result.resolved == 1
    assert fetch_owned(conn)[0].twitch_game_id == "t9"


def test_sync_is_idempotent(conn: sqlite3.Connection) -> None:
    steam = FakeSteam([OwnedSteamGame(appid=1, name="Cozy Cove", playtime_minutes=600)])
    lookup = FakeLookup({"Cozy Cove": "t1"})
    sync_owned_games(steam, lookup, conn, CLOCK, steam_id="id")
    sync_owned_games(steam, lookup, conn, CLOCK, steam_id="id")
    assert conn.execute("SELECT COUNT(*) FROM steam_games").fetchone()[0] == 1


def test_load_candidates_returns_resolved_as_helix_games(conn: sqlite3.Connection) -> None:
    steam = FakeSteam(
        [
            OwnedSteamGame(appid=1, name="Cozy Cove", playtime_minutes=600),
            OwnedSteamGame(appid=2, name="Unknown Indie", playtime_minutes=15),
        ]
    )
    lookup = FakeLookup({"Cozy Cove": "t1"})
    sync_owned_games(steam, lookup, conn, CLOCK, steam_id="id")

    candidates = load_candidates(conn)
    assert [(g.id, g.name) for g in candidates] == [("t1", "Cozy Cove")]  # unresolved excluded


def test_sync_prunes_games_no_longer_owned(conn: sqlite3.Connection) -> None:
    lookup = FakeLookup({"Cozy Cove": "t1", "Old Game": "t2"})
    first = FakeSteam([OwnedSteamGame(1, "Cozy Cove", 600), OwnedSteamGame(2, "Old Game", 30)])
    sync_owned_games(first, lookup, conn, CLOCK, steam_id="id")
    # A later refresh no longer includes appid 2.
    second = FakeSteam([OwnedSteamGame(1, "Cozy Cove", 700)])
    sync_owned_games(second, lookup, conn, CLOCK, steam_id="id")
    assert [o.twitch_game_id for o in fetch_owned(conn)] == ["t1"]


def test_incomplete_fetch_does_not_prune_absent_games(conn: sqlite3.Connection) -> None:
    # An incomplete fetch (a malformed item was dropped) must not delete a game absent
    # from this response: it may be missing only because its item was malformed (R5).
    lookup = FakeLookup({"Cozy Cove": "t1", "Old Game": "t2"})
    sync_owned_games(
        FakeSteam([OwnedSteamGame(1, "Cozy Cove", 600), OwnedSteamGame(2, "Old Game", 30)]),
        lookup,
        conn,
        CLOCK,
        steam_id="id",
    )
    partial = FakeSteam([OwnedSteamGame(1, "Cozy Cove", 700)], complete=False)
    result = sync_owned_games(partial, lookup, conn, CLOCK, steam_id="id")
    assert result.complete is False
    assert {o.twitch_game_id for o in fetch_owned(conn)} == {"t1", "t2"}  # t2 preserved


def test_sync_does_not_wipe_library_on_empty_fetch(conn: sqlite3.Connection) -> None:
    # An empty response (a private profile is indistinguishable from an empty library)
    # must never delete the stored library (Astra R1 safety).
    lookup = FakeLookup({"Cozy Cove": "t1"})
    sync_owned_games(
        FakeSteam([OwnedSteamGame(1, "Cozy Cove", 600)]), lookup, conn, CLOCK, steam_id="id"
    )
    result = sync_owned_games(FakeSteam([]), lookup, conn, CLOCK, steam_id="id")
    assert result.owned == 0
    assert [o.twitch_game_id for o in fetch_owned(conn)] == ["t1"]  # preserved


def test_sync_with_no_matches_stores_all_unresolved(conn: sqlite3.Connection) -> None:
    steam = FakeSteam([OwnedSteamGame(appid=1, name="Obscure", playtime_minutes=1)])
    lookup = FakeLookup({})
    result = sync_owned_games(steam, lookup, conn, CLOCK, steam_id="id")
    assert (result.resolved, result.unresolved) == (0, 1)
    assert load_candidates(conn) == []
