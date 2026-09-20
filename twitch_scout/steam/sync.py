"""steam-sync orchestration: owned games -> Twitch category ids -> stored library.

The flow, wired from injected collaborators so it is testable without any network:

    resolve steam id -> GetOwnedGames -> resolve names to Twitch ids -> upsert

Name resolution is best-effort exact match (via Helix Get Games): Steam and Twitch
often spell a game identically, but not always, so an unresolved game is stored with
a null twitch id rather than dropped. It keeps its playtime and can be resolved by a
later sync or, once that lands, a manual mapping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from twitch_scout.clock import Clock
from twitch_scout.steam.client import OwnedSteamGame
from twitch_scout.store.db import Connection
from twitch_scout.store.steam import SteamGame, fetch_candidates, upsert_steam_games
from twitch_scout.twitch.models import HelixGame

logger = logging.getLogger(__name__)

_OWNED = "owned"


class SupportsOwnedGames(Protocol):
    def resolve_steam_id(self, id_or_vanity: str) -> str: ...

    def get_owned_games(self, steam_id: str) -> list[OwnedSteamGame]: ...


class SupportsGameLookup(Protocol):
    def get_games_by_name(self, names: list[str]) -> list[HelixGame]: ...


@dataclass(frozen=True)
class SyncResult:
    owned: int  # games in the library
    resolved: int  # games matched to a Twitch category
    unresolved: int  # games with no Twitch match (stored, but not sampled)
    written: int  # rows upserted


def sync_owned_games(
    steam: SupportsOwnedGames,
    lookup: SupportsGameLookup,
    conn: Connection,
    clock: Clock,
    *,
    steam_id: str,
) -> SyncResult:
    """Fetch the owned library, resolve names to Twitch ids, and upsert it."""
    resolved_id = steam.resolve_steam_id(steam_id)
    games = steam.get_owned_games(resolved_id)
    resolution = _resolve_to_twitch(games, lookup)

    rows: list[SteamGame] = []
    for game in games:
        match = resolution.get(game.appid)
        rows.append(
            SteamGame(
                appid=game.appid,
                name=game.name,
                playtime_minutes=game.playtime_minutes,
                twitch_game_id=match.id if match else None,
                twitch_game_name=match.name if match else None,
                source=_OWNED,
            )
        )

    written = upsert_steam_games(conn, rows, clock.now())
    return SyncResult(
        owned=len(games),
        resolved=len(resolution),
        unresolved=len(games) - len(resolution),
        written=written,
    )


def load_candidates(conn: Connection) -> list[HelixGame]:
    """Resolved library games as HelixGame, for the collector to sample."""
    return [
        HelixGame(id=candidate.twitch_game_id, name=candidate.twitch_game_name)
        for candidate in fetch_candidates(conn)
    ]


def _resolve_to_twitch(
    games: list[OwnedSteamGame], lookup: SupportsGameLookup
) -> dict[int, HelixGame]:
    """Map each owned appid to a Twitch game, where a name match exists.

    Get Games wants the exact category name, so the query preserves case (only
    trademark marks and stray whitespace are stripped); results are matched back
    case-insensitively via a casefolded key.
    """
    cleaned = {game.appid: _clean(game.name) for game in games}
    query_names = list({name for name in cleaned.values() if name})
    found = lookup.get_games_by_name(query_names)
    by_key = {_clean(game.name).casefold(): game for game in found}
    return {
        appid: by_key[name.casefold()]
        for appid, name in cleaned.items()
        if name and name.casefold() in by_key
    }


def _clean(name: str) -> str:
    """Drop trademark/registered/copyright marks and collapse whitespace (case kept).

    Twitch category names carry none of these marks, so removing them from the Steam
    name lifts the exact-match rate.
    """
    stripped = name.translate(dict.fromkeys(map(ord, "™®©")))
    return " ".join(stripped.split())
