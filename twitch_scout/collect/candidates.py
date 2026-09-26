"""Extra categories the collector samples during the window tier, beyond top-N.

Two sources feed it: the resolved Steam library and the watchlist. Both are deduped by
Twitch category id here; the collector dedupes again against top-N.
"""

from __future__ import annotations

from twitch_scout.store.db import Connection
from twitch_scout.store.steam import fetch_candidates
from twitch_scout.store.watchlist import list_watch
from twitch_scout.twitch.models import HelixGame


def load_candidates(conn: Connection) -> list[HelixGame]:
    games = {
        c.twitch_game_id: HelixGame(id=c.twitch_game_id, name=c.twitch_game_name)
        for c in fetch_candidates(conn)
    }
    for entry in list_watch(conn):
        games.setdefault(
            entry.twitch_game_id, HelixGame(id=entry.twitch_game_id, name=entry.twitch_game_name)
        )
    return list(games.values())
