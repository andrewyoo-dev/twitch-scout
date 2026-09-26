"""Tests for the watchlist: storage, `watch add` resolution, collection, and ranking."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from twitch_scout.clock import FrozenClock
from twitch_scout.collect.candidates import load_candidates
from twitch_scout.collect.tiers import Tier
from twitch_scout.rank.rank import RankConfig, rank_candidates
from twitch_scout.store.db import connect
from twitch_scout.store.snapshots import Snapshot, write_batch
from twitch_scout.store.steam import SteamGame, upsert_steam_games
from twitch_scout.store.watchlist import WatchEntry, add_watch, list_watch, remove_watch
from twitch_scout.twitch.models import HelixGame
from twitch_scout.watchlist import add_to_watchlist

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
CLOCK = FrozenClock(NOW)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


class FakeHelix:
    """Get Games is an exact case-insensitive match; search returns configured names."""

    def __init__(self, categories: dict[str, str], search: dict[str, list[str]] | None = None):
        self._categories = categories  # name -> id
        self._search = search or {}

    def get_games_by_name(self, names: list[str]) -> list[HelixGame]:
        wanted = {n.casefold() for n in names}
        return [
            HelixGame(id=i, name=n) for n, i in self._categories.items() if n.casefold() in wanted
        ]

    def search_categories(self, query: str) -> list[HelixGame]:
        names = self._search.get(query, [])
        return [HelixGame(id=self._categories.get(n, f"s-{n}"), name=n) for n in names]


def _entry(game_id: str, name: str, requested: str | None = None, at: datetime = NOW) -> WatchEntry:
    return WatchEntry(game_id, name, requested or name, at)


# --- store ---


def test_add_is_idempotent_and_keeps_first_added_at(conn: sqlite3.Connection) -> None:
    assert add_watch(conn, _entry("1", "Tiny Bookshop")) is True
    later = NOW + timedelta(days=3)
    assert add_watch(conn, _entry("1", "Tiny Bookshop", "tiny bookshop", later)) is False
    [entry] = list_watch(conn)
    assert entry.added_at == NOW
    assert entry.requested_name == "tiny bookshop"


def test_remove_matches_twitch_or_typed_name_case_insensitively(conn: sqlite3.Connection) -> None:
    add_watch(conn, _entry("1", "Tiny Aquarium: Social Fishkeeping", "Tiny Aquarium"))
    add_watch(conn, _entry("2", "Anime Shop Simulator"))
    assert remove_watch(conn, "tiny aquarium") == ["Tiny Aquarium: Social Fishkeeping"]
    assert remove_watch(conn, "ANIME SHOP SIMULATOR") == ["Anime Shop Simulator"]
    assert remove_watch(conn, "Nothing") == []
    assert list_watch(conn) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"twitch_game_id": ""}, "twitch_game_id"),
        ({"twitch_game_name": " "}, "twitch_game_name"),
        ({"requested_name": ""}, "requested_name"),
    ],
)
def test_invalid_entry_rejected(kwargs: dict[str, str], message: str) -> None:
    base: dict[str, object] = {
        "twitch_game_id": "1",
        "twitch_game_name": "Game",
        "requested_name": "Game",
        "added_at": NOW,
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        WatchEntry(**base)  # type: ignore[arg-type]


# --- watch add ---


def test_add_resolves_exact_and_search_names(conn: sqlite3.Connection) -> None:
    helix = FakeHelix(
        {"Anime Shop Simulator": "10", "Tiny Aquarium: Social Fishkeeping": "11"},
        search={"Tiny Aquarium": ["Tiny Aquarium: Social Fishkeeping"]},
    )
    outcomes = add_to_watchlist(["anime shop simulator", "Tiny Aquarium"], helix, conn, CLOCK, {})
    assert [(o.game.name if o.game else None, o.via, o.new) for o in outcomes] == [
        ("Anime Shop Simulator", None, True),
        ("Tiny Aquarium: Social Fishkeeping", "search", True),
    ]
    assert {e.twitch_game_id for e in list_watch(conn)} == {"10", "11"}


def test_unresolved_name_is_not_stored_and_returns_suggestions(conn: sqlite3.Connection) -> None:
    helix = FakeHelix({}, search={"Anime Shop": ["Anime Shop Simulator", "Anime Shop Tycoon"]})
    [outcome] = add_to_watchlist(["Anime Shop"], helix, conn, CLOCK, {})
    assert outcome.game is None
    assert outcome.suggestions == ("Anime Shop Simulator", "Anime Shop Tycoon")
    assert list_watch(conn) == []


def test_blank_names_are_skipped(conn: sqlite3.Connection) -> None:
    assert add_to_watchlist(["  "], FakeHelix({}), conn, CLOCK, {}) == []


# --- collection and ranking ---


def _seed_window(
    conn: sqlite3.Connection, game_id: str, name: str, viewers: int, channels: int
) -> None:
    for day in (1, 2, 3):
        write_batch(
            conn,
            NOW - timedelta(days=day),
            Tier.WINDOW,
            [Snapshot(game_id, name, viewers, channels)],
        )


def test_candidates_union_steam_and_watchlist_deduped(conn: sqlite3.Connection) -> None:
    upsert_steam_games(conn, [SteamGame(1, "Owned", 60, "o1", "Owned")], NOW)
    add_watch(conn, _entry("w1", "Watched"))
    add_watch(conn, _entry("o1", "Owned"))  # also owned: must not be sampled twice
    ids = [g.id for g in load_candidates(conn)]
    assert sorted(ids) == ["o1", "w1"]


def test_watchlist_section_surfaces_a_small_watched_game(conn: sqlite3.Connection) -> None:
    _seed_window(conn, "w1", "Tiny Bookshop", viewers=40, channels=4)  # below the strict floor
    add_watch(conn, _entry("w1", "Tiny Bookshop"))
    result = rank_candidates(conn, CLOCK, RankConfig())
    assert "w1" not in {c.game_id for c in result.candidates}
    assert [c.game_id for c in result.watched] == ["w1"]


def test_owned_game_on_the_watchlist_shows_only_as_owned(conn: sqlite3.Connection) -> None:
    _seed_window(conn, "o1", "Owned Sim", viewers=40, channels=4)
    upsert_steam_games(conn, [SteamGame(1, "Owned Sim", 600, "o1", "Owned Sim")], NOW)
    add_watch(conn, _entry("o1", "Owned Sim"))
    result = rank_candidates(conn, CLOCK, RankConfig())
    assert [c.game_id for c in result.owned] == ["o1"]
    assert result.watched == []


def test_watched_game_without_samples_is_listed_as_unsampled(conn: sqlite3.Connection) -> None:
    add_watch(conn, _entry("w9", "Brand New Game"))
    assert rank_candidates(conn, CLOCK, RankConfig()).watch_unsampled == ["Brand New Game"]


def test_watch_section_can_be_disabled(conn: sqlite3.Connection) -> None:
    _seed_window(conn, "w1", "Tiny Bookshop", viewers=40, channels=4)
    add_watch(conn, _entry("w1", "Tiny Bookshop"))
    assert rank_candidates(conn, CLOCK, RankConfig(watch_guards=None)).watched == []
