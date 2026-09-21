# Twitch Category Scout

Handoff document. A personal tool for picking which game to stream next, based
on real data collected at the times that actually matter.

---

## 1. Context

**Who this is for:** AspenPK — Twitch: `twitch.tv/aspenpk`, YouTube:
`youtube.com/@AspenPK`.
**Schedule:** Tue / Thu / Sat, 7:00 PM PT, ~3 hours.
**Channel size:** ~58 followers, averaging 2–6 concurrent viewers.
**Games:** job sims, life sims, crime sims, survival/building, horror.
**Developer:** the streamer himself — 4.5 years as an AI engineer. Skip the
API-101 explanations.

### Why this exists

Picking the next game by gut has failed repeatedly, in two distinct ways:

1. **Empty categories.** Carpet Cleaning Simulator and Cyberpunk Store Simulator
   had literally zero live channels. Great "exposure" on paper, no viewers in
   reality — nobody browses a category nobody streams.
2. **Missed spikes.** ReStory peaked in July at ~1,296 avg viewers, fell to 487
   in August, and sits near 78 live now. Arriving weeks late means arriving
   after the audience left.

There is also a structural constraint that no amount of cleverness fixes: he
works a day job on the US West Coast and streams Tue/Thu/Sat evenings. He
**cannot** be there for a launch-day spike unless the launch happens to land on
his schedule. So "be early to new releases" is not a viable primary strategy for
him — steady, long-lived categories are.

Existing sites (SullyGnome, TwitchTracker) answer general questions well but not
his specific one: *what does this category look like at 7PM PT on a Tuesday?*

---

## 2. Hard constraint: do not scrape SullyGnome or TwitchTracker

SullyGnome explicitly asks people not to scrape it — it's a hobby project with
limited resources. TwitchTracker's terms prohibit automated collection. Both are
small operations where scraping does real harm.

**Pull from the Twitch API directly instead.** It is the same source those sites
use, it is officially supported, and it is free.

---

## 3. What the Twitch API does and does not give

**Gives:** a snapshot of *right now*.

- `Get Top Games` — games ordered by current viewers, paginated.
- `Get Streams` — live streams, filterable by `game_id`, each with `viewer_count`.

Aggregating `Get Streams` for a game yields live viewers and live channel count —
the same two numbers TwitchTracker shows as "Live viewers / Live channels".

**Does not give:** any history. No 7-day average, no trend, no peak-last-month.
That is precisely the value those stats sites add, and it is what this tool must
build for itself by sampling over time.

Auth is an **App Access Token** (client credentials). No user login, read-only.

---

## 4. The core idea

Do not rebuild a general stats site. Collect the slice that matters:

> **Sample the categories at the exact hours he streams, and judge candidates on
> those samples rather than on 24/7 averages.**

Category viewership swings hard by hour. A 24-hour average blends in times he
will never be live. Two games with identical daily averages can look completely
different at 7PM PT on a Tuesday, and that difference is the whole decision.

Sampling more broadly than that is still useful for trend detection (§6), but
the stream-window samples are the primary signal.

---

## 5. Collection

```
scheduler → Twitch API (Get Top Games → Get Streams per game)
          → append snapshot rows with timestamp
          → SQLite
```

**Two sampling tiers:**

| Tier | When | Why |
| --- | --- | --- |
| Stream-window | Tue/Thu/Sat, 6:30–10:30 PM PT, every ~10 min | The decision-grade data |
| Baseline | Every hour, always | Trend detection, hour-of-day profiles |

Start with roughly the top 500–1000 games; below that, categories are too small
to matter (see §7).

**Schema sketch:**

```
snapshots(ts, game_id, game_name, viewers, channels)
```

One row per game per sample. Everything else — averages, trends, hour profiles —
is a query over this table, not a stored field. Keep the raw samples; derived
numbers can always be recomputed, but lost samples are gone.

SQLite is sufficient. This is a single-user tool collecting a few thousand rows
an hour.

---

## 6. Metrics that actually drove decisions

These are the ones that changed his mind during analysis. Implement these first.

**Viewers per channel.** Viewers ÷ channels. High means demand outstrips supply.
But see the noise warning in §7 — this metric lies constantly when computed
naively.

