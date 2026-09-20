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
    # Discoverability sort: score = viewers^(1-c) * channels^c. c is how hard to
    # penalise concentration (a category carried by one big streamer). 0 = rank on
    # raw viewers (favours giants, the old ratio's failure mode); 1 = rank on
    # channel count alone (pure spread); 0.5 = geometric mean sqrt(viewers*channels).
    # Default 0.75: on live data 0.5 still floated high-viewer single-giant categories
    # (Virtual Casino, Last Pirates); 0.75 surfaces the channel-rich, small-stream band
    # a 2-6 viewer channel can actually appear in. See the memory note dated 2026-09-20.
    concentration_penalty: float = 0.75
    guards: GuardConfig = field(default_factory=GuardConfig)

    def __post_init__(self) -> None:
        # Validate at the boundary: a nonsensical exponent is a bug, not something
        # to debug later as a mysteriously reordered list.
        if not 0.0 <= self.concentration_penalty <= 1.0:
            raise ValueError("concentration_penalty must be between 0 and 1")


@dataclass(frozen=True)
class Candidate:
    game_id: str
    game_name: str
    window_viewers: float
    window_channels: float
    ratio: float
    score: float  # discoverability sort key: viewers^(1-c) * channels^c
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

    # Sort by the discoverability score, not raw viewers-per-channel: the latter
    # floats single-giant categories (Last Pirates: 852 v/ch, one streamer) that a
    # 2-6 viewer channel is buried under. Break ties on raw window viewers so a
    # bigger real audience wins.
    candidates.sort(key=lambda c: (c.score, c.window_viewers), reverse=True)
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
    score = _discoverability_score(
        agg.avg_viewers, agg.avg_channels, config.concentration_penalty
    )
    trend = classify_trend(agg.avg_viewers_recent, agg.avg_viewers, config.trend_eps)
    floor = int(percentile(viewers, config.floor_percentile)) if viewers else 0
    spiking = is_spike(viewers[-1], median(viewers), config.spike_factor) if viewers else False
    return Candidate(
        game_id=agg.game_id,
        game_name=agg.game_name,
        window_viewers=agg.avg_viewers,
        window_channels=agg.avg_channels,
        ratio=ratio,
        score=score,
        trend=trend,
        floor=floor,
        spiking=spiking,
        window_samples=agg.window_samples,
    )


def _discoverability_score(viewers: float, channels: float, penalty: float) -> float:
    """viewers^(1-penalty) * channels^penalty (higher = better for a tiny channel).

    Computed from the totals directly (no viewers/channels division) so a zero on
    either side yields 0 rather than raising; guards normally keep both positive.
    """
    if viewers <= 0.0 or channels <= 0.0:
        return 0.0
    return viewers ** (1.0 - penalty) * channels**penalty
