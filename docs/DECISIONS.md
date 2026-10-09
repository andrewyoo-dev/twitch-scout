# Design decisions

A short log of decisions that a future reader needs to understand the project, with the reasoning that is not obvious from the code.
Newest first.

## Grow into a web app for small English-language streamers, hosted on Vercel

Decided 2026-10-08.

**Decision.** The personal CLI becomes the base of a public web app that recommends games from a streamer's average viewers, stream hours, and genres.
The first scope is English-language game streamers averaging 1 to 30 viewers.
Language-specific values are stored as fixed English and all-language columns on `snapshots`, not per-language rows.
The web app will be hosted on Vercel; collection stays on GitHub Actions.
Details are in [PRODUCT.md](PRODUCT.md), [DATA_MODEL.md](DATA_MODEL.md) ("Planned: schema v5"), and [METHODOLOGY.md](METHODOLOGY.md) (Part 2).

**Why.** Existing stats sites rank categories globally; none answer where a stream of a given size would appear in a category for its own language and hours.
A narrow first scope keeps data, methodology, and support small enough to sustain at hobby pace.
Fixed English columns follow from that scope, not from a general preference for columns over rows: they keep writes at one row per category per batch.
Vercel was chosen because the user already knows it well; its Hobby plan is non-commercial, so monetization requires the Pro plan.

**Reviewed constraints.** The specification went through several rounds of external review.
The rules that came out of it, and that implementation must keep:

- Truncated listings, and listings that dropped invalid items, are not valid evidence; English completeness is recorded separately from all-language completeness.
- Viewer distributions are exact counts up to a stored `max`, so positions are exact apart from genuine ties.
- Metrics are computed per slot before they are summarized over a period.
- Missing data is never treated as zero viewers, and every slot and category has exactly one observation status.
- Recommendations present evidence and caveats, not promises of growth.

**Alternatives rejected.** Storing every stream as its own row (tens of thousands of rows per slot, beyond Turso's free write budget).
A separate distribution table (doubles rows written).
Fixed threshold counts such as "streams above 5 viewers" (too coarse: one example widened an exact 21st place into a 1st to 21st range).

## Sample the top 1000 during stream hours, rank from a 50-viewer floor, batch the writes

Decided 2026-09-29.

**Decision.** The window tier samples the top 1000 categories (`SCOUT_WINDOW_TOP_N`); the hourly baseline stays at 500.
The main ranking's default viewer floor drops from 100 to 50.
Snapshot batches are written with multi-row upserts of 100 rows instead of one statement per row.

**Why.** The streamer averages 3.3 viewers, and on the category directory a stream sits below every stream with more viewers.
Measured during stream hours, a 3-viewer stream lands around 5th in categories ranked 500-1000 (about 50-160 total viewers), 16th at ~360 viewers, and 50th or worse above ~1,000.
The top-500 cutoff during stream hours was about 110 viewers, so the best-fit band sat just outside what was being sampled.
Past rank ~1,000 (under ~45 viewers) the position gains only a slot or two while the audience keeps halving, so 1,000 is the stopping point; ranks 1,251-1,500 had one usable category in a sample of 30.
The old 100-viewer floor would have filtered out exactly the newly sampled categories.

**Batched writes.** The Turso driver sends one HTTP request per statement, and its `executemany` loops row by row.
A 500-row batch therefore took about 4-5 minutes of a 10-minute job, and 1,000 rows would have risked losing whole slots to the timeout.
Measured against Turso: 1,000 rows in 2.9 s batched, versus about 145 s row by row.
Rows are deduplicated by game id before writing, because a multi-row upsert cannot touch the same key twice.

**Next.** Channel count is only a proxy for position (one category had 100 channels, yet a 3-viewer stream would still sit 5th because most streams were at 0-3 viewers).
Storing a small viewer histogram per category would let rank sort by the streamer's actual expected position.

## The watchlist was removed

Decided 2026-09-29 (built 2026-09-26).

**Decision.** `scout watch add/remove/list` and its `watchlist` table were removed; migration v4 drops the table that v3 created.
No wishlist source will be built either.

**Why.** The app's purpose is to find categories where a ~3-viewer channel can pick up viewers.
Once the window tier samples the top 1,000, every category in that band is already observed automatically, including ones the streamer has never heard of.
A hand-kept list could only add categories below rank ~1,000, which during stream hours have under ~45 total viewers.
It also required knowing the game in advance, which is the opposite of discovery.
A wishlist fails the same test: a wished-for game in the top 1,000 is already ranked, and one outside it has no audience to find.

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

**Duplicate categories.** Twitch search can list stale duplicates under one exact name (two "Anime Shop Simulator ✨").
When the only tie is between identically named categories, resolution defers to Get Games, which returns the one category Twitch maps that exact name to.
Ties between different names stay unresolved.

**Case-only twins.** Get Games matches names case-insensitively, so it is not authoritative when two categories differ only by letter case.
Observed 2026-09-29: "Dressmaker" (the popular new game, 100+ channels) came back from Get Games as the unrelated "DressMaker" (9 channels).
A Get Games hit whose case differs from the requested name is therefore confirmed with a search: it is accepted if it is the only category with that spelling, otherwise the exact-case category wins, and with no exact-case one the match is refused.
This costs one search per case-only match (12 in the current Steam library) from the same bounded search budget.

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
A watchlist was built and later removed, and a wishlist was dropped (see "The watchlist was removed"); the top-1,000 window sampling covers that ground.

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
