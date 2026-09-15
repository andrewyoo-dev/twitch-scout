"""Queries that turn raw snapshots into per-category aggregates.

Everything a candidate is judged on is derived here from the window-tier samples —
the decision-grade data collected at the streamer's actual hours (handoff section 5).
The clock is injected so the evaluation window is testable against fixtures.

SQL is kept to the portable subset (AVG/COUNT + conditional aggregation) so it runs
identically on SQLite and Turso. Percentile/median are computed in Python over the
small per-candidate series (see rank.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from twitch_scout.clock import Clock
from twitch_scout.collect.tiers import Tier
from twitch_scout.store.db import Connection, to_iso


@dataclass(frozen=True)
class GameAggregate:
    """Per-category window-tier aggregate over the evaluation window."""

    game_id: str
    game_name: str
    window_samples: int
    avg_viewers: float  # over the full evaluation window
    avg_channels: float
    avg_viewers_recent: float | None  # over the trend sub-window (None if no samples)


_AGGREGATE_SQL = """
SELECT
    game_id,
    MAX(game_name) AS game_name,
    COUNT(*) AS window_samples,
    AVG(viewers) AS avg_viewers,
    AVG(channels) AS avg_channels,
    AVG(CASE WHEN ts >= ? THEN viewers END) AS avg_viewers_recent
FROM snapshots
WHERE tier = ? AND ts >= ?
GROUP BY game_id
"""


def fetch_aggregates(
    conn: Connection,
    clock: Clock,
    *,
    eval_days: int,
    trend_window_days: int,
) -> list[GameAggregate]:
    """One GROUP BY over the window-tier samples in the last ``eval_days``."""
    if eval_days <= 0 or trend_window_days <= 0:
        raise ValueError("day windows must be > 0")
    now = clock.now()
    eval_since = to_iso(now - timedelta(days=eval_days))
    trend_since = to_iso(now - timedelta(days=trend_window_days))
    cursor = conn.execute(_AGGREGATE_SQL, (trend_since, str(Tier.WINDOW), eval_since))
    return [
        GameAggregate(
            game_id=row[0],
            game_name=row[1],
            window_samples=row[2],
            avg_viewers=row[3],
            avg_channels=row[4],
            avg_viewers_recent=row[5],
        )
        for row in cursor
    ]


def fetch_window_series(
    conn: Connection,
    clock: Clock,
    game_ids: list[str],
    *,
    eval_days: int,
) -> dict[str, list[int]]:
    """Window-tier viewer readings per game over the evaluation window, oldest first.

    Returns only the requested games (typically the guard-eligible set), so the
    IN-list stays small. Used for the robust floor, the spike median, and the latest
    reading — all computed in Python.
    """
    if not game_ids:
        return {}
    since = to_iso(clock.now() - timedelta(days=eval_days))
    placeholders = ",".join("?" for _ in game_ids)
    sql = (
        "SELECT game_id, viewers FROM snapshots "
        f"WHERE tier = ? AND ts >= ? AND game_id IN ({placeholders}) "
        "ORDER BY game_id, ts"
    )
    cursor = conn.execute(sql, (str(Tier.WINDOW), since, *game_ids))
    series: dict[str, list[int]] = {game_id: [] for game_id in game_ids}
    for game_id, viewers in cursor:
        series[game_id].append(viewers)
    return series
