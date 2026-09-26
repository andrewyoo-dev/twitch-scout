"""Watchlist persistence: Twitch categories the streamer wants tracked.

Unlike the Steam library, a watchlist entry is keyed by the Twitch category id, because
that is what gets sampled and a watched game need not be on Steam or owned. The
collector samples every entry during the window tier; rank shows them in their own
section.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from twitch_scout.store.db import Connection, from_iso, to_iso, transaction


@dataclass(frozen=True)
class WatchEntry:
    twitch_game_id: str
    twitch_game_name: str
    requested_name: str
    added_at: datetime

    def __post_init__(self) -> None:
        if not self.twitch_game_id:
            raise ValueError("twitch_game_id must not be empty")
        if not self.twitch_game_name.strip():
            raise ValueError("twitch_game_name must not be blank")
        if not self.requested_name.strip():
            raise ValueError("requested_name must not be blank")


_UPSERT = """
INSERT INTO watchlist (twitch_game_id, twitch_game_name, requested_name, added_at)
VALUES (?, ?, ?, ?)
ON CONFLICT (twitch_game_id) DO UPDATE SET
    twitch_game_name = excluded.twitch_game_name,
    requested_name   = excluded.requested_name
"""


def add_watch(conn: Connection, entry: WatchEntry) -> bool:
    """Add (or refresh) an entry. Returns True if it was new.

    Re-adding keeps the original ``added_at`` so the history of when it was first
    watched is preserved.
    """
    existed = (
        conn.execute(
            "SELECT 1 FROM watchlist WHERE twitch_game_id = ?", (entry.twitch_game_id,)
        ).fetchone()
        is not None
    )
    with transaction(conn):
        conn.execute(
            _UPSERT,
            (
                entry.twitch_game_id,
                entry.twitch_game_name,
                entry.requested_name,
                to_iso(entry.added_at),
            ),
        )
    return not existed


def remove_watch(conn: Connection, name: str) -> list[str]:
    """Remove entries whose Twitch or requested name matches ``name`` (case-insensitive).

    Returns the Twitch names removed (empty if nothing matched).
    """
    wanted = name.strip().casefold()
    matches = [
        e
        for e in list_watch(conn)
        if wanted in (e.twitch_game_name.casefold(), e.requested_name.casefold())
    ]
    if not matches:
        return []
    with transaction(conn):
        conn.executemany(
            "DELETE FROM watchlist WHERE twitch_game_id = ?",
            [(e.twitch_game_id,) for e in matches],
        )
    return [e.twitch_game_name for e in matches]


def list_watch(conn: Connection) -> list[WatchEntry]:
    """Every entry, oldest first."""
    cursor = conn.execute(
        "SELECT twitch_game_id, twitch_game_name, requested_name, added_at "
        "FROM watchlist ORDER BY added_at, twitch_game_name"
    )
    return [
        WatchEntry(
            twitch_game_id=row[0],
            twitch_game_name=row[1],
            requested_name=row[2],
            added_at=from_iso(row[3]),
        )
        for row in cursor
    ]
