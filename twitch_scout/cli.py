"""Command-line entry point: ``scout <command>``.

Built on argparse (boring and static — coding standard 7). Commands wired so far:

  * ``scout init-db``    — create/migrate the database.
  * ``scout collect``    — run one sample. ``--tier auto`` (the cron path) lets the
    clock decide window vs baseline; ``window``/``baseline`` force it for backfill.
  * ``scout rank``       — rank candidate categories from the collected samples.
  * ``scout steam-sync`` — refresh the Steam library as a ranking candidate source.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from dataclasses import replace
from datetime import timedelta

from twitch_scout.clock import Clock, SystemClock
from twitch_scout.collect.collector import Collector, CollectResult
from twitch_scout.collect.tiers import Tier
from twitch_scout.config import Config, ConfigError
from twitch_scout.rank.guards import GuardConfig
from twitch_scout.rank.rank import Candidate, RankConfig, RankResult, rank_candidates
from twitch_scout.steam.client import SteamClient, SteamError
from twitch_scout.steam.sync import load_candidates, sync_owned_games
from twitch_scout.store.db import StoreError, connect, schema_version
from twitch_scout.twitch.client import HelixClient, TwitchError

logger = logging.getLogger(__name__)

_NAME_WIDTH = 32  # column width for the game name in the ranking table


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scout", description="Twitch category scout")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    init_db = sub.add_parser("init-db", help="create or migrate the database")
    init_db.set_defaults(func=cmd_init_db)

    collect = sub.add_parser("collect", help="run one sample")
    collect.add_argument(
        "--tier",
        choices=["auto", "window", "baseline"],
        default="auto",
        help="auto lets the clock decide (default); window/baseline force it",
    )
    collect.add_argument("--top", type=int, default=None, help="override SCOUT_TOP_N")
    collect.add_argument(
        "--force", action="store_true", help="re-sample even if the slot already exists"
    )
    collect.set_defaults(func=cmd_collect)

    rank = sub.add_parser("rank", help="rank candidate categories from collected samples")
    rank.add_argument("--days", type=int, default=14, help="evaluation window in days")
    rank.add_argument(
        "--floor", type=float, default=None, help="minimum window viewers (guard override)"
    )
    rank.add_argument(
        "--min-channels", type=float, default=None, help="minimum window channels (guard override)"
    )
    rank.add_argument(
        "--max-channels",
        type=float,
        default=None,
        help="maximum window channels: drop giant categories (Just Chatting, WoW) "
        "where a 2-6 viewer channel is buried (default: no upper limit)",
    )
    rank.add_argument(
        "--concentration-penalty",
        type=float,
        default=None,
        help="how hard to down-rank single-giant categories: 0 = raw viewers, "
        "1 = channel count only, 0.5 = geometric mean (default: 0.75)",
    )
    rank.add_argument("--limit", type=int, default=25, help="rows to show")
    rank.add_argument("--hide-falling", action="store_true", help="drop cooling categories")
    rank.add_argument(
        "--show-rejected", action="store_true", help="also list filtered-out categories and why"
    )
    rank.set_defaults(func=cmd_rank)

    steam_sync = sub.add_parser(
        "steam-sync", help="refresh the Steam library as a ranking candidate source"
    )
    steam_sync.set_defaults(func=cmd_steam_sync)

    return parser


def cmd_init_db(args: argparse.Namespace, config: Config) -> int:
    conn = connect(config.db, auth_token=config.turso_auth_token)
    try:
        version = schema_version(conn)
    finally:
        conn.close()
    print(f"initialized {config.db} at schema v{version}")
    return 0


def cmd_collect(args: argparse.Namespace, config: Config) -> int:
    creds = config.require_twitch()  # ConfigError if unset
    collector_config = config.collector
    if args.top is not None:
        try:
            collector_config = replace(collector_config, top_n=args.top)
        except ValueError as exc:
            # CollectorConfig validates the range; report it like any config error
            # rather than letting the ValueError surface as a traceback.
            raise ConfigError(str(exc)) from exc
    tier = None if args.tier == "auto" else Tier(args.tier)

    conn = connect(config.db, auth_token=config.turso_auth_token)
    try:
        with HelixClient.create(creds.client_id, creds.client_secret) as client:
            collector = Collector(
                client,
                conn,
                SystemClock(),
                config=collector_config,
                # Sampled only on a window slot; the store read is deferred until then.
                steam_candidates=lambda: load_candidates(conn),
            )
            result = collector.run(force=args.force, tier=tier)
    finally:
        conn.close()

    _print_result(result)
    return 0


def cmd_steam_sync(args: argparse.Namespace, config: Config) -> int:
    steam_creds = config.require_steam()  # ConfigError if unset
    twitch_creds = config.require_twitch()  # name resolution needs Helix Get Games

    conn = connect(config.db, auth_token=config.turso_auth_token)
    try:
        with (
            SteamClient.create(steam_creds.api_key) as steam,
            HelixClient.create(twitch_creds.client_id, twitch_creds.client_secret) as helix,
        ):
            result = sync_owned_games(
                steam, helix, conn, SystemClock(), steam_id=steam_creds.steam_id
            )
    finally:
        conn.close()

    print(
        f"synced {result.owned} owned games: {result.resolved} resolved to Twitch, "
        f"{result.unresolved} unresolved ({result.written} rows written)"
    )
    return 0


def cmd_rank(args: argparse.Namespace, config: Config) -> int:
    if args.limit < 1:
        # Validate before touching the DB; a zero/negative limit otherwise slices the
        # results silently ([:0] shows nothing, [:-3] drops the tail) with exit 0.
        raise ConfigError("--limit must be >= 1")
    rank_config = _rank_config_from_args(args)
    clock = SystemClock()
    _validate_eval_window(rank_config, clock)

    conn = connect(config.db, auth_token=config.turso_auth_token)
    try:
        result = rank_candidates(conn, clock, rank_config)
    finally:
        conn.close()

    _print_ranking(result, limit=args.limit, show_rejected=args.show_rejected)
    return 0


def _rank_config_from_args(args: argparse.Namespace) -> RankConfig:
    """Build the rank config, turning out-of-range values into a clean ConfigError.

    The dataclasses validate their thresholds in __post_init__; without this a bad
    CLI value (e.g. --concentration-penalty 2, or --max-channels below --min-channels)
    would surface as an uncaught ValueError traceback instead of "error: ...".
    """
    guard_defaults = GuardConfig()
    rank_defaults = RankConfig()
    try:
        guards = GuardConfig(
            min_viewer_floor=(
                args.floor if args.floor is not None else guard_defaults.min_viewer_floor
            ),
            min_avg_channels=(
                args.min_channels
                if args.min_channels is not None
                else guard_defaults.min_avg_channels
            ),
            max_avg_channels=(
                args.max_channels
                if args.max_channels is not None
                else guard_defaults.max_avg_channels
            ),
        )
        # The owned section keeps its relaxed floor/min-channels, but shares the
        # user's --max-channels ceiling: without it, giant owned categories (Valheim,
        # CS) top the section by score, which is the opposite of surfacing the
        # low-competition owned games a tiny channel can actually appear in.
        owned_guards = replace(rank_defaults.owned_guards, max_avg_channels=guards.max_avg_channels)
        return RankConfig(
            eval_days=args.days,
            hide_falling=args.hide_falling,
            concentration_penalty=(
                args.concentration_penalty
                if args.concentration_penalty is not None
                else rank_defaults.concentration_penalty
            ),
            guards=guards,
            owned_guards=owned_guards,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _validate_eval_window(config: RankConfig, clock: Clock) -> None:
    """Reject a day window that falls outside datetime's representable range."""
    try:
        clock.now() - timedelta(days=config.eval_days)
    except OverflowError as exc:
        raise ConfigError("eval_days is too large for the current date") from exc


