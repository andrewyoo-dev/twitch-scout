# Handoff: verify the CLI input validation

You are the reviewer for a scoped change in `twitch-scout` (a personal Python CLI: Twitch Helix sampling to pick the next game to stream).
The CLI input validation is **already implemented**.
Your job is to independently verify it is correct and complete, add or run tests, and report gaps.
Do not re-implement unless you find a real defect.

## What was implemented

Across commits `a3ba123` (rank config values), `100d86a` (a related store leak fix), and `50df75b` (the remaining CLI validation), every `scout` argument with a valid range now rejects a bad value with a clean `error: <message>` on stderr and exit code 1, never a traceback, and never a silently disabled guard.

Covered cases:

- `rank --concentration-penalty` outside `[0, 1]` (also rejects `nan`/`inf`).
- `rank --max-channels` below `--min-channels`.
- `rank --floor` negative, `rank --min-channels` negative.
- `rank --days` <= 0 (`RankConfig.eval_days`).
- `collect --top` <= 0 (was an uncaught `ValueError` from `replace()`; now converted to `ConfigError`).
- `rank --limit` <= 0 (was accepted with exit 0; now rejected before the DB is opened).
- `rank --floor` / `--min-channels` / `--max-channels` set to `nan` or `inf` (NaN escaped every ordering check, so the guard silently did nothing; `GuardConfig.__post_init__` now rejects non-finite thresholds explicitly).

Design: value errors are surfaced as `ConfigError`, which `main()` reports as `error: ...` with exit code 1 (one error channel, one exit code), matching `Config.from_env`.
Range/finiteness checks live in the dataclass `__post_init__` (`GuardConfig`, `RankConfig`, `CollectorConfig`) so both the CLI and any programmatic caller are protected; the CLI layer only converts `ValueError` to `ConfigError`.

## What to verify

Run each of these and confirm a clean `error:` message and exit code 1 (no traceback):

```
SCOUT_DB=/tmp/t.db TWITCH_CLIENT_ID=x TWITCH_CLIENT_SECRET=x python -m twitch_scout.cli collect --top -5
SCOUT_DB=/tmp/t.db TWITCH_CLIENT_ID=x TWITCH_CLIENT_SECRET=x python -m twitch_scout.cli collect --top 0
SCOUT_DB=/tmp/t.db python -m twitch_scout.cli rank --limit 0
SCOUT_DB=/tmp/t.db python -m twitch_scout.cli rank --limit -3
SCOUT_DB=/tmp/t.db python -m twitch_scout.cli rank --floor nan
SCOUT_DB=/tmp/t.db python -m twitch_scout.cli rank --min-channels nan
SCOUT_DB=/tmp/t.db python -m twitch_scout.cli rank --max-channels nan
SCOUT_DB=/tmp/t.db python -m twitch_scout.cli rank --floor inf
SCOUT_DB=/tmp/t.db python -m twitch_scout.cli rank --concentration-penalty 2
SCOUT_DB=/tmp/t.db python -m twitch_scout.cli rank --days 0
```

Check the exit code with `echo $?` (a trailing `| tail` in a pipe reports the pipe's last command, not python).

## Edge cases worth probing (report if any misbehaves)

- Other float flags with `nan`/`inf` that might still slip through.
- `--min-channels` and `--max-channels` equal (should be allowed; only `max < min` is rejected).
- `--top` and `--limit` at their minimum valid values (`1`) should work.
- Very large but finite values should be accepted (they are valid, just strict).
- Confirm the `collect --top` path is clean only when credentials are present; with no credentials the creds `ConfigError` fires first, which is expected and also clean.

## Tests to run and extend

```
ruff format --check twitch_scout tests
ruff check .
mypy twitch_scout
python -m pytest -q
```

Expected: full suite green (currently 171 passed, 1 skipped), zero lint/type warnings.
Relevant tests already added:

- `tests/test_cli.py`: `test_collect_bad_top_exits_cleanly`, `test_rank_bad_limit_exits_cleanly`, `test_rank_nan_float_exits_cleanly`, plus the earlier `test_rank_bad_concentration_penalty_exits_cleanly` and `test_rank_max_channels_below_min_exits_cleanly`.
- `tests/test_guards.py`: the `test_invalid_config_rejected` table now includes `nan`/`inf` rows.
- `tests/test_rank.py`: `test_rank_config_rejects_out_of_range_penalty`, `test_rank_config_rejects_nonpositive_days`.

If you find a gap, add a failing test first, then the fix, and keep the suite green.

## Constraints (repo rules)

- Follow the `coding-standards` skill (validate at boundaries, small functions, fail loud).
- No em-dashes anywhere. Use commas or parentheses.
- No `Co-Authored-By` trailer on commits.
- Conventional Commits, imperative, explain the why.

## Pointers

- CLI: `twitch_scout/cli.py` (`main`, `cmd_collect`, `cmd_rank`, `_rank_config_from_args`, `_print_ranking`).
- Config + error type: `twitch_scout/config.py` (`ConfigError`, `Config.from_env`).
- Range/finiteness validation: `twitch_scout/rank/guards.py` (`GuardConfig.__post_init__`), `twitch_scout/rank/rank.py` (`RankConfig.__post_init__`), `twitch_scout/collect/collector.py` (`CollectorConfig.__post_init__`).
