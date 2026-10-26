# Real-data smoke run (task 1.6)

**Run:** 2026-10-21 09:58 UTC with `uv run python scripts/smoke_real_data.py`, risk.yaml `2f1c0b60d582`. The modules built ahead of the data layer (2.1 indicators, 2.2 bases, 2.3 regime, 3.1 Trend Template, 3.5a plans) over the backfilled history. Nothing is tuned here.

## Inputs

- Sessions 2016-01-04 to 2026-10-16: 2713.
- Securities in the history by source: {'ACTIVE': 5362, 'ENUMERATED': 7408, 'INACTIVE': 902}.
- **Universe** (README §5.1, point in time): 4577 securities were in it at some session; 1504 of them (32.9%) no longer trade, and 1093 were found only by the ticker sweep (type assumed common).
- Universe size per session after the first 252 (no security has 252 bars before then): min 1575, median 1894, max 2261.
- With a CIK (earnings dates and fundamentals): 3070 of 4577 (67.1%).
- Securities with a missing session inside their history (a halt, under 31 sessions): 506, 22718 bars in all. Indicators count bars, so a window over a gap spans one session more.

## Checks

- **Exceptions:** 0.
- **NaN or infinite values** where the inputs were complete: 0.
- **Day moves of 50% or more** in a universe security's split-adjusted close: 3038. The largest are below with the raw close's move: equal moves are real trading (a crash or a squeeze); a split-adjusted move with a flat raw close would be a missed split.

| Symbol | Session | Close move | Raw close move |
|---|---|---|---|
| AXLA | 2023-08-29 | +299% | +299% |
| VANI | 2021-03-05 | +299% | +299% |
| ZYNE | 2023-08-14 | +298% | +298% |
| HYFM | 2026-08-03 | +298% | +298% |
| TENX | 2023-11-13 | +297% | +297% |
| AHG | 2020-08-13 | +297% | +297% |
| STI | 2025-10-13 | +294% | +294% |
| INBS | 2023-01-23 | +293% | +293% |
| FABC | 2020-05-29 | +292% | +292% |
| OCG | 2025-12-09 | +292% | +292% |
| EOCN | 2024-09-17 | +290% | +290% |
| EEIQ | 2021-03-26 | +288% | +288% |
| ALT | 2018-09-19 | +285% | +285% |
| RNAZ | 2023-09-25 | +284% | +284% |
| TNON | 2025-03-25 | +283% | +283% |

## Regime (2.3)

Sessions per regime and year (the state machine starts in CORRECTION on the second session; the first year is warm-up, as the 200-day average and the 252-session breadth windows fill). Leadership health scores 0 until the breakout tracker (3.7).

| Year | CORRECTION | EARLY_UPTREND | UNDER_PRESSURE | CONFIRMED_UPTREND |
|---|---|---|---|---|
| 2016 | 146 | 98 | 2 | 5 |
| 2017 | 124 | 13 | 28 | 86 |
| 2018 | 105 | 62 | 45 | 39 |
| 2019 | 58 | 74 | 30 | 90 |
| 2020 | 65 | 84 | 72 | 32 |
| 2021 | 93 | 41 | 69 | 49 |
| 2022 | 135 | 108 | 6 | 2 |
| 2023 | 79 | 66 | 49 | 56 |
| 2024 | 37 | 20 | 126 | 69 |
| 2025 | 72 | 60 | 92 | 26 |
| 2026 | 73 | 27 | 76 | 23 |

**The Feb-Mar 2020 crash** (2020-01-15 to 2020-06-30): unbroken stretches.

| From | To | Regime | Sessions |
|---|---|---|---|
| 2020-01-15 | 2020-01-24 | CONFIRMED_UPTREND | 7 |
| 2020-01-27 | 2020-02-21 | UNDER_PRESSURE | 19 |
| 2020-02-24 | 2020-04-01 | CORRECTION | 28 |
| 2020-04-02 | 2020-05-12 | EARLY_UPTREND | 28 |
| 2020-05-13 | 2020-05-19 | CORRECTION | 5 |
| 2020-05-20 | 2020-06-25 | EARLY_UPTREND | 26 |
| 2020-06-26 | 2020-06-30 | CORRECTION | 3 |