**Channel count in his window.** More directly useful than viewer count. With
2–6 viewers he will land low in the directory listing, so what matters is how
many channels sit above him. Categories with 3–30 concurrent channels are the
workable band.

**7-day vs 14-day comparison.** The single best signal for "is this rising or
dying". If the 7-day average sits below the 14-day, the category is cooling —
ReStory showed exactly this pattern before the monthly numbers confirmed the
collapse. Flag every candidate as rising / stable / falling.

**Spike detection.** A single large streamer can move a small category by 20x.
TCG Card Shop Simulator showed 1,809 live viewers across 28 channels at one
moment against an 82-viewer 7-day average — one big channel, briefly. Compare
current readings against the category's own recent median and mark outliers, or
they will be mistaken for genuine growth.

**Floor value.** The minimum the category sustains over months. TCG Card Shop
Simulator never dropped below ~60–70 viewers across two years. That floor is the
real audience; everything above it is weather. A high floor means the game can
be saved for later without the opportunity disappearing — which is exactly why
it got deferred behind ReStory.

---

## 7. Noise traps — read before writing the ranking logic

Both of these produced garbage candidate lists during manual analysis. Guard
against them explicitly.

**Trap 1 — ratio sorting surfaces pure noise.** Sorting by viewers-per-channel
puts one-off events on top: a category with 1 streamer, 60 minutes of airtime,
and one big broadcaster yields an absurd ratio. Real examples that topped such a
list: Claw Machine Sim, Ratatouille, Iron Man 2. Nobody is there the rest of the
time.

*Guard:* require a minimum number of distinct streamers and a minimum average
concurrent channel count (≥3) before a category is eligible to rank at all.

**Trap 2 — the floor below which nothing works.** Categories averaging under
~100 viewers do not produce meaningful inflow regardless of how favourable the
ratio looks. Ranks past ~650 on SullyGnome's watch-time list were all in this
band. Doloc Town confirmed it in practice: a genuinely enjoyable game that drew
2 average viewers and 0 new followers, purely because too few people browse that
category.

*Guard:* a viewer floor in the filter, and make it a tunable rather than a
constant — the right threshold will shift as the channel grows.

---

## 8. Output

A ranked candidate list, filtered to his constraints. Minimum useful columns:

```
game | viewers @ his window | channels @ his window | ratio | 7d vs 14d trend | floor
```

Two joins make it substantially more useful:

- **Steam library** (260 games, Steam ID `kamagui`) — flags games he already
  owns and has played, where taste is already proven
- **Steam wishlist** — flags upcoming releases, useful as a release calendar

Steam's Web API can pull owned games and playtime. Wishlist access is less
reliable; a manual export is an acceptable fallback.

CLI output is fine. This is a tool he runs before deciding what to stream, not a
product.

---

## 9. Engineering constraints

Follow the project's `coding-standards` skill. The ones that bite here:

- **Respect Twitch's rate limits.** Token bucket, and back off on 429. The
  collector runs unattended; getting the app throttled silently breaks weeks of
  data.
- **Bound the pagination loop.** `Get Top Games` pages until exhausted — cap it.
- **Timeout and failure path on every API call.** A failed sample should log and
  skip, never crash the collector or leave a partial row.
- **Idempotent writes.** Re-running a sample for the same timestamp must not
  duplicate rows. The collector will get restarted mid-run.
- **Validate API responses at the boundary.** Missing `viewer_count`, renamed
  games, and null `game_id` all occur in practice.
- **Inject the clock and the API client** so ranking logic can be tested against
  fixture data instead of waiting days for real samples to accumulate.

---

## 10. First milestone

1. App Access Token auth
2. One-shot collector: `Get Top Games` → `Get Streams` → write snapshot rows
3. Scheduler for the two sampling tiers
4. A query that ranks candidates with the §7 guards applied

Useful ranking needs history, so expect roughly two weeks of collection before
the trend metrics mean anything. The filters and guards can be built and tested
against fixtures in the meantime.

---

## 11. Open questions

- Sampling interval — 10 min during stream windows may be finer than necessary.
- How far down the game list to collect. Top 1000 is a guess.
- Whether to record per-stream rows (language, title, individual viewer counts)
  or only category aggregates. Per-stream is much larger but enables "how many
  English channels are above me right now", which is closer to the real question.
- Whether to track his own channel's numbers in the same store for correlation.
