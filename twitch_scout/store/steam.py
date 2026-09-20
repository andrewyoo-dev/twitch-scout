"""Steam library persistence: the candidate-source table.

Owned (and later watchlist/wishlist) games live in ``steam_games``, each resolved to
a Twitch category id when a match exists. Two consumers read it:

  * the collector samples the resolved rows alongside top-N during the window tier,
    so a game that never enters the top-500 still gets window-tier data;
  * rank joins on ``twitch_game_id`` to surface owned games in their own section.

Everything here is the common DB-API subset used elsewhere in the store, so the same
code runs on sqlite and Turso.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from twitch_scout.store.db import Connection, to_iso, transaction


@dataclass(frozen=True)
class SteamGame:
    """One row of the Steam library, validated at construction (coding standard 3).

    ``twitch_game_id``/``twitch_game_name`` are None until name resolution finds a
    matching Twitch category; an unresolved game is still stored so a later sync (or
    a manual mapping) can fill it in without losing playtime.
    """

    appid: int
    name: str
    playtime_minutes: int = 0
    twitch_game_id: str | None = None
    twitch_game_name: str | None = None
    source: str = "owned"

    def __post_init__(self) -> None:
        if self.appid <= 0:
            raise ValueError("appid must be > 0")
        if not self.name.strip():
            raise ValueError("name must not be blank")
        if self.playtime_minutes < 0:
            raise ValueError("playtime_minutes must be >= 0")
        if not self.source.strip():
            raise ValueError("source must not be blank")


@dataclass(frozen=True)
class OwnedGame:
    """A resolved candidate as the collector and rank consume it, keyed by Twitch id.

    Multiple Steam appids can map to one Twitch category (a game plus its demo, or a
    regional variant); these are already aggregated by ``twitch_game_id``.
    """

    twitch_game_id: str
    twitch_game_name: str
    playtime_minutes: int


_UPSERT = """
INSERT INTO steam_games
    (appid, name, playtime_minutes, twitch_game_id, twitch_game_name, source, synced_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (appid) DO UPDATE SET
    name             = excluded.name,
    playtime_minutes = excluded.playtime_minutes,
    twitch_game_id   = excluded.twitch_game_id,
    twitch_game_name = excluded.twitch_game_name,
    source           = excluded.source,
    synced_at        = excluded.synced_at
"""


def upsert_steam_games(conn: Connection, games: list[SteamGame], synced_at: datetime) -> int:
    """Upsert library rows idempotently (keyed on appid). Returns the row count.

    A re-run of steam-sync overwrites each game's playtime and resolution rather than
    duplicating it; the whole set goes in one transaction so a crash leaves the prior
    library intact.
    """
    synced = to_iso(synced_at)
    params = [
        (
            g.appid,
            g.name,
            g.playtime_minutes,
            g.twitch_game_id,
            g.twitch_game_name,
            g.source,
            synced,
        )
        for g in games
    ]
    with transaction(conn):
        conn.executemany(_UPSERT, params)
    return len(params)


def fetch_candidates(conn: Connection) -> list[OwnedGame]:
    """Every resolved library game (any source), deduped by Twitch category id.

    This is the collector's extra sampling set: all games that resolved to a Twitch
    category, so they are sampled during the window tier even when absent from top-N.
    Playtime is summed across appids that share a category.
    """
    return _fetch_resolved(conn, source=None)


def fetch_owned(conn: Connection) -> list[OwnedGame]:
    """Resolved games with source 'owned', for rank's owned section."""
    return _fetch_resolved(conn, source="owned")


def _fetch_resolved(conn: Connection, *, source: str | None) -> list[OwnedGame]:
    sql = (
        "SELECT twitch_game_id, MAX(twitch_game_name), SUM(playtime_minutes) "
        "FROM steam_games WHERE twitch_game_id IS NOT NULL"
    )
    params: tuple[str, ...] = ()
    if source is not None:
        sql += " AND source = ?"
        params = (source,)
    sql += " GROUP BY twitch_game_id"
    cursor = conn.execute(sql, params)
    return [
        OwnedGame(
            twitch_game_id=row[0],
            twitch_game_name=row[1],
            playtime_minutes=int(row[2]),
        )
        for row in cursor
    ]
