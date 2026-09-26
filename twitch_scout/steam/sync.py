"""steam-sync orchestration: owned games -> Twitch category ids -> stored library.

The flow, wired from injected collaborators so it is testable without any network:

    resolve steam id -> GetOwnedGames -> resolve names to Twitch ids -> upsert

Name resolution (``steam/resolve.py``) runs conservative tiers: a reviewed alias file,
exact match, separator variants, edition stripping, and a verified category search. An
unresolved game is stored with a null twitch id rather than dropped, so it keeps its
playtime and can be resolved later (a new rule or an alias).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from twitch_scout.clock import Clock
from twitch_scout.steam.client import OwnedLibrary
from twitch_scout.steam.resolve import SupportsGameLookup, resolve_names
from twitch_scout.store.db import Connection
from twitch_scout.store.steam import SteamGame, replace_owned_games

logger = logging.getLogger(__name__)

_OWNED = "owned"

__all__ = [
    "SupportsGameLookup",
    "SupportsOwnedGames",
    "SyncResult",
    "sync_owned_games",
]


class SupportsOwnedGames(Protocol):
    def resolve_steam_id(self, id_or_vanity: str) -> str: ...

    def get_owned_games(self, steam_id: str) -> OwnedLibrary: ...


@dataclass(frozen=True)
class SyncResult:
    owned: int  # games in the library
    resolved: int  # games matched to a Twitch category
    unresolved: int  # games with no Twitch match (stored, but not sampled)
    written: int  # rows upserted
    complete: bool = True  # False if the fetch was partial (pruning was skipped)
    skipped: int = 0  # malformed items the Steam response dropped
    ignored: int = 0  # test/beta builds, not looked up
    # (steam name, twitch name, tier) for every non-exact match, so a wrong one is visible
    fallbacks: tuple[tuple[str, str, str], ...] = ()


def sync_owned_games(  # noqa: PLR0913 -- injected collaborators plus the steam id and aliases
    steam: SupportsOwnedGames,
    lookup: SupportsGameLookup,
    conn: Connection,
    clock: Clock,
    *,
    steam_id: str,
    aliases: Mapping[str, str] | None = None,
) -> SyncResult:
    """Fetch the owned library, resolve names to Twitch ids, and upsert it."""
    resolved_id = steam.resolve_steam_id(steam_id)
    library = steam.get_owned_games(resolved_id)
    games = library.games
    resolution = resolve_names(games, lookup, aliases or {})

    rows: list[SteamGame] = []
    fallbacks: list[tuple[str, str, str]] = []
    for game in games:
        match = resolution.matches.get(game.appid)
        if match is not None and game.appid in resolution.via:
            fallbacks.append((game.name, match.name, resolution.via[game.appid]))
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

    # Replace (not just upsert) so games no longer owned stop being candidates, but
    # only prune on a complete fetch: pruning from a partial response would delete a
    # still-owned game whose item was merely dropped as malformed.
    written = replace_owned_games(conn, rows, clock.now(), prune=library.complete)
    resolved = len(resolution.matches)
    ignored = len(resolution.ignored)
    return SyncResult(
        owned=len(games),
        resolved=resolved,
        unresolved=len(games) - resolved - ignored,
        written=written,
        complete=library.complete,
        skipped=library.skipped,
        ignored=ignored,
        fallbacks=tuple(fallbacks),
    )
