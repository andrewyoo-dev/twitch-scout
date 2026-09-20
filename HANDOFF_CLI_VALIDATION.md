# Handoff: finish CLI input validation

You are picking up a scoped task in `twitch-scout` (a personal Python CLI: Twitch Helix sampling to pick the next game to stream).
Scope is **CLI argument validation only**.
Do not touch ranking math, the collector, or the store.

## Goal

Every `scout` subcommand should reject a bad argument value with a clean `error: <message>` on stderr and exit code 1, never a Python traceback.
Numeric arguments that have a valid range must enforce it.

## Already done (do NOT redo)

Commit `a3ba123` already hardened the `rank` config values.
`twitch_scout/cli.py:_rank_config_from_args` builds `GuardConfig`/`RankConfig` and converts any `ValueError` from their `__post_init__` into `ConfigError`, which `main()` already reports cleanly.
These are verified working and covered by tests:

- `--concentration-penalty` outside `[0, 1]`
- `--max-channels` below `--min-channels`
- `--floor` negative (GuardConfig validates `min_viewer_floor >= 0`)
- `--min-channels` negative
- `--days` <= 0 (validated in `RankConfig.__post_init__` as `eval_days > 0`)

Leave all of that as is.

## Gaps to fix

### 1. `scout collect --top <bad>` tracebacks

`cmd_collect` in `twitch_scout/cli.py:98` calls `replace(collector_config, top_n=args.top)` at line 102.
`CollectorConfig.__post_init__` (`twitch_scout/collect/collector.py:59`) raises `ValueError("top_n must be > 0")` for a non-positive value.
That `ValueError` is not caught, so `scout collect --top -5` (or `--top 0`, with credentials set) prints a full traceback and exits 1.

Reproduce (credentials can be dummy; the traceback happens before any network call):

```
SCOUT_DB=/tmp/t.db TWITCH_CLIENT_ID=x TWITCH_CLIENT_SECRET=x python -m twitch_scout.cli collect --top -5
```

Fix: wrap the `replace(...)` (the only place `--top` is applied) so a `ValueError` becomes a `ConfigError`, consistent with how `rank` does it.
`require_twitch()` is called before `replace()`, so with no credentials the creds error fires first; that ordering is fine, but the `--top` path must still be clean when credentials are present.

### 2. `scout rank --limit <=0` is silently wrong

`--limit 0` currently prints "no eligible categories..." (misleading), and a negative limit slices oddly (`candidates[:-3]`).
`--limit` is applied in `_print_ranking` via `result.candidates[:limit]`.
Add validation that `--limit >= 1` and report it cleanly (exit 1), or reject at the argparse layer (exit 2).
Pick one approach and apply it consistently (see the decision below).

### 3. Audit the remaining numeric args

Confirm each of these is either already clean or gets the same treatment, and add a test for each real gap you find:

- `collect`: `--top` (gap 1 above).
- `rank`: `--limit` (gap 2 above), plus re-confirm the `a3ba123` set still reports cleanly (regression guard).
- Any int/float arg you add validation for should have a boundary test.

`SCOUT_TOP_N` / `SCOUT_STREAMS_MAX_PAGES` from the environment are already validated in `Config.from_env` (`twitch_scout/config.py:53`), so env-side is done; this task is about the CLI flag overrides.

## Design decision to make (state it in the PR/commit)

Two consistent styles are possible; the codebase currently leans on the first for `rank`:

- **A (recommended): convert to `ConfigError`.** Build the config, catch `ValueError`, re-raise as `ConfigError`; `main()` prints `error: ...` and exits 1. Matches `_rank_config_from_args` and `Config.from_env`, keeps one error channel and exit code. Preferred for range checks that live in the dataclasses.
- **B: argparse `type=` validators.** Custom `type` callables that raise `argparse.ArgumentTypeError`, giving a usage message and exit code 2. Cleaner for pure CLI-shape checks, but splits validation across two layers and two exit codes.

Recommendation: use **A** so all value errors surface identically.
For `--limit`, which has no dataclass home, either add the check in `cmd_rank`/`_print_ranking` and raise `ConfigError`, or use an argparse validator; if you choose A everywhere, prefer raising `ConfigError`.
Do not split the same command across both styles.

## Constraints

- Follow the `coding-standards` skill (validate at boundaries, small functions, fail loud, no silent skips).
- No em-dashes anywhere (repo rule). Use commas or parentheses.
- Do not add a `Co-Authored-By` trailer to commits (repo rule).
- Commit messages: Conventional Commits, imperative, explain the why.
- Prefer the existing patterns already in `cli.py` over introducing new ones.

## Quality gates (all must pass before you commit)

```
ruff format twitch_scout tests
ruff check .
mypy twitch_scout
python -m pytest -q
```

Target: the full suite green (currently 160 passed, 1 skipped), zero lint/type warnings.

## Manual verification to include in your summary

Show the actual output of each fixed case, for example:

```
python -m twitch_scout.cli collect --top -5      # -> error: top_n must be > 0 ; exit 1
python -m twitch_scout.cli rank --limit 0        # -> clean error ; exit 1
```

## Pointers

- CLI: `twitch_scout/cli.py` (`main`, `cmd_collect`, `cmd_rank`, `_rank_config_from_args`, `_print_ranking`).
- Config + error type: `twitch_scout/config.py` (`ConfigError`, `Config.from_env`, `_int_env`).
- Collector config validation: `twitch_scout/collect/collector.py:51` (`CollectorConfig`).
- Tests: `tests/test_cli.py` (parser + clean-exit tests already exist as a model to copy).
