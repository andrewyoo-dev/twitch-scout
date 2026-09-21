# Design decisions

A short log of decisions that a future reader needs to understand the project, with
the reasoning that is not obvious from the code.
Newest first.

## Collection is triggered by an external scheduler, not GitHub `schedule`

Decided 2026-09-19.

**Decision.** `scout collect --tier auto` is triggered every ~15 minutes by a
cron-job.org job that calls the GitHub `workflow_dispatch` API; the workflow's
`schedule:` crons were removed.

**Why.** GitHub's scheduled events are best-effort: they are dropped and delayed
under load, worst at the top of the hour, and high-frequency crons are throttled.
This hit the window tier hardest, which is the decision-grade data `scout rank`
depends on. Measured over four days, one Thursday stream window captured zero
batches while every in-window scheduled fire was dropped. Lowering the cadence and
offsetting the crons off `:00` did not help. An explicit `workflow_dispatch` is
honored far more reliably, and the collector is idempotent (it skips an
already-sampled slot before spending API calls), so any trigger cadence is safe.
Confirmed after the switch: a Saturday window captured all 16 slots with no gaps.

**Trade-off accepted.** A GitHub fine-grained PAT (Actions read/write, this repo,
expiring) is stored in cron-job.org. Secrets otherwise stay in a gitignored `.env`
and GitHub repo secrets.

**Alternatives rejected.** An always-on VPS/Pi (more robust but adds cost and a host
to maintain); local Windows Task Scheduler (declined by the user; ask before
re-proposing); keeping GitHub `schedule` and accepting sparse data (fails the window
tier, which is the point). A further EventBridge -> Lambda upgrade is documented as a
contingency in [plans/aws-lambda-collector.md](plans/aws-lambda-collector.md) if
cron-job.org proves insufficient.

## Steam owned library is a candidate source, not just a display column

Decided 2026-09-20.

**Decision.** `scout steam-sync` resolves the owned Steam library to Twitch
categories and stores it; the window-tier collector samples those categories even
when they never enter the top-N, and `scout rank` lists owned games in a separate
section under relaxed guards.

**Why.** Sampling only the top-N never observes a game the streamer owns that
currently ranks low, which defeats the "what should I stream?" question. Owned games
therefore need to be sampled as their own candidate set. The separate rank section
with relaxed guards (a low viewer floor, one channel, one sample) surfaces
low-ranked owned games; a resolved owned game with no live streams reads as zero
viewers and is dropped by the relaxed floor, which is exactly "exclude the dead
game". Sampling is confined to the window tier to keep the extra API calls off the
hourly baseline.

**Scope.** v1 is owned games only. Manual watchlist, then wishlist, are the planned
follow-ups (the `source` column already distinguishes them).
