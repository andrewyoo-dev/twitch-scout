"""Extra categories the collector samples during the window tier, beyond the top games.

The source is the resolved Steam library (already one row per Twitch category); the
collector dedupes it against the top games it fetched.
"""

from __future__ import annotations

from twitch_scout.store.db import Connection
from twitch_scout.store.steam import fetch_candidates
from twitch_scout.twitch.models import HelixGame


def load_candidates(conn: Connection) -> list[HelixGame]:
    return [HelixGame(id=c.twitch_game_id, name=c.twitch_game_name) for c in fetch_candidates(conn)]
