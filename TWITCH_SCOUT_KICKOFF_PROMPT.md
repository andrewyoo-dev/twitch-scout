# Kickoff prompt for Claude Code

Paste this as the first message in a new Claude Code session, with
`TWITCH_SCOUT_HANDOFF.md` in the working directory.

---

I'm building a personal tool that tells me which game to stream next, using
Twitch API data sampled at the hours I actually stream. Read
`TWITCH_SCOUT_HANDOFF.md` first — it has the context, the metrics that matter,
and two specific noise traps that wrecked my manual analysis.

About me: AI engineer, ~4.5 years. Skip explanations of how REST APIs or OAuth
work.

**Stack:** Python. Twitch Helix API with an App Access Token (client
credentials, read-only). SQLite for the sample store. CLI output.

**Do not scrape SullyGnome or TwitchTracker** — the handoff explains why. Twitch
API only.

**Follow the `coding-standards` skill.** Especially: rate limiting with backoff,
bounded pagination, timeout and failure path on every API call, idempotent
writes (the collector will get restarted mid-run), and injecting the clock and
API client so the ranking logic is testable against fixtures instead of waiting
two weeks for real samples.

**Goal for this session:**

1. App Access Token auth
2. One-shot collector — `Get Top Games` → `Get Streams` per game → write
   timestamped snapshot rows
3. Scheduler for two sampling tiers (my stream windows at high frequency, plus
   an hourly baseline)
4. A ranking query with the noise guards from section 7 applied

The thing I care most about: **the guards are the product.** Sorting by
viewers-per-channel without a minimum-streamer floor surfaces pure garbage —
categories where one big streamer went live for an hour. I already burned time
on a candidate list topped by Ratatouille and Iron Man 2. Build the filters so
that can't happen.

Store raw samples and compute everything else as queries — I want to be able to
change the metrics later without having lost data.

Start by proposing the schema and the module layout. Don't write the full
implementation until I've agreed on the structure.
