"""SQLite connection factory, schema migrations, and timestamp helpers.

The store keeps raw sample rows and nothing derived — averages, trends and floors
are all queries over ``snapshots`` (handoff section 5). Keeping the source samples
means the metrics can change later without having lost data.

SQLite is the local/test backend; Turso (libSQL) is the same SQL over the wire in
production. Callers depend only on a DB-API ``Connection`` and the helpers here, so
swapping the backend is confined to :func:`connect`.

Transactions are managed explicitly (``isolation_level=None`` + :func:`transaction`)
because sqlite3's legacy autocommit does not wrap DDL, which would make migrations
non-atomic.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Busy timeout: the unattended collector may hit a DB briefly locked by a reader
# (coding standard 2 — every I/O call gets a timeout).
_BUSY_TIMEOUT_S = 30

# Each entry is one atomic migration: a tuple of statements applied together. The
# applied count is tracked in PRAGMA user_version. Append-only; never edit an entry
# that has shipped.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE snapshots (
            ts        TEXT    NOT NULL,   -- ISO8601 UTC, the batch/slot timestamp
            tier      TEXT    NOT NULL,   -- 'window' | 'baseline'
            game_id   TEXT    NOT NULL,
            game_name TEXT    NOT NULL,
            viewers   INTEGER NOT NULL,
            channels  INTEGER NOT NULL,
            truncated INTEGER NOT NULL DEFAULT 0,  -- 1 if the stream page cap was hit
            PRIMARY KEY (ts, game_id)     -- idempotency: one row per game per batch
        )
        """,
        "CREATE INDEX idx_snap_game_ts ON snapshots (game_id, ts)",
        "CREATE INDEX idx_snap_ts ON snapshots (ts)",
        """
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ),
)

SCHEMA_VERSION = len(MIGRATIONS)


def to_iso(dt: datetime) -> str:
    """Serialize an aware datetime to a canonical UTC ISO8601 string.

    Naive datetimes are rejected: storing one would bake in an undefined instant
    and break the idempotency key.
    """
    if dt.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return dt.astimezone(UTC).isoformat()


def from_iso(value: str) -> datetime:
    """Parse a stored ISO8601 timestamp back into an aware UTC datetime."""
    return datetime.fromisoformat(value).astimezone(UTC)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block inside a single explicit transaction; roll back on any error."""
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def connect(database: str | Path) -> sqlite3.Connection:
    """Open a connection, apply pragmas, and migrate to the current schema."""
    conn = sqlite3.connect(database, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # concurrent reader while collector writes
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_S * 1000}")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than this code (v{SCHEMA_VERSION}); "
            "upgrade the tool"
        )
    # Bounded by len(MIGRATIONS) (coding standard 1).
    for version in range(current, SCHEMA_VERSION):
        with transaction(conn):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            # PRAGMA user_version cannot be parameterized; version is a trusted int.
            conn.execute(f"PRAGMA user_version = {version + 1}")
