# Design decisions

A short log of decisions that a future reader needs to understand the project, with the reasoning that is not obvious from the code.
Newest first.

## The watchlist lives in the database and is managed from the CLI

Decided 2026-09-26.

**Decision.** `scout watch add/remove/list` stores watched games in a `watchlist` table (schema v3), keyed by Twitch category id.
The collector samples every entry during the window tier, and `scout rank` shows them in their own section with the same relaxed guards and `--max-channels` ceiling as owned games.

**Why.** Top-N sampling only catches a game that is currently big, so a small or new game the streamer wants to try is sampled rarely or never.
Keying by Twitch category (not Steam appid) lets a watched game be unowned or not on Steam at all.
The CLI and database were chosen over a repo file because adding a game should take effect at the next window slot without a commit, and a personal interest list does not belong in a public repo.
Names resolve through the same tiers as the Steam library; an unverified name is refused with the closest candidates rather than guessed.

**Duplicate categories.** Twitch search can list stale duplicates under one exact name (two "Anime Shop Simulator ✨").
When the only tie is between identically named categories, resolution defers to Get Games, which returns the one category Twitch maps that exact name to.
Ties between different names stay unresolved.

**Case-only twins.** Get Games matches names case-insensitively, so it is not authoritative when two categories differ only by letter case.
Observed 2026-09-29: "Dressmaker" (the popular new game, 100+ channels) came back from Get Games as the unrelated "DressMaker" (9 channels).
A Get Games hit whose case differs from the requested name is therefore confirmed with a search: it is accepted if it is the only category with that spelling, otherwise the exact-case category wins, and with no exact-case one the match is refused.
This costs one search per case-only match (12 in the current Steam library) from the same bounded search budget.

## Steam names resolve through conservative tiers, never by truncating a title

Decided 2026-09-26.

**Decision.** `steam/resolve.py` maps Steam library names to Twitch categories through ordered tiers, each applied only to names the earlier tiers left unresolved: a reviewed alias file (`twitch_scout/steam/aliases.toml`), exact match, separator variants (`X - Y` to `X: Y`), known edition/year/expansion qualifiers stripped, and a Search Categories fallback that accepts exactly one verified result.
Test and beta builds (PTS, Open Beta, Playtest) are not looked up.
`steam-sync` prints every non-exact match with its tier so a wrong one is visible.

**Why.** Exact matching alone left 35 of 269 owned games unresolved, including the streamer's most-played cozy game.
The mismatches were separators, edition qualifiers, a subtitle that only Twitch carries ("Tiny Aquarium" vs "Tiny Aquarium: Social Fishkeeping"), accents, and Twitch naming choices ("Counter-Strike 2" is streamed under "Counter-Strike").
The tiers resolved 25 more (259 of 270), with 4 left deliberately unresolved because the candidates were ambiguous or unverified.

**Alternative rejected.** Cutting a name at its first colon or dash and searching the remainder.
It does not fix the motivating cases, because Get Games is an exact match and Twitch keeps the subtitle ("Retro Rewind: Video Store Simulator").
It also reduces titles to series names that are different Twitch categories: "Divinity: Original Sin 2" becomes "Divinity" and "Endzone - A World Apart" becomes "Endzone", both of which exist.
A wrong match is worse than a miss, because it samples the wrong category and shows an unowned game as owned.
The useful part of that idea survives safely in the search tier: a result is accepted only if it is the full Steam title plus a subtitle, and only if exactly one result qualifies.

**Aliases.** Editorial renames that no rule should guess live in a small, reviewed file in the repo, added only after confirming the Twitch name.

## Steam owned library is a candidate source, not just a display column

Decided 2026-09-20.

**Decision.** `scout steam-sync` resolves the owned Steam library to Twitch categories and stores it.
The window-tier collector samples those categories even when they never enter the top-N, and `scout rank` lists owned games in a separate section under relaxed guards.

**Why.** Sampling only the top-N never observes a game the streamer owns that currently ranks low, which defeats the "what should I stream?" question.
Owned games therefore need to be sampled as their own candidate set.
The separate rank section with relaxed guards (a low viewer floor, one channel, one sample) surfaces low-ranked owned games.
A resolved owned game with no live streams reads as zero viewers and is dropped by the relaxed floor, which is exactly "exclude the dead game".
Sampling is confined to the window tier to keep the extra API calls off the hourly baseline.
Pruning removed games happens only on a complete fetch, so a malformed item in a Steam response can never delete a still-owned game.

**Scope.** v1 is owned games only.
Manual watchlist, then wishlist, are the planned follow-ups; the `source` column already distinguishes them, and the watchlist will reuse the name resolution above.

## Collection is triggered by an external scheduler, not GitHub `schedule`

Decided 2026-09-19.

**Decision.** `scout collect --tier auto` is triggered every ~15 minutes by a cron-job.org job that calls the GitHub `workflow_dispatch` API; the workflow's `schedule:` crons were removed.

**Why.** GitHub's scheduled events are best-effort: they are dropped and delayed under load, worst at the top of the hour, and high-frequency crons are throttled.
This hit the window tier hardest, which is the decision-grade data `scout rank` depends on.
Measured over four days, one Thursday stream window captured zero batches while every in-window scheduled fire was dropped.
Lowering the cadence and offsetting the crons off `:00` did not help.
An explicit `workflow_dispatch` is honored far more reliably, and the collector is idempotent (it skips an already-sampled slot before spending API calls), so any trigger cadence is safe.
Confirmed after the switch: a Saturday window captured all 16 slots with no gaps.

**Trade-off accepted.** A GitHub fine-grained PAT (Actions read/write, this repo, expiring) is stored in cron-job.org.
Secrets otherwise stay in a gitignored `.env` and GitHub repo secrets.

**Alternatives rejected.** An always-on VPS or Pi (more robust but adds cost and a host to maintain).
Local Windows Task Scheduler (declined by the user; ask before re-proposing).
Keeping GitHub `schedule` and accepting sparse data (fails the window tier, which is the point).
A further EventBridge to Lambda upgrade is documented as a contingency in [plans/aws-lambda-collector.md](plans/aws-lambda-collector.md) if cron-job.org proves insufficient.
