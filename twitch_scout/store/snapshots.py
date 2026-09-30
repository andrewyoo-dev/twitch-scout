"""Snapshot persistence: idempotent batch writes and read helpers.

A *batch* is one sample run: every row shares the slot timestamp produced by
:mod:`twitch_scout.collect.tiers`. Re-running a batch (the collector gets restarted
mid-run — handoff section 9) overwrites rather than duplicates, keyed on (ts, game_id).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from twitch_scout.collect.tiers import Tier
from twitch_scout.store.db import Connection, from_iso, to_iso, transaction


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


_COLUMNS = 7
# Rows per INSERT statement. The Turso driver sends one HTTP request per statement (its
# executemany loops row by row), so a 500-row batch cost ~500 sequential round trips and
# ~4-5 minutes per collect. Multi-row VALUES cuts that to a handful of requests. 100 rows
# x 7 params = 700 bound values, under SQLite's oldest variable limit (999).
_ROWS_PER_STATEMENT = 100

_UPSERT_TAIL = """
ON CONFLICT (ts, game_id) DO UPDATE SET
    tier      = excluded.tier,
    game_name = excluded.game_name,
    viewers   = excluded.viewers,
    channels  = excluded.channels,
    truncated = excluded.truncated
"""


def _upsert_sql(row_count: int) -> str:
    values = ", ".join(["(" + ", ".join(["?"] * _COLUMNS) + ")"] * row_count)
    return (
        "INSERT INTO snapshots (ts, tier, game_id, game_name, viewers, channels, truncated) "
        f"VALUES {values}{_UPSERT_TAIL}"
    )


def write_batch(
    conn: Connection,
    ts: datetime,
    tier: Tier,
    rows: list[Snapshot],
) -> int:
    """Write one sample batch idempotently. Returns the number of rows upserted.

    All rows go in a single transaction so a batch is all-or-nothing; a crash
    mid-write leaves no partial batch behind. Rows are deduplicated by game id first
    (the last reading wins, as row-by-row upserts would): a multi-row upsert cannot
    touch the same key twice, and Helix pagination can repeat a game whose rank moved
    between pages.
    """
    ts_iso = to_iso(ts)
    latest = {r.game_id: r for r in rows}
    params = [
        (ts_iso, str(tier), r.game_id, r.game_name, r.viewers, r.channels, int(r.truncated))
        for r in latest.values()
    ]
    with transaction(conn):
        # Bounded by len(params) / _ROWS_PER_STATEMENT (coding standard 1).
        for start in range(0, len(params), _ROWS_PER_STATEMENT):
            chunk = params[start : start + _ROWS_PER_STATEMENT]
            conn.execute(_upsert_sql(len(chunk)), [value for row in chunk for value in row])
    return len(params)


def has_batch(conn: Connection, ts: datetime) -> bool:
    """Whether any row already exists for this slot — lets the collector skip a
    slot that was already sampled instead of spending API calls to overwrite it."""
    row = conn.execute("SELECT 1 FROM snapshots WHERE ts = ? LIMIT 1", (to_iso(ts),)).fetchone()
    return row is not None


def read_batch(conn: Connection, ts: datetime) -> list[Snapshot]:
    """Read back one batch's rows, ordered by viewers descending.

    Columns are read positionally so the same code works whether the backend
    returns tuples (Turso) or sqlite3.Row.
    """
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
            game_id=row[0],
            game_name=row[1],
            viewers=row[2],
            channels=row[3],
            truncated=bool(row[4]),
        )
        for row in cursor
    ]


def batch_timestamps(conn: Connection) -> list[datetime]:
    """Every distinct batch timestamp on record, oldest first."""
    cursor = conn.execute("SELECT DISTINCT ts FROM snapshots ORDER BY ts")
    return [from_iso(row[0]) for row in cursor]
