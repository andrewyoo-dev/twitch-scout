# Twitch Category Scout

A personal CLI that samples the Twitch Helix API at the hours you actually stream,
so you can pick the next game from real data instead of gut feel. See
[docs/TWITCH_SCOUT_HANDOFF.md](docs/TWITCH_SCOUT_HANDOFF.md) for the why, the metrics
that matter, and the two noise traps the ranking must guard against; key design
decisions are in [docs/DECISIONS.md](docs/DECISIONS.md).

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
scout steam-sync                 # refresh the owned Steam library as candidates
scout rank --max-channels 50     # rank the workable band + your owned games
```

`steam-sync` resolves your owned games to Twitch categories and stores them; the
window-tier collector then samples those categories even when they never enter the
top-N, and `rank` lists them in a separate "from your Steam library" section under
relaxed guards (so a game you own but that ranks low still surfaces).

Configuration is read from the environment:

| Variable | Default | Notes |
| --- | --- | --- |
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | — | required for `collect`, `steam-sync` |
| `SCOUT_DB` | `scout.db` | local path, or the Turso libSQL URL in prod |
| `SCOUT_TOP_N` | `500` | how many top games to sample |
| `SCOUT_STREAMS_MAX_PAGES` | `3` | per-game stream page cap |
| `STEAM_API_KEY` | — | required for `steam-sync` (free, from Steam) |
| `STEAM_ID` | — | steamid64 or vanity name, for `steam-sync` |

Register an app at <https://dev.twitch.tv/console/apps> to get the client id and
secret (client-credentials flow; the redirect URL is unused). Get a Steam Web API
key at <https://steamcommunity.com/dev/apikey>; your profile's game details must be
public for `steam-sync` to read the library.

## Development

For Claude Code implementation and independent Astra review, follow [docs/REVIEW_WORKFLOW.md](docs/REVIEW_WORKFLOW.md).
Active review notes live in a local, gitignored `reviews/` directory; confirmed fixes land in commits and tests, durable decisions in [docs/DECISIONS.md](docs/DECISIONS.md).

```bash
ruff check . && ruff format --check . && mypy twitch_scout && pytest -q
```

Every module injects its nondeterministic dependencies (clock, HTTP client, DB),
so the collector and ranking are tested against fixtures — no network, no waiting
two weeks for real samples. See the `coding-standards` skill.

## Status

Built: config, clock, sampling tiers, SQLite + Turso store, Helix client, one-shot
collector, ranking (`scout rank`) with discoverability scoring and noise guards, and
the Steam owned-library candidate source (`scout steam-sync` + window-tier sampling +
the owned rank section). CLI: `init-db`, `collect`, `rank`, `steam-sync`.
Next: manual watchlist and wishlist candidate sources; separate "reliable trend" from
"min samples" in rank.
