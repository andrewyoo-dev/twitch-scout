# Operations

How to keep collection running, check that data is arriving, and recover from the failures seen so far.
Start here after any break from the project.
Never paste credential values into this file, issues, or review notes.

## What runs where

| Piece | Where | Cadence |
| --- | --- | --- |
| Trigger | cron-job.org job calling the GitHub `workflow_dispatch` API for `collect.yml` | every 15 minutes, all day |
| Collector | GitHub Actions, [.github/workflows/collect.yml](../.github/workflows/collect.yml): `scout init-db` then `scout collect --tier auto` | per trigger, 10-minute job timeout |
| Database | Turso (libSQL) | always on |
| Steam library sync | `scout steam-sync`, run locally by hand | when the library changes |

The collector picks the tier from the Pacific-time clock.
Window slots (Tuesday, Thursday, Saturday, 18:30 to 22:30 PT) sample the top 1,000 categories every 15 minutes.
All other hours sample the top 500 once per hour.
A run whose slot already has a batch exits without calling Twitch.

## Secrets and accounts

| Secret | Stored in | Used by |
| --- | --- | --- |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | local `.env`, GitHub repo secrets | collect, steam-sync |
| `SCOUT_DB` (Turso URL), `TURSO_AUTH_TOKEN` | local `.env`, GitHub repo secrets | everything that touches the database |
| `STEAM_API_KEY`, `STEAM_ID` | local `.env` only | steam-sync |
| GitHub fine-grained PAT (Actions read/write, this repo only) | cron-job.org job | triggering the workflow |

The Twitch app is registered as `aspen-scout`.
Twitch rejects app names containing "twitch", and the account needs two-factor authentication.

## Calendar

| Date | Item | Action |
| --- | --- | --- |
| 2026-10-19 | GitHub's `ubuntu-latest` label starts moving to Ubuntu 26 | Check the first collect runs after the switch. |
| 2026-12-19 | The cron-job.org PAT expires | Create a new PAT with the same scope before this date and update the cron-job.org job. When it expires, collection stops silently. |

## Routine check

Run this weekly, and always after a break.

1. Recent runs, all should be `success`:

   ```bash
   gh run list --workflow collect.yml --limit 100
   ```

2. Batches per Pacific day, compared with what the schedule expects.
   Expected counts come from the collector's own slot logic, so they stay right across daylight-saving changes (23 or 25 baseline hours on transition days).
   A normal stream day has 16 window and 20 baseline batches.
   The 19:00, 20:00, and 21:00 hours have no baseline fire at all, and the 22:00 timestamp is taken by the window batch, because the collector skips a timestamp that already has a batch in any tier.
   Other days have 24 baseline batches.
   Rows per window batch should be about 1,150 (top 1,000 plus owned games), and about 500 per baseline batch.

   Load `.env` (below), save this as a file outside the repository, and run it with the project installed (`pip install -e .`):

   ```python
   import os
   from collections import Counter
   from datetime import UTC, date, datetime, timedelta

   from twitch_scout.collect.tiers import PACIFIC, resolve_slot
   from twitch_scout.store.db import connect, from_iso, to_iso

   DAYS = 14  # complete Pacific days to check, ending yesterday


   def day_bounds(day: date) -> tuple[datetime, datetime]:
       midnight = datetime(day.year, day.month, day.day, tzinfo=PACIFIC)
       return midnight.astimezone(UTC), (midnight + timedelta(days=1)).astimezone(UTC)


   def expected_batches(day: date) -> Counter[str]:
       start, end = day_bounds(day)
       steps = int((end - start) / timedelta(minutes=15))  # 92, 96, or 100
       # One batch per timestamp: the collector skips a timestamp that already has a
       # batch, so the first fire wins (22:00 on stream days is a window batch only).
       batches: dict[datetime, str] = {}
       for i in range(steps):
           slot = resolve_slot(start + timedelta(minutes=15 * i))
           batches.setdefault(slot.ts, str(slot.tier))
       return Counter(batches.values())


   def check(conn, today: date) -> None:
       days = [today - timedelta(days=n) for n in range(DAYS, 0, -1)]
       since, until = day_bounds(days[0])[0], day_bounds(days[-1])[1]
       rows = conn.execute(
           "SELECT ts, tier, COUNT(*) FROM snapshots WHERE ts >= ? AND ts < ? GROUP BY ts, tier",
           (to_iso(since), to_iso(until)),
       ).fetchall()
       actual: dict[date, Counter[str]] = {day: Counter() for day in days}
       for ts, tier, _count in rows:
           actual[from_iso(ts).astimezone(PACIFIC).date()][tier] += 1
       for day in days:
           want, got = expected_batches(day), actual[day]
           flag = "" if want == got else "  <-- MISMATCH"
           print(f"{day} {day:%a}  expected {dict(want)}  actual {dict(got)}{flag}")


   if __name__ == "__main__":
       db = connect(os.environ["SCOUT_DB"], auth_token=os.environ.get("TURSO_AUTH_TOKEN"))
       check(db, datetime.now(PACIFIC).date())
   ```

   Today is excluded because it is incomplete.
   A plain SQL check with a fixed UTC offset misassigns batches on daylight-saving days, which is why this uses the time zone database.
   A `scout health` command with tests could replace this snippet later.

