# Spike 0.3d: macro data timing (VIX, VIX3M, FRED)

**Run:** 2026-10-09, `uv run --group spikes python -m spikes.macro <snapshot|fred_lag|poll>`. Raw output in `spikes/macro/output/` (git-ignored).
**Status: DONE** (2026-10-13). The history checks ran on 2026-10-09; the evening timing watch ran on Monday 2026-10-12, 11:05–22:17 ET ([results](#evening-watch-monday-2026-10-12-poll)). One optional follow-up, which doesn't change the decision: FRED's lag after a holiday (re-check with `fred_lag`). The pre-market snapshot was skipped and is no longer needed (agenda #5: `premarket_check` reads settled closes only).
**Decision: ADJUST** (approved at 0.4 on 2026-10-13, agenda #5). CBOE's history files appear 1.5–5 hours after the 17:25 regime job, and FRED at least a full session later. The evening regime reads VIX/VIX3M from **CBOE's delayed-quote JSON**, final by **16:32 ET** and equal to the history row on both samples, with the history file and FRED as fallbacks.

## Sources checked

| Source | URL (all free, no key except FRED) |
|---|---|
| CBOE history files | `https://cdn.cboe.com/api/global/us_indices/daily_prices/{VIX,VIX3M}_History.csv` (307 redirect to `cdn-api.cboe.com`) |
| CBOE delayed quotes | `https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{VIX,VIX3M}.json` |
| FRED | series `VIXCLS`, `VXVCLS` (VIX3M), `BAMLH0A0HYM2` (ICE BofA high-yield option-adjusted spread) |

## Results

| Question | Answer |
|---|---|
| History depth | VIX from 1990-01-02, VIX3M from 2009-09-18 (FRED's VXVCLS from 2007-12-04). Both cover 2016 onward |
| Gaps 2016 → 2026-10-08 | **None** in either CBOE file (2,707 expected sessions) |
| Extra rows | **The VIX file has rows on US market holidays from 2022 on** (e.g. 2022-05-30, 2023-01-16, 2024-02-19): CBOE computes VIX during global trading hours. The ingest must keep only `market_sessions` dates. VIX3M has none |
| CBOE vs FRED values | **Identical**: 0 differences over 2,740 VIX days and 2,707 VIX3M days |
| **When the history files update** | The 2026-10-08 rows: `Last-Modified` **VIX3M 18:31 ET, VIX 21:11 ET**. Both after the 17:25 `regime` job. The CDN adds up to 15 more minutes (`s-maxage=900`, `stale-while-revalidate=900`) |
| Delayed-quote JSON | Updates every few seconds (`s-maxage=5`), **15 minutes delayed** (at 09:57 ET it showed `last_trade_time` 09:42). Fields: `current_price`, `close`, `prev_day_close`, `open`, `high`, `low`, `last_trade_time`. VIX settles at 16:15, so the final value should be there around **16:30–16:35 ET**. Confirmed Monday: final at 16:32 ET ([below](#evening-watch-monday-2026-10-12-poll)) |
| **FRED lag** | `last_updated` of VIXCLS / VXVCLS: **2026-10-09 08:38 CT (09:38 ET) for the 2026-10-08 value**. FRED is one session behind at 17:25, and still behind at the 07:30 `premarket_check` |
| FRED vintages (ALFRED) | Not usable for timing: each vintage is dated by its observation date (59 of 60 show "lag 0") |
| Credit spread (`BAMLH0A0HYM2`) | **Only 3 years of history on FRED** (from 2023-10-10): "Starting in April 2026, this series will only include 3 years of observations." It can't be backtested to 2016. No README rule uses it today |

### Weekend snapshot (Sunday 2026-10-11, `snapshot`)

| Question | Answer |
|---|---|
| Delayed-quote JSON after the close | Still holds Friday's final values all weekend: `close` VIX **14.84**, VIX3M **17.77**, `last_trade_time` **2026-10-09 16:15:01** (the settlement) |
| **JSON = history row?** | **Yes, on this sample:** the CSV rows for 2026-10-09 are VIX 14.84 and VIX3M 17.77. Monday's poll adds a second sample |
| History-file timing, 2nd sample | Friday's rows: `Last-Modified` **VIX3M 18:31 ET, VIX 21:21 ET** (Thursday's: 18:31 / 21:11). The same pattern, still after the 17:25 `regime` job |
| FRED over the weekend | Still at **2026-10-08** on Sunday (`last_updated` Friday 09:38 ET); no weekend update. Friday's value is expected Monday around 09:38 ET. **Monday is Columbus Day** (a federal holiday; markets and CBOE open), so FRED may publish a day late. If it does, re-check with `fred_lag` on Tuesday before counting it as FRED's normal lag |

### Evening watch (Monday 2026-10-12, `poll`)

Polled every 2 minutes from 11:05 to 22:17 ET: 393 log entries, 2 transient connection errors (one CBOE timeout, one FRED connect error), both fine on the next poll.

| Source | Today's value first seen (ET) | Value | Notes |
|---|---|---|---|
| Delayed-quote JSON, VIX and VIX3M | **16:31:35** (the 16:29 poll still showed 16:13) | VIX **15.21**, VIX3M **17.93**, `last_trade_time` 16:15:01 | About 16 minutes behind all day. Unchanged after 16:31 for the rest of the evening |
| History file, VIX3M | **19:00** | 17.93 | `Last-Modified` 18:31 ET; the CDN kept serving the old file for 29 more minutes |
| History file, VIX | **21:33** | 15.21 | `Last-Modified` 21:11 ET; 22 more minutes on the CDN |
| FRED VIXCLS / VXVCLS | **Not by 22:17** | still 2026-10-09 | Friday's value appeared between Sunday 13:06 and Monday 11:05 ET. Columbus Day may have delayed Monday's; re-check Tuesday with `fred_lag` |

| Question | Answer |
|---|---|
| **When is the JSON final?** | **16:32 ET** on this sample (16:15 settlement + about 16 minutes of delay). A 17:00 `ingest` has about 28 minutes of margin |
| **JSON = history row?** | **Yes, 2 of 2 samples** (Friday 10-09 and Monday 10-12), both series, to the cent |
| **History-file timing, 3rd sample** | VIX3M 18:31, VIX 21:11 ET by `Last-Modified`, as on Thursday and Friday. Readers see it up to 30 minutes later (`s-maxage=900` + `stale-while-revalidate=900`) |
| `Last-Modified` as a change signal | **Not usable.** On the VIX file it flipped between two values every few polls all day (Sat 01:11 and 01:21 GMT) while the content stayed the same: the CDN serves two copies. The ingest must check the file's **last row date**, not the header |

## What it means for the design

- **README §5.1 / §5.2 (volatility pillar) as written, "CBOE primary, FRED fallback", doesn't work at 17:25:** CBOE's *history file* is late and FRED is a session behind. Without a change, the evening regime would score the volatility pillar on **yesterday's** VIX every day. The crisis override "VIX/VIX3M > 1.0 for ≥ 2 sessions" would effectively need 3.
- **Proposal:**
  1. Evening (`ingest` 17:00 / `regime` 17:25): read **today's close from the delayed-quote JSON**, accepting it only if `last_trade_time` is at or after 16:15 on session T (final by 16:32 ET on Monday's sample). Store it in `macro_series` with `fetched_at` and the source.
  2. Next evening: confirm it against the CBOE history row (same value expected) and log any difference.
  3. Fallbacks, in order: the CBOE history file (if it already has session T), then FRED (T−1). If only T−1 is available, the README's staleness rule applies (more than 2 sessions stale → the volatility pillar scores 0).
  4. `premarket_check` (07:30) reads the delayed-quote JSON too. CBOE also computes VIX in its extended (global) trading hours before the US open; whether and how the JSON shows those values before 09:30 was not checked (the snapshot was skipped; agenda #5 has `premarket_check` ignore overnight values).
- **Backtests:** use the CBOE history file (complete, identical to FRED), filtered to sessions.
- **Credit spread:** keep it only as LLM context (recent values), or drop it. It can't be part of any backtested rule.

## Monday 2026-10-12 run sheet

| When (ET) | Command | Answers |
|---|---|---|
| Skipped (optional; can be any pre-market) | `uv run --group spikes python -m spikes.macro snapshot` | What the delayed-quote JSON shows before the open (for `premarket_check`) |
| ✅ 11:05–22:17 | `uv run --group spikes python -m spikes.macro poll --until 22:15 --every 120` | When the final close appears in the JSON (expected ~16:30), in each history file, and on FRED; whether the JSON's final value equals the history row |

Done: the timing table is [above](#evening-watch-monday-2026-10-12-poll) and 0.3d is ticked.
