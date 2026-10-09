# Methodology

How collected samples become recommendations.
Part 1 describes the CLI ranking that exists today.
Part 2 is the reviewed specification for the web app's recommendations, not yet implemented.
Stored fields are defined in [DATA_MODEL.md](DATA_MODEL.md); product intent is in [PRODUCT.md](PRODUCT.md).

## Part 1: current CLI ranking (`scout rank`)

Code: [twitch_scout/rank/](../twitch_scout/rank/).

### Input data

Only window-tier rows from the last `--days` days (default 14) are used.
The window is Tuesday, Thursday, and Saturday, 18:30 to 22:30 Pacific time, sampled every 15 minutes.
Baseline rows are not used for ranking.

### Pipeline

1. **Aggregate** per category: number of window samples, average viewers, average channels, and average viewers over the last 7 days.
2. **Guards** remove noise; every failed guard is reported with its reason (`--show-rejected`).

   | Guard | Default | Purpose |
   | --- | --- | --- |
   | Viewer floor | 50 | Categories too small to bring any inflow. |
   | Minimum average channels | 3 | One-off events carried by a single stream. |
   | Minimum samples | 3 | Categories seen only briefly. |
   | Maximum average channels | off | Crowded categories (`--max-channels`). |

3. **Enrich** the eligible set:
   - Trend: last-7-day average versus the full-window average, with a ±10% dead band, labeled rising, stable, or falling.
   - Floor: the 10th percentile of window viewers.
   - Spike: the latest reading above 3 times the window median.
4. **Sort** by a discoverability score, `viewers^(1-c) * channels^c`, with `c = 0.75` by default (`--concentration-penalty`).
   Raw viewers per channel floats categories carried by one large stream; weighting channel count favors categories where many small streams draw viewers.

Owned Steam games are ranked in a separate section with relaxed guards (floor 10, one channel, one sample).

### Known limits

- Channel count is only a proxy for position: a category with 100 channels can still put a 3-viewer stream 5th if most streams have 0 to 3 viewers.
- All languages are counted together.
- A trend can come from a few samples in a single evening.
- `truncated` is ignored, so very large categories have partial totals that can also include streams counted twice across pages.
- Two gambling-adjacent categories with captive audiences still rank high.

Part 2 addresses the first three directly.

## Part 2: web recommendations (specification)

Status: specified and reviewed, not implemented.
Thresholds are configuration with initial values, to be tuned from tester feedback.

### Inputs

- `V`: the streamer's average concurrent viewers.
  Decimals are rounded half up (`floor(x + 0.5)`, so 3.5 becomes 4), with a minimum of 1.
  Python's built-in `round()` is not used because it rounds 3.5 and 4.5 both to 4.
  The display says which integer was used ("based on 3 viewers").
- Stream days and hours with a time zone, converted to the set of UTC slots they cover.
- An optional genre filter.

### Step 1: compute per slot, then aggregate over the period

Every metric is computed inside each slot first, and only then summarized over the recommendation window.
Summarizing first gives wrong answers.
Example with three slots of (English viewers, top English stream):

| Slot | `en_viewers` | `en_top_viewers` | Non-top viewers |
| --- | --- | --- | --- |
| 1 | 100 | 10 | 90 |
| 2 | 100 | 99 | 1 |
| 3 | 12 | 11 | 1 |

`median(en_viewers - en_top_viewers)` is 1, which is correct: in most slots almost nobody watches anything but the top stream.
`median(en_viewers) - median(en_top_viewers)` is 89, which is wrong.

### Step 2: slot eligibility

A slot contributes to a category's English metrics only if:

- the slot is "collected" and the category is "observed" (see "Observation status" in [DATA_MODEL.md](DATA_MODEL.md)),
- `en_complete = 1`, which also means no English stream was dropped by validation, and
- `V <= max` in that row's `en_dist`.

Slots failing any condition are counted by cause and reported separately, never treated as zero viewers.
All-language comparison values additionally require `truncated = 0` and `all_dropped = 0`; otherwise they are shown as incomplete.

### Step 3: per-slot metrics

From the English distribution, with `gt(k)` the number of streams with more than `k` viewers:

- `gt(k) = sum(counts[k+1 .. max]) + over`
- `ge(k) = gt(k) + counts[k]`

For a stream with `V` viewers that is not yet live in the category:

- **Optimistic position** = `1 + gt(V)`: placed ahead of every stream with the same viewer count.
- **Pessimistic position** = `1 + ge(V)`: placed behind them.

The gap between the two comes only from ties, and the display says so ("12 other streams also have 3 viewers").
Because the distribution is exact up to `max`, an input like 6 viewers among twenty 7-viewer streams gives exactly 21st, not a range of 1st to 21st.

