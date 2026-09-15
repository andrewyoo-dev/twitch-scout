"""Noise guards — the anti-garbage filter that decides which categories may rank.

This is the product. Ranking by viewers-per-channel without these guards surfaces
one-off events: a category with a single big streamer live for an hour yields an
absurd ratio and floats to the top (real offenders during manual analysis:
Claw Machine Sim, Ratatouille, Iron Man 2). See TWITCH_SCOUT_HANDOFF.md section 7.

Everything here is a pure function over already-computed stats, so it can be tested
against fixtures without a database or the Twitch API. The stats themselves come
from the query layer; this module only decides eligibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CandidateStats:
    """Aggregated stats for one category over an evaluation window.

    Averages are taken over the samples in which the category was present with at
    least one live channel; ``sample_count`` is how many samples that was. That
    count is what distinguishes a persistent category from a one-off spike, so it
    is the primary defence against Trap 1 alongside ``avg_channels``.

    ``distinct_streamers`` is only known when per-stream collection is enabled
    (see the handoff's open questions); leave it ``None`` otherwise. A guard that
    needs it will refuse to run silently rather than pretend the data exists.
    """

    game_id: str
    game_name: str
    avg_viewers: float
    avg_channels: float
    sample_count: int
    distinct_streamers: int | None = None


@dataclass(frozen=True)
class GuardConfig:
    """Tunable thresholds. Defaults encode the handoff's section 6/7 guidance.

    These are deliberately tunable rather than constants: the right floor shifts
    as the channel grows (handoff section 7, Trap 2).
    """

    # Trap 2: categories under ~100 viewers do not produce meaningful inflow.
    min_viewer_floor: float = 100.0
    # Trap 1 / section 6 workable band lower bound: need real concurrent supply.
    min_avg_channels: float = 3.0
    # Trap 1: reject one-off events that appear in only a handful of samples.
    # Meaningful only once some history has accumulated; tune upward over time.
    min_sample_count: int = 3
    # Section 6 workable band upper bound. None = no upper limit (not a noise
    # guard; a crowded category is real, just hard for a 2-6 viewer channel).
    max_avg_channels: float | None = None
    # Only enforceable with per-stream data. None = off. If set, candidates must
    # carry distinct_streamers or evaluate() raises rather than silently passing.
    min_distinct_streamers: int | None = None

    def __post_init__(self) -> None:
        # Validate the config at its boundary: nonsensical thresholds are a bug,
        # not something to discover later as mysteriously-empty candidate lists.
        if self.min_viewer_floor < 0:
            raise ValueError("min_viewer_floor must be >= 0")
        if self.min_avg_channels < 0:
            raise ValueError("min_avg_channels must be >= 0")
        if self.min_sample_count < 1:
            raise ValueError("min_sample_count must be >= 1")
        if self.max_avg_channels is not None and self.max_avg_channels < self.min_avg_channels:
            raise ValueError("max_avg_channels must be >= min_avg_channels")
        if self.min_distinct_streamers is not None and self.min_distinct_streamers < 1:
            raise ValueError("min_distinct_streamers must be >= 1 when set")


@dataclass(frozen=True)
class GuardResult:
    """Outcome for one candidate. ``failures`` is empty iff the candidate passed.

    Reasons are kept human-readable and explicit so a filtered candidate list is
    inspectable — being able to see *why* something was dropped is the whole point.
    """

    game_id: str
    game_name: str
    passed: bool
    failures: tuple[str, ...] = field(default_factory=tuple)


def evaluate(stats: CandidateStats, config: GuardConfig) -> GuardResult:
    """Apply every guard to one candidate and collect all failure reasons.

    All checks run (rather than short-circuiting) so the result explains every
    reason a candidate was rejected, not just the first.
    """
    failures: list[str] = []

    if stats.avg_viewers < config.min_viewer_floor:
        failures.append(
            f"avg viewers {stats.avg_viewers:.1f} below floor {config.min_viewer_floor:.0f}"
        )

    if stats.avg_channels < config.min_avg_channels:
        failures.append(
            f"avg channels {stats.avg_channels:.1f} below minimum {config.min_avg_channels:.1f}"
        )

    if config.max_avg_channels is not None and stats.avg_channels > config.max_avg_channels:
        failures.append(
            f"avg channels {stats.avg_channels:.1f} above band {config.max_avg_channels:.1f}"
        )

    if stats.sample_count < config.min_sample_count:
        failures.append(
            f"only {stats.sample_count} samples, need {config.min_sample_count} "
            "(likely a one-off, not a persistent category)"
        )

    if config.min_distinct_streamers is not None:
        if stats.distinct_streamers is None:
            # Do not swallow a misconfiguration: the caller asked for a guard the
            # data cannot support. Fail loud rather than silently skip it.
            raise ValueError(
                f"min_distinct_streamers is set but {stats.game_name!r} has no "
                "distinct_streamers (per-stream collection is off)"
            )
        if stats.distinct_streamers < config.min_distinct_streamers:
            failures.append(
                f"only {stats.distinct_streamers} distinct streamers, "
                f"need {config.min_distinct_streamers}"
            )

    return GuardResult(
        game_id=stats.game_id,
        game_name=stats.game_name,
        passed=not failures,
        failures=tuple(failures),
    )


def partition(
    candidates: list[CandidateStats], config: GuardConfig
) -> tuple[list[GuardResult], list[GuardResult]]:
    """Split candidates into (eligible, rejected). Order within each is preserved.

    The rejected list is returned rather than discarded so the caller can show
    what was filtered and why.
    """
    eligible: list[GuardResult] = []
    rejected: list[GuardResult] = []
    for stats in candidates:
        result = evaluate(stats, config)
        (eligible if result.passed else rejected).append(result)
    return eligible, rejected