3. Turso usage (storage and rows written) in the Turso dashboard, against the free-tier limits on the pricing page.

To run checks against Turso from a local shell, load `.env` into the session first.
The app does not read `.env` by itself.
In PowerShell:

```powershell
Get-Content .env | Where-Object { $_ -match '^\s*[^#=]+=' } | ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item "env:$($k.Trim())" $v.Trim() }
```

## Known failures and what to do

**A run failed with "The job was not acquired by Runner".**
GitHub had no runner available; this is not a code problem.
For a baseline hour, a later run in the same hour samples that hour, so nothing is lost if one succeeds before the hour ends.
That sample reflects the later moment, not the original fire time.
A window slot has exactly one trigger, so a failed window run loses that 15-minute slot.
Seen on 2026-10-05 (five consecutive runs, baseline only, no data lost).

**Runs succeed but no batches appear.**
Check that the `SCOUT_DB` secret is set: the workflow exits successfully without collecting when it is empty.

**Turso returns 401 ("role was invalidated").**
The auth token was rotated somewhere but not everywhere.
Update `.env`, the GitHub secret, and reload the terminal, because a stale environment variable outlives the rotation.

**Runs stop entirely.**
Check the cron-job.org job history first (an expired PAT returns 401 or 403 there), then GitHub Actions.

**A run hits the 10-minute timeout.**
Check the database before deciding what was lost.
The batch is written in one transaction at the end of the run, so an interruption before that write normally leaves no batch, and the next run for the slot can collect it.
An interruption during or after the commit can leave a complete batch even though the job failed.
Run the coverage check, or query `snapshots` for the slot's `ts`, and resample only if no rows exist.
The usual cause of timeouts is slow per-statement writes to Turso; writes are batched 100 rows per statement for this reason.

**steam-sync printed a URL with the Steam key.**
Fixed by silencing httpx request logging; if it ever reappears, rotate the Steam key immediately.

## Quotas

| Service | Limit that matters | Where to check |
| --- | --- | --- |
| Twitch Helix | 800 points per minute for the app token; the client rate-limits itself and backs off on 429 | collect logs |
| Turso free tier | 5 GB storage, 10 million rows written per month | Turso dashboard, pricing page |
| GitHub Actions | free for public repositories; 10-minute timeout per run | Actions tab |

## Contingency

If cron-job.org or GitHub Actions become unreliable, [plans/aws-lambda-collector.md](plans/aws-lambda-collector.md) describes moving the collector to EventBridge Scheduler and Lambda.
Running collection from a local Windows machine was declined; ask before proposing it again.