**The 2022 bear market** (2021-11-01 to 2023-03-31): unbroken stretches.

| From | To | Regime | Sessions |
|---|---|---|---|
| 2021-11-01 | 2021-11-22 | CONFIRMED_UPTREND | 16 |
| 2021-11-23 | 2021-11-29 | UNDER_PRESSURE | 4 |
| 2021-11-30 | 2021-12-14 | CORRECTION | 11 |
| 2021-12-15 | 2021-12-17 | EARLY_UPTREND | 3 |
| 2021-12-20 | 2021-12-23 | CORRECTION | 4 |
| 2021-12-27 | 2022-01-07 | EARLY_UPTREND | 10 |
| 2022-01-10 | 2022-02-08 | CORRECTION | 21 |
| 2022-02-09 | 2022-02-22 | EARLY_UPTREND | 9 |
| 2022-02-23 | 2022-03-01 | CORRECTION | 5 |
| 2022-03-02 | 2022-03-11 | EARLY_UPTREND | 8 |
| 2022-03-14 | 2022-03-17 | CORRECTION | 4 |
| 2022-03-18 | 2022-04-08 | EARLY_UPTREND | 16 |
| 2022-04-11 | 2022-05-26 | CORRECTION | 33 |
| 2022-05-27 | 2022-06-10 | EARLY_UPTREND | 10 |
| 2022-06-13 | 2022-06-23 | CORRECTION | 8 |
| 2022-06-24 | 2022-07-21 | EARLY_UPTREND | 19 |
| 2022-07-22 | 2022-08-02 | CORRECTION | 8 |
| 2022-08-03 | 2022-08-16 | EARLY_UPTREND | 10 |
| 2022-08-17 | 2022-08-18 | CONFIRMED_UPTREND | 2 |
| 2022-08-19 | 2022-08-26 | UNDER_PRESSURE | 6 |
| 2022-08-29 | 2022-10-20 | CORRECTION | 38 |
| 2022-10-21 | 2022-12-05 | EARLY_UPTREND | 31 |
| 2022-12-06 | 2023-01-05 | CORRECTION | 21 |
| 2023-01-06 | 2023-02-02 | EARLY_UPTREND | 19 |
| 2023-02-03 | 2023-02-09 | CONFIRMED_UPTREND | 5 |
| 2023-02-10 | 2023-02-27 | UNDER_PRESSURE | 11 |
| 2023-02-28 | 2023-03-28 | CORRECTION | 21 |
| 2023-03-29 | 2023-03-31 | EARLY_UPTREND | 3 |

## Trend Template (3.1)

