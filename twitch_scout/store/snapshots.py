"""Snapshot persistence: idempotent batch writes and read helpers.

A *batch* is one sample run: every row shares the slot timestamp produced by
:mod:`twitch_scout.collect.tiers`. Re-running a batch (the collector gets restarted
mid-run — handoff section 9) overwrites rather than duplicates, keyed on (ts, game_id).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from twitch_scout.collect.tiers import Tier
from twitch_scout.store.db import from_iso, to_iso, transaction


@dataclass(frozen=True)
class Snapshot:
    """One category's aggregated reading within a batch.

    Validated at construction so malformed readings never reach the DB (coding
    standard 3). The Twitch layer validates the API response; this is the second
    gate, guarding what actually gets persisted.
    """

    game_id: str
    game_name: str
    viewers: int
    channels: int
    truncated: bool = False

    def __post_init__(self) -> None:
        if not self.game_id:
            raise ValueError("game_id must not be empty")
        if not self.game_name.strip():
            raise ValueError("game_name must not be blank")
        if self.viewers < 0:
            raise ValueError("viewers must be >= 0")
        if self.channels < 0:
            raise ValueError("channels must be >= 0")


_UPSERT = """
INSERT INTO snapshots (ts, tier, game_id, game_name, viewers, channels, truncated)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (ts, game_id) DO UPDATE SET
    tier      = excluded.tier,
    game_name = excluded.game_name,
    viewers   = excluded.viewers,
    channels  = excluded.channels,
    truncated = excluded.truncated
"""


def write_batch(
    conn: sqlite3.Connection,
    ts: datetime,
    tier: Tier,
    rows: list[Snapshot],
) -> int:
    """Write one sample batch idempotently. Returns the number of rows upserted.

    All rows go in a single transaction so a batch is all-or-nothing; a crash
    mid-write leaves no partial batch behind.
    """
    ts_iso = to_iso(ts)
    params = [
        (ts_iso, str(tier), r.game_id, r.game_name, r.viewers, r.channels, int(r.truncated))
        for r in rows
    ]
    with transaction(conn):
        conn.executemany(_UPSERT, params)
    return len(params)


def has_batch(conn: sqlite3.Connection, ts: datetime) -> bool:
    """Whether any row already exists for this slot — lets the collector skip a
    slot that was already sampled instead of spending API calls to overwrite it."""
    row = conn.execute("SELECT 1 FROM snapshots WHERE ts = ? LIMIT 1", (to_iso(ts),)).fetchone()
    return row is not None


def read_batch(conn: sqlite3.Connection, ts: datetime) -> list[Snapshot]:
    """Read back one batch's rows, ordered by viewers descending."""
    cursor = conn.execute(
        """
        SELECT game_id, game_name, viewers, channels, truncated
        FROM snapshots WHERE ts = ?
        ORDER BY viewers DESC, game_id
        """,
        (to_iso(ts),),
    )
    return [
        Snapshot(
            game_id=row["game_id"],
            game_name=row["game_name"],
            viewers=row["viewers"],
            channels=row["channels"],
            truncated=bool(row["truncated"]),
        )
        for row in cursor
    ]


def batch_timestamps(conn: sqlite3.Connection) -> list[datetime]:
    """Every distinct batch timestamp on record, oldest first."""
    cursor = conn.execute("SELECT DISTINCT ts FROM snapshots ORDER BY ts")
    return [from_iso(row["ts"]) for row in cursor]
