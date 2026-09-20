# Handoff: collection reliability

> **Status (2026-09-19): RESOLVED.**
> Collection now triggers every ~15 min via a cron-job.org job calling GitHub `workflow_dispatch`; the unreliable `schedule:` crons were removed.
> This document is kept as the record of the problem and why that approach was chosen (option 1 below).

Paste this into a fresh Claude Code session, in the `twitch-scout` working directory.

I need help solving one specific problem: **the automated collector is not reliably
capturing the samples that matter.** The tool itself is built and working; this is an
operations/scheduling problem, not a code-correctness one.

---

## 1. What the tool is (one paragraph)

`twitch-scout` samples the Twitch Helix API on a schedule, stores raw snapshots in
Turso (libSQL), and ranks which game to stream next. It samples in two tiers
(`twitch_scout/collect/tiers.py`):

- **window** — Tue/Thu/Sat 18:30–22:30 PT, every ~20 min. *The decision-grade data.*
- **baseline** — hourly, always. Trend and hour-of-day context.

`scout rank` only produces the streamer's real answer from **window-tier** samples
(it needs several per category). Baseline is supporting context. So window coverage
is the whole point.

Pipeline is verified end-to-end: real Twitch → Turso writes succeed, guards/ranking
work, 149 tests green. Repo: github.com/andrewyoo-dev/twitch-scout (public). See
`TWITCH_SCOUT_HANDOFF.md` for the product rationale and the noise guards.

## 2. The problem

Collection runs on **GitHub Actions `schedule`** (`.github/workflows/collect.yml`).
GitHub's scheduled events are best-effort — they are dropped and delayed under load,
worst at the top of the hour, and high-frequency crons are throttled. In practice
GitHub is delivering only a fraction of the scheduled runs, and it is hitting the
window tier hardest.

The workflow itself is healthy: when a run does fire, install + app-token auth +
collect + Turso write all succeed (500/500 games in ~1–3 min).

## 3. Evidence (as of 2026-09-18)

Queried directly from Turso (`SELECT ts, tier, COUNT(*) FROM snapshots GROUP BY ...`):

- **window-tier batches ever: 1** — only `2026-09-16T03:30Z` (a Tuesday).
- **Thursday 2026-09-17 window (= 09-18 01:30–05:30 UTC): 0 batches.** Every
  in-window scheduled fire was dropped. The runs that *did* execute around then
  landed at **05:52+ UTC — after the 22:30 PT / 05:30 UTC window close** — so they
  were (correctly) classified baseline.
- Baseline coverage is also sparse: ~20 batches total over ~4 days where hourly alone
  should be ~24/day.

Already tried and **not sufficient** (commit `8250d26`): lowered the window cron
10 → 20 min and offset both crons off `:00` (baseline `23 * * * *`, window
`7,27,47 1-6 * * 3,5,0`). Thursday still got 0 window samples. Offsetting/reducing
cadence does not fix GitHub dropping the runs.

Note: baseline slot timestamps read as `:00` because the slot is floored to the hour
in `tiers.py`, not because the cron fires at `:00` — the `:23` offset is working.

## 4. Why it matters

Without reliable window samples, `scout rank` never yields the streamer's actual
answer ("what does this category look like at 7 PM on my stream nights"). Weeks could
pass without enough window data. This blocks the core value of the tool.

## 5. Options to weigh (the decision I need help with)

1. **External cron → GitHub `workflow_dispatch`** (e.g. cron-job.org, free). An
   explicit dispatch is honored more reliably than a `schedule` event. Still runs on
   GitHub runners; requires a GitHub PAT stored in the third-party service (weigh the
   secret-sharing).
2. **Always-on host** (cheap VPS / Raspberry Pi) running real `cron` + `scout
   collect`. Most robust, 24/7; small cost/setup; secrets stay on that host.
3. **Local Windows Task Scheduler** on the streamer's own PC — reliable *during
   windows* because the PC is on when streaming; free; no secret-sharing.
   **The user has already declined this approach** (reason not captured). Do not
   re-propose it without first asking the user why it was rejected.
4. **Keep GitHub `schedule`, accept sparse data.** Rejected on the merits: it fails
   the window tier, which is the point.

No option is chosen yet. Help evaluate and, once the user picks, implement it. Keep
GitHub Actions for best-effort baseline regardless of which is chosen for window.

## 6. Constraints / things to respect

- The streamer works a US-West day job and streams Tue/Thu/Sat evenings PT.
- Don't put secrets in the repo or in chat. Secrets live in a gitignored `.env`
  locally and in GitHub repo secrets: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`,
  `SCOUT_DB` (Turso URL), `TURSO_AUTH_TOKEN`.
- Follow the `coding-standards` skill (bounded loops, timeouts, injected deps, tests).
- `scout collect --tier auto` self-decides tier from the clock and is idempotent
  (skips an already-sampled slot before spending API calls), so any trigger can call
  it at any cadence safely.

## 7. Unrelated backlog (do NOT let it distract from the above)

From an external review, queued for later: Steam as a *candidate source* (not just a
display column), separating "≥3 samples" from a trustworthy 7d/14d trend
("insufficient history"), a connection leak in `connect()` on migration error,
CLI input validation, `truncated` handling in ranking, and stale README/workflow
comments. These are tracked in the session memory; the scheduling problem above comes
first.
