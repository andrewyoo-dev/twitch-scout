# Product

What this project is for, who it serves first, and how we will know it works.
This is the current source of product intent.
[TWITCH_SCOUT_HANDOFF.md](TWITCH_SCOUT_HANDOFF.md) is the original personal-tool brief and is kept as history.
How recommendations are computed is in [METHODOLOGY.md](METHODOLOGY.md); the stored data is in [DATA_MODEL.md](DATA_MODEL.md).

## Problem

Small streamers pick their next game by gut feel, and it often fails in two ways.
A category can look open but have nobody browsing it, or it can look busy while every viewer is watching one large stream.
General stats sites answer "what is popular" with one global list.
They do not answer the question a small streamer actually has: "at my size, in my language, at the hours I stream, which games put me where people will see me?"

## Direction

Decided 2026-10-08: grow the personal CLI into a public web app.
A streamer enters their average viewer count, stream hours, and preferred genres.
The app lists games where a stream of that size is likely to appear near the top of the category directory, with the reasons and the limits of each recommendation.

The product provides **evidence for choosing a game**, not a promise of growth.
Every recommendation shows why it was made and what the data cannot tell.

The aspiration is real users and possibly a second income.
The reality is hobby pace: work happens in bursts with weeks off in between.
Every design choice should survive a few weeks of nobody looking at it.

## First target user

English-language game streamers averaging **1 to 30 concurrent viewers**.

The scope is deliberately narrow.
It keeps the stored data small, the methodology checkable, and the support burden low.
Other languages and larger channels come only after this group finds the tool useful.

## What makes it different

The stats sites we know of rank categories by global popularity or a single viewers-per-channel ratio.
That is a positioning hypothesis, not a survey; checking existing tools is an open question below.
This app personalizes three things:

- **Expected directory position for the streamer's own viewer count**, counted among English streams.
- **The streamer's own stream hours**, rather than 24-hour averages.
- **Concentration**: whether a category's audience is spread out or captive to one large stream.

Owned Steam games (already built for the CLI) are a later addition to the web app.

## MVP scope

**Inputs.**

- Average concurrent viewers (1 to 30).
- Stream days and hours, with a time zone.
- Genre filter (optional).

**Output.**
A ranked list of categories.
Each entry shows the expected position range, English viewer size, concentration, how much data backs it, and its reasons and caveats in plain language.

**Feedback.**
A per-recommendation "this looks wrong" control, stored in our own database.

## Non-goals (for now)

- Promising or predicting channel growth.
- Languages other than English, or channels above 30 viewers.
- Filling in average viewers from a channel name.
  The Twitch API gives a channel's language and current viewers, but no historical average.
  Channel lookup may come later as a convenience.
- User accounts, logins, or saved profiles.
- Scraping third-party stats sites (see the handoff, section 2).

## Success criteria

Validation is direct, not inferred from traffic.

- Recruit **5 to 10 streamers** who agree to a usage test.
- Ask them how many times a week they used it, whether it changed a game choice, and which results looked wrong.
- The signal we want: they come back before their next stream without being reminded.

Web analytics shows only inflow size.
Neither Cloudflare nor Vercel Web Analytics can tell whether the same person returned on a later day, so they are a secondary signal.

## Roadmap

Each step leaves something usable, so a pause at any point loses nothing.

1. **Data foundation.**
   Store per-category English and all-language viewer distributions, concentration, completeness flags, and collection run records.
   Verify the position and period calculations against synthetic data before collecting.
   See [DATA_MODEL.md](DATA_MODEL.md), "Planned: schema v5".
2. **Collection experiment.**
   Run the new collection for a day, measure storage size, rows written, and query cost, then decide 24-hour collection depth and retention.
3. **Small web app**, in parallel with step 2.
   Inputs, ranked list, reasons and caveats, feedback control, genre filter.
4. **Usage test** with 5 to 10 streamers.
5. **Later**: channel-name lookup, Steam library, other languages.

## Constraints that shape the product

- **The streamer's own game search never breaks.** The project exists first to pick the next game for the streamer who builds it, and that has to work on any day, at any step of the roadmap.
  `scout rank` and the window-tier collection it reads must keep working through every change.
  Schema changes are additive, so existing queries keep running on old and new rows.
  A collection change must keep the streamer's stream hours sampled at least as deeply as today.
  A change that would replace the CLI ranking ships its replacement first.
- **Name.** Twitch rejects app names containing "twitch", so the public product needs another name before launch.
- **Directory order is approximate.** Twitch's default category sort is "Recommended For You", which is personalized and not public.
  Position by viewer count is the best observable proxy and is always labeled as such.
- **Monetization gates.** Check these before adding ads, donations, or paid features:
  - Vercel's Hobby plan is for non-commercial personal use only; commercial use needs Pro.
  - IGDB's API is free for commercial projects too, but monetized products are asked to join its partner program and show visible attribution to IGDB.com.
  - The Twitch Developer Services Agreement governs use of Twitch data.

## Open questions

- Product name.
- Which existing tools already offer per-streamer recommendations; a short survey before launch.
- Default recommendation window (the CLI uses 14 days) and raw-data retention period; decided after the collection experiment.
- Where to recruit the first testers (r/Twitch small-streamer threads and streamer Discords are the likely places).
- Initial thresholds (concentration 60%, minimum 8 observed slots) are hypotheses to tune from tester feedback.