Universe members passing all 8 rules per session, by year (RS rating = the percentile of the RS score within that session's universe):

| Year | Min | Median | Max |
|---|---|---|---|
| 2016 | 0 | 0 | 0 |
| 2017 | 235 | 392 | 462 |
| 2018 | 23 | 394 | 483 |
| 2019 | 44 | 320 | 443 |
| 2020 | 11 | 309 | 494 |
| 2021 | 189 | 417 | 537 |
| 2022 | 23 | 173 | 443 |
| 2023 | 94 | 311 | 466 |
| 2024 | 221 | 430 | 492 |
| 2025 | 34 | 336 | 497 |
| 2026 | 249 | 413 | 519 |

Share of member-sessions passing each rule: 1: 55.1%, 2: 60.6%, 3: 61.4%, 4: 55.1%, 5: 55.2%, 6: 57.1%, 7: 70.6%, 8: 30.0%.

## Bases and the base count (2.2)

- Breakouts: 70041 (89.2% with a reliable count); failed candidates: 15646.

| Year | Stage 1 | 2 | 3 | 4+ |
|---|---|---|---|---|
| 2016 | 5518 | 850 | 67 | 2 |
| 2017 | 4722 | 1553 | 475 | 107 |
| 2018 | 3925 | 1231 | 368 | 229 |
| 2019 | 5691 | 863 | 192 | 82 |
| 2020 | 5400 | 1320 | 200 | 46 |
| 2021 | 3662 | 2131 | 871 | 259 |
| 2022 | 4582 | 672 | 257 | 141 |
| 2023 | 5052 | 1280 | 293 | 81 |
| 2024 | 4652 | 1437 | 472 | 196 |
| 2025 | 4591 | 1250 | 321 | 163 |
| 2026 | 3186 | 1121 | 386 | 144 |

Trend Template passers by the stage of their last breakout, over all sessions: 1: 50.8%, 2: 31.3%, 3: 11.1%, 4+: 4.7%, none: 2.1%.

Stage 5 and above (outliers to explain): 399. The highest:

| Stage | Symbol | Breakout | Count reliable |
|---|---|---|---|
| 9 | HRTG | 2026-04-24 | yes |
| 9 | HRTG | 2026-02-26 | yes |
| 9 | BELFA | 2024-12-02 | yes |
| 9 | BELFA | 2024-09-13 | yes |
| 8 | TEAM | 2021-02-02 | no |
| 8 | HRTG | 2025-08-12 | yes |
| 8 | BELFA | 2024-05-08 | yes |
| 8 | AGI | 2026-04-08 | yes |
| 7 | XYZ | 2018-05-16 | no |
| 7 | TRHC | 2018-12-11 | yes |
| 7 | TNK | 2024-04-26 | yes |
| 7 | TEAM | 2020-11-20 | no |
| 7 | TEAM | 2020-10-01 | no |
| 7 | SKE | 2026-04-10 | yes |
| 7 | PBI | 2025-09-08 | yes |

## Position plans (3.5a): sample trades

The first Trend Template breakout of each year, entered at the breakout close, stop at the base low capped at 8%, default VCP plan, run through `plans.resolve` for up to 60 sessions. A check that the engine runs on real bars, not a result.

| Symbol | Entry day | Entry | Stop | Exit | Sessions | R | Plan versions |
|---|---|---|---|---|---|---|---|
| BAP | 2017-01-03 | 161.49 | 148.57 | TRAIL | 43 | 0.11 | 1 |
| TTWO | 2018-01-02 | 112.87 | 103.84 | HARD_STOP | 26 | -1.0 | 1 |
| CIEN | 2019-01-04 | 34.56 | 31.8 | TRAIL | 49 | 1.65 | 2 |
| GE | 2020-01-02 | 95.44 | 87.8 | TRAIL | 13 | -0.59 | 1 |
| SLP | 2021-01-04 | 75.06 | 69.06 | HARD_STOP | 31 | 0.0 | 2 |
| MPC | 2022-01-03 | 65.66 | 60.41 | TRAIL | 49 | 1.66 | 2 |
| JEF | 2023-01-04 | 38.68 | 35.59 | TRAIL | 33 | -0.33 | 1 |
| PBR | 2024-01-03 | 16.58 | 15.25 | TRAIL | 39 | -0.05 | 1 |
| PRAX | 2025-01-02 | 79.52 | 73.16 | TRAIL | 4 | -0.92 | 1 |

Run time (s): load 44.5, indicators 28.5, rs 11.0, trend_template 71.7, bases 8.2, regime 9.3, plans 0.1

## Reading

Written by hand on 2026-10-21, after the run above (EDGAR history complete). Nothing here is tuned; misses go to the task named.

**Checks pass.** No exceptions and no NaN or infinite values over 2,713 sessions and 4,577 universe securities. The largest one-day moves have equal split-adjusted and raw moves, so they are real trading (biotech squeezes and the like), not missed splits.

**Earnings and fundamentals coverage.** 3,070 of the 4,577 universe securities have a CIK (67.1%). The gap is entirely delisted names, as decision #130 expects: of the 2,163 members on the last session, 2,160 have a CIK (the other 3, NBN, TOWN and HIFS, are banks that file with the FDIC, not the SEC), while none of the 1,504 securities that no longer trade has one. Backtests (2.4, 3.8) must report that delisted names carry no earnings blackout and no fundamentals. Of the 131,839 historical earnings events, the first earnings headline times 71% in 2016, rising steadily to 92% in 2026 (73, 76, 76, 78, 82, 84, 85, 87, 91% in 2017-2025); the rest are timed by the 8-K acceptance alone (#119).

**Regime (2.3), against known stretches:**
- *The 2020 crash* reads right: `UNDER_PRESSURE` from 2020-01-27, `CORRECTION` from 2020-02-24 (the first day of the crash), and `EARLY_UPTREND` on 2020-04-02, the follow-through day IBD also called.
- *The 2022 bear market* reads right in direction: `CORRECTION` from 2022-01-10 and only 8 sessions above `EARLY_UPTREND` in 2022. But the regime flips between `CORRECTION` and `EARLY_UPTREND` on each failed rally, 108 `EARLY_UPTREND` sessions in all, and that state allows starter positions. For 2.4: how much starter risk those failed FTDs would have cost.
- *A clear miss: 2017.* The regime sat in `CORRECTION` from 2017-04-12 to 2017-10-11 (124 of the 127 sessions; the other 3 are explained below) in one of the calmest uptrends on record. The score was +3 or +4 on 54 of those 127 sessions; what held it there was the exit rule. The `no_ftd` fallback did fire on 2017-06-06 after 10 sessions at ≥ +3, with QQQ's rally low at 138.78 (its lowest low of those 10 sessions). On 2017-06-09, the tech-stock selloff took QQQ to 138.11 while SPY closed flat; that undercut ended the uptrend after 3 sessions, with the score still at +4. After that the score never held ≥ +3 for 10 straight sessions again (the longest run was 9), and no follow-through day came until 2017-10-12 in a market with few 1.25% up days. Logged under task 2.4 (false `CORRECTION` spells); candidates to compare there are a rally low for the fallback taken from the lowest close or with a cushion, and the fallback's length.
- *Also for 2.4:* 126 `UNDER_PRESSURE` sessions in 2024, a strong but narrow year. Leadership health scores 0 until the breakout tracker (3.7), so the regime stays provisional.

**Trend Template (3.1).** 2016 has no passers because the RS rating needs 252 sessions of history (warm-up). After that the median pass count sits at 310-430 in up years and drops to 173 in 2022; the lows fall on known bottoms: 11 on 2020-03-16, 23 on 2018-12-24 and 23 on 2022-06-17. Rule 8 (RS rating ≥ 70) passes 30% of member-sessions, as a percentile rule should. Plausible.

**Bases and the base count (2.2).** 89% of breakouts have a reliable count; the rest start before the history does, with no listing date to anchor them. The stage mix is plausible: half of the Trend Template passers are on a stage-1 base and 16% on stage 3 or later. The stage 5+ outliers are real multi-year leaders, not bad ticks: HRTG rose from about $2.80 to $35 over 2023-2026 and BELFA from about $30 to $260, and each new base came at least 20% above the prior pivot (`new_stage_advance`) without undercutting the prior base low, so IBD's rules keep counting. Some HRTG bases are 41-48% deep, just inside `max_depth` 0.50. Both counts reset when a base finally undercuts (BELFA in December 2024, HRTG in April 2026). At stage 4 and above the system takes no entry, so the exact number above 4 changes nothing. TEAM and XYZ show the unreliable case: a stage 7-8 count accumulated from a history that begins mid-advance. No bad tick reaches the count: the 4× level-jump split and ingest's outlier filter (#129, #130) catch them first.

**Position plans (3.5a).** The engine runs on real bars: the sample trades exit by the trail or the hard stop, losses are capped at -1R, and plan versions advance as the levels tighten. These are a smoke check, not a result.

**Module-to-storage mismatches:** none found in this run. The two found earlier (renamed tickers repeating their successor's bars, and 4× level jumps) were fixed in the history build (#130) before this run.
