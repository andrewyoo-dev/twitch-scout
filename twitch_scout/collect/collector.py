"""One-shot collector: the body of ``scout collect``.

Assembles the pieces — clock, tier decision, Helix client, store — into a single
sample run:

    resolve slot -> skip if already sampled -> Get Top Games
                 -> Get Streams per game (aggregate) -> write one batch

Failure policy (handoff section 2, 9):
  * A per-game ``Get Streams`` failure is logged and skipped; one flaky category
    never aborts the whole sample.
  * A ``Get Top Games`` failure aborts the run (nothing is written) — there is no
    meaningful partial sample without the game list. The caller reports it.
  * The batch is written once, at the end, keyed on the slot timestamp, so a crash
    mid-gather leaves no partial batch and the next run redoes the slot.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from twitch_scout.clock import Clock
from twitch_scout.collect.tiers import (
    DEFAULT_SCHEDULE,
    Slot,
    StreamSchedule,
    Tier,
    resolve,
    slot_for,
)
from twitch_scout.store.snapshots import Snapshot, has_batch, write_batch
from twitch_scout.twitch.client import StreamsResult, TwitchError
from twitch_scout.twitch.models import HelixGame

logger = logging.getLogger(__name__)


class SupportsHelix(Protocol):
    """The slice of the Helix client the collector needs. Lets tests inject a fake
    without the HTTP machinery; HelixClient satisfies it structurally."""

    def get_top_games(self, limit: int) -> list[HelixGame]: ...

    def get_streams(self, game_id: str, *, max_pages: int) -> StreamsResult: ...


@dataclass(frozen=True)
class CollectorConfig:
    top_n: int = 500
    streams_max_pages: int = 3
    # Per-stream collection (language, individual viewer counts) is designed for
    # but not yet wired to a table. Setting this fails loud rather than silently
    # doing nothing (handoff open questions; coding standard 4).
    per_stream: bool = False

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError("top_n must be > 0")
        if self.streams_max_pages <= 0:
            raise ValueError("streams_max_pages must be > 0")


@dataclass(frozen=True)
class CollectResult:
    slot: Slot
    skipped: bool
    games_seen: int
    games_written: int
    games_failed: int


class Collector:
    def __init__(
        self,
        client: SupportsHelix,
        conn: sqlite3.Connection,
        clock: Clock,
        *,
        config: CollectorConfig | None = None,
        schedule: StreamSchedule = DEFAULT_SCHEDULE,
    ) -> None:
        self._client = client
        self._conn = conn
        self._clock = clock
        self._config = config or CollectorConfig()
        self._schedule = schedule

    def run(self, *, force: bool = False, tier: Tier | None = None) -> CollectResult:
        if self._config.per_stream:
            raise NotImplementedError("per-stream collection is not implemented yet")

        # tier=None: let the clock decide (the cron path). Otherwise force the tier.
        slot = (
            resolve(self._clock, self._schedule)
            if tier is None
            else slot_for(self._clock.now(), tier, self._schedule)
        )

        if not force and has_batch(self._conn, slot.ts):
            logger.info("slot %s (%s) already sampled; skipping", slot.ts.isoformat(), slot.tier)
            return CollectResult(slot, skipped=True, games_seen=0, games_written=0, games_failed=0)

        # A Get Top Games failure propagates: no game list, no sample.
        games = self._client.get_top_games(self._config.top_n)

        rows: list[Snapshot] = []
        failed = 0
        for game in games:  # bounded by top_n
            try:
                streams = self._client.get_streams(
                    game.id, max_pages=self._config.streams_max_pages
                )
            except TwitchError as exc:
                # Log and skip this category; keep sampling the rest (standard 2).
                failed += 1
                logger.warning("get_streams failed for %s (%s): %s", game.name, game.id, exc)
                continue
            rows.append(
                Snapshot(
                    game_id=game.id,
                    game_name=game.name,
                    viewers=streams.viewers,
                    channels=streams.channels,
                    truncated=streams.truncated,
                )
            )

        written = write_batch(self._conn, slot.ts, slot.tier, rows)
        logger.info(
            "sampled slot %s (%s): %d written, %d failed of %d games",
            slot.ts.isoformat(),
            slot.tier,
            written,
            failed,
            len(games),
        )
        return CollectResult(
            slot,
            skipped=False,
            games_seen=len(games),
            games_written=written,
            games_failed=failed,
        )
