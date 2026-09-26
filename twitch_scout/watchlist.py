"""`scout watch add`: resolve requested game names to Twitch categories and store them.

Names go through the same conservative tiers as the Steam library (steam/resolve.py),
so a watched game is matched as safely as an owned one. A name that does not resolve
is not guessed: the caller gets the closest search candidates to retry with.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from twitch_scout.clock import Clock
from twitch_scout.steam.resolve import SupportsGameLookup, resolve_titles
from twitch_scout.store.db import Connection
from twitch_scout.store.watchlist import WatchEntry, add_watch
from twitch_scout.twitch.models import HelixGame

_SUGGESTIONS = 5


@dataclass(frozen=True)
class AddOutcome:
    requested: str
    game: HelixGame | None  # None if no verified category was found
    via: str | None = None  # tier for a non-exact match
    new: bool = False  # False if it was already on the watchlist
    suggestions: tuple[str, ...] = ()  # closest categories when unresolved


def add_to_watchlist(
    names: list[str],
    lookup: SupportsGameLookup,
    conn: Connection,
    clock: Clock,
    aliases: Mapping[str, str],
) -> list[AddOutcome]:
    """Resolve and store each requested name; one outcome per non-blank name."""
    titles = {i: name.strip() for i, name in enumerate(names) if name.strip()}
    resolution = resolve_titles(titles, lookup, aliases)
    outcomes: list[AddOutcome] = []
    for key, requested in titles.items():
        game = resolution.matches.get(key)
        if game is None:
            # Unique names only: search can list stale duplicates of one category.
            unique = dict.fromkeys(g.name for g in lookup.search_categories(requested))
            outcomes.append(AddOutcome(requested, None, suggestions=tuple(unique)[:_SUGGESTIONS]))
            continue
        new = add_watch(conn, WatchEntry(game.id, game.name, requested, clock.now()))
        outcomes.append(AddOutcome(requested, game, via=resolution.via.get(key), new=new))
    return outcomes
