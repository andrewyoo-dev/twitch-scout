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

import math
from dataclasses import dataclass, field

from twitch_scout.clock import Clock
from twitch_scout.rank.guards import CandidateStats, GuardConfig, GuardResult, partition
from twitch_scout.rank.queries import GameAggregate, fetch_aggregates, fetch_window_series
from twitch_scout.rank.stats import median, percentile
from twitch_scout.rank.trend import Trend, classify_trend, is_spike
from twitch_scout.store.db import Connection
from twitch_scout.store.steam import OwnedGame, fetch_owned


def _relaxed_owned_guards() -> GuardConfig:
    # Owned games are the point of the section, so the guards only exclude the truly
    # dead (near-zero demand): a much lower viewer floor, a single channel, and a
    # single sample. A resolved owned game with no live streams reads as 0 viewers
    # and is filtered by the floor, which is exactly "exclude the dead game".
    return GuardConfig(min_viewer_floor=10.0, min_avg_channels=1.0, min_sample_count=1)


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
    # Owned games get their own section with relaxed guards, so a game the streamer
    # owns but that ranks low still surfaces (the whole reason for the Steam source).
    owned_guards: GuardConfig = field(default_factory=_relaxed_owned_guards)

    def __post_init__(self) -> None:
        # Validate at the boundary: a nonsensical config is a bug, not something to
        # debug later as an empty list or a traceback from deep in the query layer.
        if not 0.0 <= self.concentration_penalty <= 1.0:
            raise ValueError("concentration_penalty must be between 0 and 1")
        if self.eval_days <= 0:
            raise ValueError("eval_days must be > 0")
        if self.trend_window_days <= 0:
            raise ValueError("trend_window_days must be > 0")


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
    playtime_minutes: int | None = None  # set for owned candidates only


@dataclass(frozen=True)
class RankResult:
    candidates: list[Candidate]  # eligible, best first
    rejected: list[GuardResult]  # filtered out, with reasons
    owned: list[Candidate]  # owned games, relaxed guards, best first


def rank_candidates(conn: Connection, clock: Clock, config: RankConfig) -> RankResult:
    aggregates = fetch_aggregates(
        conn, clock, eval_days=config.eval_days, trend_window_days=config.trend_window_days
    )
    by_id = {agg.game_id: agg for agg in aggregates}
    owned_by_id = {game.twitch_game_id: game for game in fetch_owned(conn)}

    stats = [_to_stats(agg) for agg in aggregates]
    eligible, rejected = partition(stats, config.guards)

    # The owned section runs relaxed guards over the owned-only subset, so an owned
    # game that the strict guards drop can still surface here.
    owned_stats = [s for s in stats if s.game_id in owned_by_id]
    owned_eligible, _ = partition(owned_stats, config.owned_guards)

    eligible_ids = [result.game_id for result in eligible]
    owned_ids = [result.game_id for result in owned_eligible]
    # One series fetch for the union of both sets.
    series = fetch_window_series(
        conn, clock, list(dict.fromkeys(eligible_ids + owned_ids)), eval_days=config.eval_days
    )

    def build(ids: list[str]) -> list[Candidate]:
        cands = [_to_candidate(by_id[gid], series.get(gid, []), config, owned_by_id) for gid in ids]
        if config.hide_falling:
            cands = [c for c in cands if c.trend is not Trend.FALLING]
        # Sort by the discoverability score, not raw viewers-per-channel: the latter
        # floats single-giant categories (Last Pirates: 852 v/ch, one streamer) that a
        # 2-6 viewer channel is buried under. Break ties on raw window viewers.
        cands.sort(key=lambda c: (c.score, c.window_viewers), reverse=True)
        return cands

    return RankResult(candidates=build(eligible_ids), rejected=rejected, owned=build(owned_ids))


def _to_stats(agg: GameAggregate) -> CandidateStats:
    return CandidateStats(
        game_id=agg.game_id,
        game_name=agg.game_name,
        avg_viewers=agg.avg_viewers,
        avg_channels=agg.avg_channels,
        sample_count=agg.window_samples,
    )


def _to_candidate(
    agg: GameAggregate,
    viewers: list[int],
    config: RankConfig,
    owned_by_id: dict[str, OwnedGame],
) -> Candidate:
    ratio = agg.avg_viewers / agg.avg_channels if agg.avg_channels else 0.0
    score = _discoverability_score(agg.avg_viewers, agg.avg_channels, config.concentration_penalty)
    trend = classify_trend(agg.avg_viewers_recent, agg.avg_viewers, config.trend_eps)
    floor = int(percentile(viewers, config.floor_percentile)) if viewers else 0
    spiking = is_spike(viewers[-1], median(viewers), config.spike_factor) if viewers else False
    owned = owned_by_id.get(agg.game_id)
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
        playtime_minutes=owned.playtime_minutes if owned else None,
    )


def _discoverability_score(viewers: float, channels: float, penalty: float) -> float:
    """viewers^(1-penalty) * channels^penalty (higher = better for a tiny channel).

    Computed from the totals directly (no viewers/channels division) so a zero on
    either side yields 0 rather than raising; guards normally keep both positive.
    """
    if viewers <= 0.0 or channels <= 0.0:
        return 0.0
    # math.pow keeps the result statically float (the ** operator infers Any here).
    return math.pow(viewers, 1.0 - penalty) * math.pow(channels, penalty)