def _print_ranking(result: RankResult, *, limit: int, show_rejected: bool) -> None:
    if not result.candidates:
        print("no eligible categories yet (need more samples, or loosen the guards)")
    else:
        header = (
            f"{'game':<{_NAME_WIDTH}} {'score':>7} {'viewers':>8} {'chan':>6} {'ratio':>7}  "
            f"{'trend':<8}{'floor':>6} spike"
        )
        print(header)
        print("-" * len(header))
        for c in result.candidates[:limit]:
            spike = "!" if c.spiking else ""
            name = (
                c.game_name
                if len(c.game_name) <= _NAME_WIDTH
                else c.game_name[: _NAME_WIDTH - 1] + "…"
            )
            print(
                f"{name:<{_NAME_WIDTH}} {c.score:>7.0f} {c.window_viewers:>8.0f} "
                f"{c.window_channels:>6.1f} {c.ratio:>7.1f}  {c.trend:<8}{c.floor:>6} {spike}"
            )
    _print_owned(result.owned)
    if show_rejected and result.rejected:
        print("\nfiltered out:")
        for r in result.rejected:
            print(f"  {r.game_name}: {'; '.join(r.failures)}")


def _print_owned(owned: list[Candidate]) -> None:
    if not owned:
        return
    print("\nfrom your Steam library (relaxed guards):")
    header = (
        f"{'game':<{_NAME_WIDTH}} {'score':>7} {'viewers':>8} {'chan':>6} {'ratio':>7} "
        f"{'played':>7}  {'trend':<8}{'floor':>6} spike"
    )
    print(header)
    print("-" * len(header))
    for c in owned:
        spike = "!" if c.spiking else ""
        name = (
            c.game_name if len(c.game_name) <= _NAME_WIDTH else c.game_name[: _NAME_WIDTH - 1] + "…"
        )
        played = f"{c.playtime_minutes // 60}h" if c.playtime_minutes is not None else "-"
        print(
            f"{name:<{_NAME_WIDTH}} {c.score:>7.0f} {c.window_viewers:>8.0f} "
            f"{c.window_channels:>6.1f} {c.ratio:>7.1f} {played:>7}  "
            f"{c.trend:<8}{c.floor:>6} {spike}"
        )


def _print_result(result: CollectResult) -> None:
    ts = result.slot.ts.isoformat()
    if result.skipped:
        print(f"slot {ts} ({result.slot.tier}) already sampled; skipped")
        return
    extra = f" (+{result.candidates_added} Steam)" if result.candidates_added else ""
    print(
        f"sampled {result.slot.tier} slot {ts}: "
        f"{result.games_written} written, {result.games_failed} failed "
        f"of {result.games_seen} games{extra}"
    )


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs each request URL at INFO. The Steam Web API carries its key in the
    # query string, so that would leak the key into stdout/CI logs. Keep httpx (and
    # its transport) at WARNING regardless of --verbose; app-level logs are unaffected.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _force_utf8_output() -> None:
    """Twitch category names are UTF-8 (Ōkami, 日本語, …) and the ranking table uses
    an ellipsis; the default Windows console codec (cp1252) raises on both. Reconfigure
    the streams to UTF-8 so output never crashes on a legitimate name. Best-effort: a
    non-reconfigurable stream (e.g. a captured pipe) is left as-is.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        config = Config.from_env()
        result: int = args.func(args, config)
        return result
    except (ConfigError, StoreError, TwitchError, SteamError) as exc:
        # Expected operational failures: report cleanly, no traceback.
        logger.debug("command failed", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
