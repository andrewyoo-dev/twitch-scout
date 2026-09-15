"""Tests for the snapshot store.

The load-bearing property is idempotency: the collector gets restarted mid-run, so
re-writing the same batch must overwrite, never duplicate (handoff section 9).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from twitch_scout.collect.tiers import Tier
from twitch_scout.store.db import (
    SCHEMA_VERSION,
    StoreError,
    connect,
    from_iso,
    is_turso_url,
    to_iso,
)
from twitch_scout.store.snapshots import (
    Snapshot,
    batch_timestamps,
    has_batch,
    read_batch,
    write_batch,
)

TS = datetime(2026, 9, 16, 2, 10, tzinfo=UTC)
TS2 = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def _rows() -> list[Snapshot]:
    return [
        Snapshot("111", "Job Simulator", viewers=140, channels=8),
        Snapshot("222", "TCG Card Shop Simulator", viewers=82, channels=12, truncated=True),
    ]


# --- schema / migrations ---


def test_connect_sets_schema_version(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION == 1


def test_migrations_are_idempotent_across_reconnects(tmp_path: Path) -> None:
    db = tmp_path / "scout.db"
    first = connect(db)
    write_batch(first, TS, Tier.WINDOW, _rows())
    first.close()
    # Reopening an existing DB must not re-run migrations or lose data.
    second = connect(db)
    assert second.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert len(read_batch(second, TS)) == 2
    second.close()


def test_schema_newer_than_code_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "scout.db"
    connect(db).close()
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="newer than this code"):
        connect(db)


# --- backend dispatch ---


@pytest.mark.parametrize(
    ("database", "expected"),
    [
        ("libsql://db-org.turso.io", True),
        ("https://db-org.turso.io", True),
        ("scout.db", False),
        (":memory:", False),
        ("/data/scout.db", False),
    ],
)
def test_is_turso_url(database: str, expected: bool) -> None:
    assert is_turso_url(database) is expected


def test_turso_url_without_token_is_rejected() -> None:
    with pytest.raises(StoreError, match="auth token"):
        connect("libsql://db-org.turso.io")


def test_turso_url_without_package_gives_install_hint() -> None:
    # turso_serverless is not installed in the dev env; the error should say how.
    with pytest.raises(StoreError, match="pip install twitch-scout"):
        connect("libsql://db-org.turso.io", auth_token="tok")


# --- write / read ---


def test_write_and_read_batch(conn: sqlite3.Connection) -> None:
    written = write_batch(conn, TS, Tier.WINDOW, _rows())
    assert written == 2
    rows = read_batch(conn, TS)
    assert [r.game_id for r in rows] == ["111", "222"]  # viewers DESC
    assert rows[1].truncated is True


def test_tier_is_stored_as_its_string_value(conn: sqlite3.Connection) -> None:
    write_batch(conn, TS, Tier.BASELINE, _rows())
    stored = {row[0] for row in conn.execute("SELECT DISTINCT tier FROM snapshots")}
    assert stored == {"baseline"}


def test_rewriting_a_batch_overwrites_not_duplicates(conn: sqlite3.Connection) -> None:
    write_batch(conn, TS, Tier.WINDOW, _rows())
    # Same slot, updated readings (a restarted collector re-samples the slot).
    updated = [
        Snapshot("111", "Job Simulator", viewers=200, channels=10),
        Snapshot("222", "TCG Card Shop Simulator", viewers=90, channels=13),
    ]
    write_batch(conn, TS, Tier.WINDOW, updated)
    rows = read_batch(conn, TS)
    assert len(rows) == 2  # not 4
    assert rows[0].viewers == 200  # overwritten
    total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert total == 2


def test_distinct_batches_accumulate(conn: sqlite3.Connection) -> None:
    write_batch(conn, TS, Tier.WINDOW, _rows())
    write_batch(conn, TS2, Tier.BASELINE, _rows())
    assert batch_timestamps(conn) == [TS, TS2]
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 4


def test_empty_batch_writes_nothing(conn: sqlite3.Connection) -> None:
    assert write_batch(conn, TS, Tier.WINDOW, []) == 0
    assert has_batch(conn, TS) is False


# --- has_batch (the collector's skip signal) ---


def test_has_batch(conn: sqlite3.Connection) -> None:
    assert has_batch(conn, TS) is False
    write_batch(conn, TS, Tier.WINDOW, _rows())
    assert has_batch(conn, TS) is True
    assert has_batch(conn, TS2) is False


# --- timestamp handling ---


def test_timestamps_round_trip_as_utc(conn: sqlite3.Connection) -> None:
    # A non-UTC aware input is normalized to UTC on the way in and comes back UTC.
    from zoneinfo import ZoneInfo

    pacific_ts = datetime(2026, 9, 15, 19, 10, tzinfo=ZoneInfo("America/Los_Angeles"))
    write_batch(conn, pacific_ts, Tier.WINDOW, _rows())
    stored = batch_timestamps(conn)[0]
    assert stored == pacific_ts  # same instant
    assert stored.utcoffset().total_seconds() == 0  # ...represented as UTC


def test_naive_timestamp_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        write_batch(conn, datetime(2026, 9, 16, 2, 10), Tier.WINDOW, _rows())  # noqa: DTZ001


def test_iso_helpers_reject_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_iso(datetime(2026, 9, 16, 2, 10))  # noqa: DTZ001
    assert from_iso("2026-09-16T02:10:00+00:00") == TS


# --- Snapshot validation ---


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"game_id": ""}, "game_id"),
        ({"game_name": "   "}, "game_name"),
        ({"viewers": -1}, "viewers"),
        ({"channels": -1}, "channels"),
    ],
)
def test_invalid_snapshot_rejected(kwargs: dict[str, object], message: str) -> None:
    base = {"game_id": "1", "game_name": "Game", "viewers": 10, "channels": 2}
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        Snapshot(**base)  # type: ignore[arg-type]
