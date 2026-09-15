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
"""

from __future__ import annotations

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
    _migrate(conn)
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


def _migrate(conn: Connection) -> None:
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
