"""Command-line entry point: ``scout <command>``.

Built on argparse (boring and static — coding standard 7). Commands wired so far:

  * ``scout init-db``  — create/migrate the database.
  * ``scout collect``  — run one sample. ``--tier auto`` (the cron path) lets the
    clock decide window vs baseline; ``window``/``baseline`` force it for backfill.

``rank`` and ``steam-sync`` are reserved for when those modules land.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

from twitch_scout.clock import SystemClock
from twitch_scout.collect.collector import Collector, CollectResult
from twitch_scout.collect.tiers import Tier
from twitch_scout.config import Config, ConfigError
from twitch_scout.store.db import StoreError, connect, schema_version
from twitch_scout.twitch.client import HelixClient, TwitchError

logger = logging.getLogger(__name__)


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
        collector_config = replace(collector_config, top_n=args.top)
    tier = None if args.tier == "auto" else Tier(args.tier)

    conn = connect(config.db, auth_token=config.turso_auth_token)
    try:
        with HelixClient.create(creds.client_id, creds.client_secret) as client:
            collector = Collector(client, conn, SystemClock(), config=collector_config)
            result = collector.run(force=args.force, tier=tier)
    finally:
        conn.close()

    _print_result(result)
    return 0


def _print_result(result: CollectResult) -> None:
    ts = result.slot.ts.isoformat()
    if result.skipped:
        print(f"slot {ts} ({result.slot.tier}) already sampled; skipped")
        return
    print(
        f"sampled {result.slot.tier} slot {ts}: "
        f"{result.games_written} written, {result.games_failed} failed "
        f"of {result.games_seen} games"
    )


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        config = Config.from_env()
        result: int = args.func(args, config)
        return result
    except (ConfigError, StoreError, TwitchError) as exc:
        # Expected operational failures: report cleanly, no traceback.
        logger.debug("command failed", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