Other per-slot values:

- **English audience**: `en_viewers`.
- **Non-top viewers**: `en_viewers - en_top_viewers`.
- **Top share**: `en_top_viewers / en_viewers`, only when `en_viewers > 0`.

Zero cases are kept distinct:

| Case | Position | Top share | Counts toward "no English streams" |
| --- | --- | --- | --- |
| `en_channels = 0` | 1 | not computable | yes |
| `en_channels > 0`, `en_viewers = 0` | 1 | not computable | no |

### Step 4: period summary per category

Over the eligible slots in the recommendation window:

- Median optimistic and pessimistic position.
- **Top-10 share**: the fraction of eligible slots whose pessimistic position is 10 or better.
  Displayed as "top 10 when sorted by viewer count", never as "first screen".
- Median and 10th percentile of English audience.
- Median non-top viewers.
- Median top share, over slots where it is computable, with the number of slots it used.
  If no slot has a computable share, the summary is unknown, not zero.
- No-English share: slots with `en_channels = 0` over eligible slots.
- Coverage: eligible slots, distinct days, and counts of each excluded slot status.

### Step 5: filter and order

| Rule | Initial value | Effect |
| --- | --- | --- |
| Audience floor | median English audience ≥ 30 | Required to be listed. Categories with no English audience are excluded here, not shown as a fallback. |
| Visibility | median pessimistic position ≤ 10 | Required to be listed. |
| Concentration | median top share ≥ 0.6 | Hidden by default, shown with the reason when the user opts in. An unknown top share never triggers this rule. |
| Data sufficiency | at least 8 eligible slots and 2 distinct days | Otherwise flagged "not enough data" and listed after sufficient entries. |
| Trend | at least 14 days of English data | Otherwise no trend is shown. |
| Genre | IGDB genres via the category's `igdb_id` | Categories without an IGDB id are excluded while a genre filter is active, and the count is shown. |

The audience floor carries over the CLI's viewer-floor guard ("Trap 2" in the handoff): a category nobody browses brings no viewers, however good the position.
Its initial value is a hypothesis for English-only audiences, roughly the CLI's 50 total viewers scaled by the English share seen during stream hours.

Order: median non-top viewers, highest first; ties broken by top-10 share.
Non-top viewers is an explainable measure of an audience not tied to one stream.
It is not a measure of how many viewers would move to a new stream, and the copy must not imply that.

### Step 6: reasons and caveats

Each entry states facts from Step 4 rather than conclusions:

- Reasons: the expected position range, the English audience size, and how evenly viewers are spread.
- Caveats, shown whenever they apply:
  - few observed days or missing slots, with counts;
  - incomplete all-language comparison;
  - "Twitch's default sort is Recommended For You, so the real position may differ."

### Worked example (synthetic numbers)

Input: 3 viewers, English, Saturdays 19:00 to 22:00 Pacific.
Recommendation window: 14 days, so 24 slots (2 Saturdays × 12 slots).

| Value | Result |
| --- | --- |
| Eligible slots | 22 of 24 (1 not collected, 1 with an incomplete English listing) |
| Distinct days | 2 |
| Median position | 3rd to 5th (2 streams above, 2 more tied at 3 viewers) |
| Top-10 share | 95% |
| English audience | median 70, 10th percentile 35 |
| Median non-top viewers | 43 |
| Median top share | 38%, below the 0.6 threshold |
| All-language comparison | 6th to 9th, 120 viewers |

Displayed as:

> **Example Game** · 3rd to 5th among English streams (based on 3 viewers) · about 70 English viewers (35 on slow nights)
> Reasons: few English streams above you; viewers are spread across streams.
> Caveats: only 2 days observed; Twitch's default sort is Recommended For You, so the real position may differ.

### Tests required before collection starts

Synthetic-data tests must cover:

- truncated all-language listing with a complete English refetch;
- a listing that reaches its last page but drops an invalid stream item, for both the all-language listing and the English refetch, with a valid control;
- English refetch failure, page cap reached, and run budget exhausted;
- ties at the input viewer count;
- every slot state and category status in [DATA_MODEL.md](DATA_MODEL.md): no run, a `started` run, a failed category listing, a dropped category item, a category stream failure, an English-only refetch failure with a written row, a successful retry, a skipped run, and a forced rerun;
- `en_channels = 0`, English streams that all have 0 viewers, a period where every slot has 0 English viewers, and a mix of zero and positive slots;
- duplicate streams across pages;
- input above a row's distribution `max`;
- half-up rounding;
- malformed distribution JSON.
