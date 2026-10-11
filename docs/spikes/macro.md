# Spike 0.3d: macro data timing (VIX, VIX3M, FRED)

**Run:** 2026-10-09, `uv run --group spikes python -m spikes.macro <snapshot|fred_lag|poll>`. Raw output in `spikes/macro/output/` (git-ignored).
**Status: PARTIAL.** The history checks are done. The evening timing watch (`poll`, 15:55–22:15 ET) was planned for 2026-10-09 but moved to **Monday 2026-10-12**, because the machine wasn't available that evening. 0.3d stays open until then.
**Decision so far: ADJUST** (proposed, for approval at 0.4). CBOE's history files appear only hours after the 17:25 regime job, and FRED a full session later. The evening regime should read VIX/VIX3M from **CBOE's delayed-quote JSON**, with the history file and FRED as fallbacks. Monday's watch confirms when that JSON carries the final 16:15 value.

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
| Delayed-quote JSON | Updates every few seconds (`s-maxage=5`), **15 minutes delayed** (at 09:57 ET it showed `last_trade_time` 09:42). Fields: `current_price`, `close`, `prev_day_close`, `open`, `high`, `low`, `last_trade_time`. VIX settles at 16:15, so the final value should be there around **16:30–16:35 ET**. **To confirm Monday** |
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

## What it means for the design

- **README §5.1 / §5.2 (volatility pillar) as written, "CBOE primary, FRED fallback", doesn't work at 17:25:** CBOE's *history file* is late and FRED is a session behind. Without a change, the evening regime would score the volatility pillar on **yesterday's** VIX every day. The crisis override "VIX/VIX3M > 1.0 for ≥ 2 sessions" would effectively need 3.
- **Proposal:**
  1. Evening (`ingest` 17:00 / `regime` 17:25): read **today's close from the delayed-quote JSON**, accepting it only if `last_trade_time` is at or after 16:15 on session T. Store it in `macro_series` with `fetched_at` and the source.
  2. Next evening: confirm it against the CBOE history row (same value expected) and log any difference.
  3. Fallbacks, in order: the CBOE history file (if it already has session T), then FRED (T−1). If only T−1 is available, the README's staleness rule applies (more than 2 sessions stale → the volatility pillar scores 0).
  4. `premarket_check` (07:30) reads the delayed-quote JSON too. CBOE also computes VIX in its extended (global) trading hours before the US open; whether and how the JSON shows those values before 09:30 is **to check Monday morning**.
- **Backtests:** use the CBOE history file (complete, identical to FRED), filtered to sessions.
- **Credit spread:** keep it only as LLM context (recent values), or drop it. It can't be part of any backtested rule.

## Monday 2026-10-12 run sheet

| When (ET) | Command | Answers |
|---|---|---|
| 07:30–09:30 (optional) | `uv run --group spikes python -m spikes.macro snapshot` | What the delayed-quote JSON shows before the open (for `premarket_check`) |
| 15:55–22:15 | `uv run --group spikes python -m spikes.macro poll --until 22:15 --every 120` | When the final close appears in the JSON (expected ~16:30), in each history file, and on FRED; whether the JSON's final value equals the history row |

Then this note gets the timing table and the final go/adjust, and 0.3d is ticked.
