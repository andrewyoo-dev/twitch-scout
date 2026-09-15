# Twitch Category Scout

A personal CLI that samples the Twitch Helix API at the hours you actually stream,
so you can pick the next game from real data instead of gut feel. See
[TWITCH_SCOUT_HANDOFF.md](TWITCH_SCOUT_HANDOFF.md) for the why, the metrics that
matter, and the two noise traps the ranking must guard against.

## Stack

- **Python** — collector, ranking, CLI
- **Twitch Helix API** — data source (App Access Token, read-only)
- **SQLite / Turso (libSQL)** — raw sample store (local file for dev, Turso in prod)

## Infra

- **GitHub Actions** runs the collector on a cron ([.github/workflows/collect.yml](.github/workflows/collect.yml)).
  Tier and DST logic live in Python, so the crons only fire often enough to cover
  the stream windows; the collector decides what to sample and skips slots it
  already has.
- **Turso** persists the database (GitHub runners are ephemeral).

## Commands

```bash
pip install -e ".[dev]"

scout init-db                    # create / migrate the database
scout collect --tier auto        # one sample; the clock picks window vs baseline
scout collect --tier baseline    # force a sample now (backfill / testing)
```

Configuration is read from the environment:

| Variable | Default | Notes |
| --- | --- | --- |
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | — | required for `collect` |
| `SCOUT_DB` | `scout.db` | local path, or the Turso libSQL URL in prod |
| `SCOUT_TOP_N` | `500` | how many top games to sample |
| `SCOUT_STREAMS_MAX_PAGES` | `3` | per-game stream page cap |

Register an app at <https://dev.twitch.tv/console/apps> to get the client id and
secret (client-credentials flow; the redirect URL is unused).

## Development

```bash
ruff check . && ruff format --check . && mypy twitch_scout && pytest -q
```

Every module injects its nondeterministic dependencies (clock, HTTP client, DB),
so the collector and ranking are tested against fixtures — no network, no waiting
two weeks for real samples. See the `coding-standards` skill.

## Status

Built: config, clock, sampling tiers, SQLite/store, Helix client, one-shot
collector, CLI (`init-db`, `collect`), noise guards.
Next: Turso backend in `store/db.py`, ranking (`scout rank`), Steam library join.
