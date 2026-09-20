"""Database backend: SQLite locally, Turso (libSQL) in production.

The store keeps raw sample rows and nothing derived — averages, trends and floors
are all queries over ``snapshots`` (handoff section 5). Keeping the source samples
means the metrics can change later without having lost data.

Two backends behind one :func:`connect`, chosen by the ``SCOUT_DB`` value:

  * a filesystem path (or ``:memory:``) -> stdlib ``sqlite3`` — dev and tests.
  * a ``libsql://`` / ``https://`` URL -> Turso over the wire — the GitHub Actions
    collector, whose runner disk is thrown away each run.

The rest of the store depends only on the small :class:`Connection` protocol and the
common DB-API subset (``execute``/``executemany``/``commit``/``rollback``), so the
same SQL and code paths run on both. Transactions use ``commit``/``rollback`` with
the connection in explicit-transaction mode, which behaves the same on each backend.

Schema version is tracked in the ``meta`` table rather than ``PRAGMA user_version``:
Turso rejects writing that PRAGMA ("SQL not allowed statement"), and a plain row
works identically on both backends.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

# Busy timeout for the local SQLite backend: the collector may briefly meet a
# reader (coding standard 2 — every I/O call gets a timeout).
_BUSY_TIMEOUT_S = 30

_TURSO_SCHEMES = ("libsql://", "https://", "http://")


class StoreError(RuntimeError):
    """A storage backend could not be opened (misconfiguration or missing driver)."""


class Connection(Protocol):
    """The DB-API subset the store uses; satisfied by both sqlite3 and Turso."""

    def execute(self, sql: str, parameters: Any = ..., /) -> Any: ...

    def executemany(self, sql: str, parameters: Any, /) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


# Each entry is one migration: a tuple of statements applied together. The applied
# count is tracked in the meta table. Append-only; never edit an entry that shipped.
#
# Every statement must be idempotent (IF NOT EXISTS, etc.): Turso does not roll back
# DDL, so a migration that fails partway leaves its earlier statements committed, and
# the retry must be able to re-run them harmlessly.
_SCHEMA_KEY = "schema_version"
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE IF NOT EXISTS snapshots (
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
        "CREATE INDEX IF NOT EXISTS idx_snap_game_ts ON snapshots (game_id, ts)",
        "CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots (ts)",
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ),
    (
        # Steam library as a candidate SOURCE: owned (later watchlist/wishlist) games,
        # each resolved to a Twitch category id when a match exists. The collector
        # samples the resolved rows alongside top-N during the window tier; rank joins
        # on twitch_game_id to surface owned games in their own section.
        """
        CREATE TABLE IF NOT EXISTS steam_games (
            appid            INTEGER PRIMARY KEY,   -- Steam AppID
            name             TEXT    NOT NULL,      -- Steam's name for the game
            playtime_minutes INTEGER NOT NULL DEFAULT 0,
            twitch_game_id   TEXT,                  -- resolved Twitch category id (NULL if none)
            twitch_game_name TEXT,                  -- Twitch's canonical name (display / dedupe)
            source           TEXT    NOT NULL DEFAULT 'owned',  -- owned | watchlist | wishlist
            synced_at        TEXT    NOT NULL       -- ISO8601 UTC of the last steam-sync
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_steam_twitch_game ON steam_games (twitch_game_id)",
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
def transaction(conn: Connection) -> Iterator[None]:
    """Run a block in one transaction; commit on success, roll back on any error.

    Relies on explicit-transaction mode (never autocommit), so the statements in the
    block — DDL included — commit atomically together.
    """
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def is_turso_url(database: str) -> bool:
    return database.startswith(_TURSO_SCHEMES)


def connect(database: str | Path, *, auth_token: str | None = None) -> Connection:
    """Open a connection to the configured backend and migrate to the current schema."""
    conn = (
        _connect_turso(str(database), auth_token)
        if isinstance(database, str) and is_turso_url(database)
        else _connect_sqlite(database)
    )
    try:
        _migrate(conn)
    except BaseException:
        # Migration failed after the handle was opened; close it so we don't leak the
        # connection (and, for the local sqlite backend, the file lock it holds — which
        # otherwise blocks the caller from deleting or reopening the database).
        with contextlib.suppress(Exception):
            conn.close()
        raise
    return conn


def _connect_sqlite(database: str | Path) -> Connection:
    # autocommit=False (PEP 249 mode) keeps one explicit transaction open so DDL and
    # multi-row writes commit atomically — the legacy sqlite3 mode would auto-commit
    # DDL and break migration/batch atomicity.
    return sqlite3.connect(database, timeout=_BUSY_TIMEOUT_S, autocommit=False)


def _connect_turso(url: str, auth_token: str | None) -> Connection:
    if not auth_token:
        raise StoreError("a Turso URL requires an auth token (TURSO_AUTH_TOKEN)")
    try:
        import turso_serverless  # noqa: PLC0415 -- optional dependency, imported lazily
    except ImportError as exc:
        raise StoreError(
            "Turso backend requires the 'turso' extra: pip install twitch-scout[turso]"
        ) from exc
    return cast("Connection", turso_serverless.connect(url, auth_token=auth_token))


def schema_version(conn: Connection) -> int:
    """The applied schema version, read from the meta table (0 if not yet created)."""
    if not _table_exists(conn, "meta"):
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_SCHEMA_KEY,)).fetchone()
    return int(row[0]) if row else 0


def _table_exists(conn: Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _migrate(conn: Connection) -> None:
    current = schema_version(conn)
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
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (_SCHEMA_KEY, str(version + 1)),
            )
