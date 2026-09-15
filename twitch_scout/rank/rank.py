"""Ranking: raw aggregates + noise guards -> an ordered candidate list.

The pipeline (handoff sections 6, 7):

    fetch window aggregates -> guards.partition (the anti-noise filter)
        -> for the eligible set: floor, spike, trend
        -> sort by viewers-per-channel (safe now that guards removed the noise)

Guards run first and are the product: sorting by ratio without them surfaces one-off
events (Ratatouille, Iron Man 2). Only guard-eligible categories are enriched and
ranked; the rejected set is returned too, with reasons, so the filter is inspectable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from twitch_scout.clock import Clock
from twitch_scout.rank.guards import CandidateStats, GuardConfig, GuardResult, partition
from twitch_scout.rank.queries import GameAggregate, fetch_aggregates, fetch_window_series
from twitch_scout.rank.stats import median, percentile
from twitch_scout.rank.trend import Trend, classify_trend, is_spike
from twitch_scout.store.db import Connection


@dataclass(frozen=True)
class RankConfig:
    eval_days: int = 14
    trend_window_days: int = 7
    trend_eps: float = 0.1
    spike_factor: float = 3.0
    floor_percentile: float = 10.0
    hide_falling: bool = False
    guards: GuardConfig = field(default_factory=GuardConfig)


@dataclass(frozen=True)
class Candidate:
    game_id: str
    game_name: str
    window_viewers: float
    window_channels: float
    ratio: float
    trend: Trend
    floor: int
    spiking: bool
    window_samples: int


@dataclass(frozen=True)
class RankResult:
    candidates: list[Candidate]  # eligible, best first
    rejected: list[GuardResult]  # filtered out, with reasons


def rank_candidates(conn: Connection, clock: Clock, config: RankConfig) -> RankResult:
    aggregates = fetch_aggregates(
        conn, clock, eval_days=config.eval_days, trend_window_days=config.trend_window_days
    )
    by_id = {agg.game_id: agg for agg in aggregates}

    stats = [_to_stats(agg) for agg in aggregates]
    eligible, rejected = partition(stats, config.guards)

    eligible_ids = [result.game_id for result in eligible]
    series = fetch_window_series(conn, clock, eligible_ids, eval_days=config.eval_days)

    candidates = [_to_candidate(by_id[gid], series.get(gid, []), config) for gid in eligible_ids]
    if config.hide_falling:
        candidates = [c for c in candidates if c.trend is not Trend.FALLING]

    # Ratio is meaningful now that guards removed the one-off noise; break ties on
    # raw window viewers so a bigger real audience wins.
    candidates.sort(key=lambda c: (c.ratio, c.window_viewers), reverse=True)
    return RankResult(candidates=candidates, rejected=rejected)


def _to_stats(agg: GameAggregate) -> CandidateStats:
    return CandidateStats(
        game_id=agg.game_id,
        game_name=agg.game_name,
        avg_viewers=agg.avg_viewers,
        avg_channels=agg.avg_channels,
        sample_count=agg.window_samples,
    )


def _to_candidate(agg: GameAggregate, viewers: list[int], config: RankConfig) -> Candidate:
    ratio = agg.avg_viewers / agg.avg_channels if agg.avg_channels else 0.0
    trend = classify_trend(agg.avg_viewers_recent, agg.avg_viewers, config.trend_eps)
    floor = int(percentile(viewers, config.floor_percentile)) if viewers else 0
    spiking = is_spike(viewers[-1], median(viewers), config.spike_factor) if viewers else False
    return Candidate(
        game_id=agg.game_id,
        game_name=agg.game_name,
        window_viewers=agg.avg_viewers,
        window_channels=agg.avg_channels,
        ratio=ratio,
        trend=trend,
        floor=floor,
        spiking=spiking,
        window_samples=agg.window_samples,
    )
