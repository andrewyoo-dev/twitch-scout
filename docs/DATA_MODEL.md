# Data model

What the database stores, what each value means, and how missing or partial data is represented.
The schema code is in [twitch_scout/store/db.py](../twitch_scout/store/db.py).
How the values are turned into recommendations is in [METHODOLOGY.md](METHODOLOGY.md).

## Glossary

| Term | Meaning |
| --- | --- |
| Slot | The canonical timestamp of one sample, in UTC. Window slots are floored to 15 minutes, baseline slots to the hour. |
| Batch | All rows written for one slot. |
| Tier | `window` (the streamer's stream hours, sampled deeply) or `baseline` (every hour, shallower). |
| Run | One execution of `scout collect`. Several runs can target the same slot; the collector skips a slot that already has a batch, in either tier. |
| Category | A Twitch game or category, keyed by Twitch `game_id`. |
| Truncated | The per-category stream listing hit its page cap, so streams past the cap were not read. |
| Position | Where a stream of a given size sits when a category's streams are sorted by viewer count, highest first. |

## Backends and migrations

`SCOUT_DB` selects the backend: a file path uses SQLite, a `libsql://` or `https://` URL uses Turso.
Migrations are append-only and tracked by a `schema_version` row in `meta`, because Turso rejects writing `PRAGMA user_version`.
Turso does not roll back DDL, so every migration statement must be safe to re-run.
`CREATE ... IF NOT EXISTS` covers tables and indexes.
`ALTER TABLE ... ADD COLUMN` has no `IF NOT EXISTS`, so a migration that adds columns must check `pragma_table_info` first.

## Current schema (v4)

### `snapshots`

One row per category per batch.
This is the raw sample store; averages, trends, and floors are computed from it, never stored.

| Column | Type | Meaning |
| --- | --- | --- |
| `ts` | TEXT | Slot timestamp, ISO 8601 UTC. |
| `tier` | TEXT | `window` or `baseline`. |
| `game_id` | TEXT | Twitch category id. |
| `game_name` | TEXT | Twitch category name at sample time. |
| `viewers` | INTEGER | Sum of `viewer_count` over the streams read, all languages. |
| `channels` | INTEGER | Number of streams read, all languages. |
| `truncated` | INTEGER | 1 if more stream pages existed past the cap. |

Primary key `(ts, game_id)`; indexes on `(game_id, ts)` and `(ts)`.

Known limits of this data:

- Only category totals are kept.
  Per-language counts, the viewer distribution, and the largest stream cannot be recovered for past rows.
- `channels` counts streams as returned.
  Helix pagination can return the same stream on two pages, so a stream may be counted twice.
- The stream listing reads at most 3 pages (300 streams) per category.
  When `truncated = 1`, the totals are partial observations: they miss streams past the cap and can also include duplicates, so they are neither a lower nor an upper bound.
- Stream items that fail validation (for example a missing `viewer_count`) are logged and dropped, and the drop is not recorded.
  A row with `truncated = 0` can therefore still be missing streams.
- A slot timestamp is shared across tiers.
  On stream days the 22:00 Pacific timestamp is both the last window hour's start and a baseline hour; the window batch is written first, so the baseline batch for that hour is skipped.
- The window tier samples the top 1,000 categories plus owned Steam games; the baseline samples the top 500.
  A category outside the top N in a slot has no row in that batch.

### `meta`

| Column | Type | Meaning |
| --- | --- | --- |
| `key` | TEXT | Primary key. Currently only `schema_version`. |
| `value` | TEXT | The value. |

### `steam_games`

The owned Steam library as a candidate source, refreshed by `scout steam-sync`.

| Column | Type | Meaning |
| --- | --- | --- |
| `appid` | INTEGER | Steam AppID, primary key. |
| `name` | TEXT | Steam's name for the game. |
| `playtime_minutes` | INTEGER | Total playtime. |
| `twitch_game_id` | TEXT | Resolved Twitch category, NULL if unresolved. |
| `twitch_game_name` | TEXT | Twitch's canonical name. |
| `source` | TEXT | `owned`. |
| `synced_at` | TEXT | ISO 8601 UTC of the last sync. |

Index on `twitch_game_id`.
The `watchlist` table created by v3 was dropped by v4 (see [DECISIONS.md](DECISIONS.md)).

## Planned: schema v5

Status: specified and reviewed, not implemented.
Rows written before v5 keep NULL in every new column, and are used only as all-language trend context.
English recommendations use v5 rows only.

### New columns on `snapshots`

Language-specific values are fixed columns because the first product scope is English only.
If more languages are supported later, this choice should be revisited rather than extended column by column.
Adding the columns to `snapshots`, instead of a second table, keeps writes at one row per category per batch.

| Column | Type | Meaning |
| --- | --- | --- |
| `en_channels` | INTEGER | English streams, after de-duplication. |
| `en_viewers` | INTEGER | Sum of viewers over English streams. |
| `en_top_viewers` | INTEGER | Viewers of the largest English stream; 0 when there are no English streams. |
| `all_top_viewers` | INTEGER | Viewers of the largest stream in any language. |
| `en_dist` | TEXT | English viewer distribution, JSON (format below). |
| `all_dist` | TEXT | All-language viewer distribution, JSON. |
| `all_dropped` | INTEGER | Stream items dropped by validation in the all-language listing. |
| `en_complete` | INTEGER | 1 if every English stream was read and none was dropped by validation (rules below). |
| `en_refetched` | INTEGER | 1 if the English values came from a separate `language=en` listing. |
| `run_id` | INTEGER | The `collect_runs` row that wrote this row. |

All-language values are complete only when `truncated = 0` and `all_dropped = 0`.
English completeness is recorded separately in `en_complete`.
The two are independent: the all-language listing can be incomplete while the English refetch is complete.
In that case the English values are usable and the all-language values are not a valid comparison.
Whenever `en_complete = 0`, the English columns are partial or NULL and must not be used.

### Viewer distribution format

Exact counts of streams at each viewer count from 0 to `max`, plus one count for everything above `max`:

```json
{"v": 1, "max": 30, "counts": [41, 12, 0, 4, 0, 1, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], "over": 2}
```

- `v` is the format version; readers reject unknown versions.
- `max` is the largest viewer count stored exactly.
- `counts[i]` is the number of streams with exactly `i` viewers, so `len(counts) == max + 1`.
- `over` is the number of streams with more than `max` viewers.
- Validation: every count is a non-negative integer, and `sum(counts) + over` equals the matching channel count.

`max` can grow in new rows without a migration.
It cannot be recovered for old rows: a stream counted in `over` could have 31 viewers or 3,000.
An input above a row's `max` is therefore not computable from that row.

### Stream reading rules

- Streams are de-duplicated by `user_id` across pages before anything is counted.
- Stream items that fail validation are dropped and counted.
  Reaching the last page does not make a listing complete if any item was dropped.
- If the all-language listing is complete, English values are derived from it, with `en_complete = 1` and `en_refetched = 0`.
- Otherwise (truncated, or any item dropped), the collector refetches the category with `language=en`.
- The English refetch has a per-category page cap and shares a per-run budget of requests and elapsed time.
  `en_complete = 1` only if the refetch reached its last page within the cap, before the budget ran out, and dropped no items.
  If the refetch fails, the English columns are NULL and a `collect_failures` row records the failure.
- Category items from Get Top Games that fail validation are dropped and counted in `collect_runs.games_dropped`.

### `collect_runs`

A run first checks whether its slot already has a batch.
A skipped run does no collection and writes no record.
Otherwise it inserts its record before any Twitch call and updates it when it ends.

A forced rerun (`--force`) replaces the slot's whole batch in one transaction, so every row of a batch carries the same `run_id`.
(The current collector upserts row by row, which can leave older rows from a previous run; v5 changes this.)

| Column | Type | Meaning |
| --- | --- | --- |
| `run_id` | INTEGER | Primary key. |
| `slot_ts` | TEXT | Target slot, ISO 8601 UTC. |
| `tier` | TEXT | `window` or `baseline`. |
| `started_at` | TEXT | Actual start time. |
| `finished_at` | TEXT | Actual end time; NULL while running or if the run died. |
| `status` | TEXT | `started`, `complete`, `partial`, or `failed`. |
| `top_n` | INTEGER | Categories requested from Get Top Games. |
| `games_listed` | INTEGER | Categories returned by Get Top Games plus extra candidates; NULL if listing failed. |
| `games_dropped` | INTEGER | Category items from Get Top Games dropped by validation. |
| `games_written` | INTEGER | Rows written to `snapshots`. |
| `games_failed` | INTEGER | Categories whose stream listing failed. |
| `en_refetches` | INTEGER | English refetches attempted. |
| `budget_exhausted` | INTEGER | 1 if the refetch budget ran out. |
| `cutoff_viewers` | INTEGER | Approximate viewer total of the last top-N category (see below). |
| `error` | TEXT | Short failure reason; never credentials or raw responses. |

Status meanings:

- `started`: the run began and never recorded an end, so it died or is still running.
- `complete`: the batch was written, the category list had no dropped items, every selected category has a row, and no English refetch failed or ran out of budget.
- `partial`: the batch was written, but some categories failed, some refetches failed, the budget ran out, or category items were dropped.
- `failed`: this run wrote no batch (the category listing or the batch write failed).

`cutoff_viewers` is approximate.
Get Top Games returns no viewer counts, so the cutoff is computed from stream listings read later in the run, not at the same instant.

### `collect_failures`

| Column | Type | Meaning |
| --- | --- | --- |
| `run_id` | INTEGER | The run. |
| `game_id` | TEXT | The category. |
| `stage` | TEXT | `streams` or `en_refetch`. |
| `reason` | TEXT | Short reason. |

Primary key `(run_id, game_id, stage)`.

### Observation status of a category in a slot

Missing data is never treated as zero viewers.
The status is decided in two steps, each evaluated top to bottom with the first match winning, so every case has exactly one status.

**Step A: the slot.**

| Slot state | Condition |
| --- | --- |
| Collected | `snapshots` has rows for the slot. The slot's run is the `run_id` on those rows. |
| Collection failed | No rows, and the slot's most recent run record is `failed`. |
| Run did not finish | No rows, and the most recent run record is `started`. |
| Not collected | Anything else: no rows and no run record (whether it never ran is unknown), or run records that do not explain the missing rows (for example rows later removed by retention). |

The last three are shown to users together as "not collected", with a count for each cause.

**Step B: the category, in a collected slot.**

| Category status | Condition |
| --- | --- |
| Observed | A row exists for the category. |
| Lookup failed | No row, and a `collect_failures` row with stage `streams` exists for the slot's run. |
| Not in the sampled set | No row, and the slot's run listed categories (`games_listed` is not NULL) with `games_dropped = 0`. Shown to users as "not included in this sample". |
| Unknown | No row, and none of the above can be established (for example a batch written before v5, which has no run record). |

**Data validity of an observed row** is separate from its status:

- English values are usable only if `en_complete = 1`.
- All-language comparison values are usable only if `truncated = 0` and `all_dropped = 0`.

A failed English refetch therefore leaves the category "observed" with unusable English values.
Its `collect_failures` row with stage `en_refetch` is diagnostic, not a second status.

## Size and write volume

These are estimates until the collection experiment measures them.

- Top 1,000 categories every 15 minutes for 30 days is 2.88 million `snapshots` rows a month.
  About 3.3 million allows for owned-game candidates and re-runs.
- Bytes per row including indexes are unmeasured; 200 bytes is a working assumption.
  At that size 24-hour deep collection would reach Turso's 5 GB free storage within about half a year.
- Turso's free tier allows 10 million rows written a month; check the current pricing page before relying on it.
- Reads matter as much as writes.
  Fourteen days of 15-minute rows for 1,000 categories is about 1.3 million rows, too many to scan per web request.
  The web app reads a precomputed result table, never raw `snapshots`.

## Retention (to be decided)

Statistics come before storage savings.
A simple hourly average of 15-minute rows cannot reproduce the medians and 10th percentiles the recommendations use.
The working rule:

- Keep raw rows longer than the recommendation window, so recommendations are always computed from raw data.
- Long-term rollups exist only for trend context, and store per-slot derived statistics (for example a daily median and 10th percentile), not averages of averages.

The retention period is decided after the collection experiment.
