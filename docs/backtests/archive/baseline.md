# Baseline backtest (task 3.8)

**Run:** 2026-10-24 19:33 UTC with `uv run python -m backtest.baseline pool`, risk.yaml `c53f8e4553f6`, regime `cu+2_corr-2_c2_dist8` (docs/backtests/regime-5-pillar.md), 1881 s. Generated; only the Reading at the end is written by hand. Method: docs/backtests/baseline-preregistration.md.

**The tuning period only (2017-2022).** The holdout (2023 on) is not loaded. The gate's second half, a positive holdout estimate, is checked once, after the regime and the defaults are frozen (step 5).

## The pool

Every unit with a rank in a session's capped list, attempted on each following session while listed, until it fills (`backtest/pool.py`). Attempts by outcome:

| Strategy | REJECTED | NOT_REACHED | NO_MINUTES | SKIPPED | MISSED | CLOSED | OPEN |
|---|---|---|---|---|---|---|---|
| `VCP_BREAKOUT` | 5,459 | 4,843 | 0 | 221 | 143 | 1,026 | 12 |
| `MOMENTUM_BREAKOUT` | 560 | 1,335 | 5 | 34 | 13 | 130 | 0 |
| `EPISODIC_PIVOT` | 605 | 0 | 0 | 462 | 277 | 625 | 1 |
| `LEADER_PULLBACK` | 6,059 | 2,041 | 15 | 574 | 213 | 1,513 | 25 |
| `EMERGING_LEADER` | 4,965 | 1,101 | 0 | 300 | 109 | 758 | 7 |

**EPs at 09:36 (#143):** 4,372 gaps of 10% or more at the open; 2,347 traded at least 25% of their prior average daily volume from the open to the entry, 37 had no minute bars. Their features, price and ATR are read at the prior close. Known residual: of the 1,970 EP listings, 111 were chosen under a gap-session regime that differs from the evening before's (the guardrails use the evening before's).

## The baseline gate (tuning period)

Closed trades only (a trade still open on 2022-12-30 is reported apart). R is the P&L over the dollars put at risk; the interval resamples whole entry sessions (2,000 draws, seed 38, 90%, nearest rank). **Pass:** the interval's low end above zero.

| Pool | Trades | Sessions | Expectancy (R) | 90% interval | Tuning half | Win rate | Avg gain (R) | Avg loss (R) |
|---|---|---|---|---|---|---|---|---|
| **All** | 4,052 | 990 | -0.11 | -0.18 to -0.04 | fails | 29% | +1.95 | -0.93 |
| `VCP_BREAKOUT` | 1,026 | 472 | -0.03 | -0.14 to +0.07 | fails | 33% | +1.81 | -0.93 |
| `MOMENTUM_BREAKOUT` | 130 | 109 | -0.51 | -0.68 to -0.30 | fails | 19% | +1.77 | -1.05 |
| `EPISODIC_PIVOT` | 625 | 408 | +0.21 | -0.09 to +0.55 | fails | 27% | +3.61 | -1.06 |
| `LEADER_PULLBACK` | 1,513 | 656 | -0.21 | -0.30 to -0.12 | fails | 26% | +2.03 | -1.01 |
| `EMERGING_LEADER` | 758 | 161 | -0.21 | -0.27 to -0.14 | fails | 30% | +0.80 | -0.65 |

**Survivors only** (the live rule: a security without report dates is rejected by the earnings blackout, so almost every delisted name is out): -0.08 over 2,888 trades (-0.16 to +0.00).

Open at the end: `VCP_BREAKOUT` 12, `MOMENTUM_BREAKOUT` 0, `EPISODIC_PIVOT` 1, `LEADER_PULLBACK` 25, `EMERGING_LEADER` 7.

## Per year

Expectancy (R) and closed trades by entry year.

| Strategy | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|
| **All** | +0.09 (1052) | -0.41 (648) | -0.07 (833) | +0.30 (509) | -0.31 (650) | -0.45 (360) |
| `VCP_BREAKOUT` | +0.27 (404) | -0.40 (161) | +0.07 (191) | -0.09 (112) | -0.53 (150) | -0.62 (8) |
| `MOMENTUM_BREAKOUT` | -0.76 (34) | -0.69 (22) | -0.37 (18) | -0.27 (23) | -0.30 (31) | -1.15 (2) |
| `EPISODIC_PIVOT` | +0.24 (106) | -0.29 (62) | +0.21 (76) | +0.86 (132) | +0.09 (186) | -0.35 (63) |
| `LEADER_PULLBACK` | -0.04 (508) | -0.63 (189) | -0.30 (271) | +0.26 (223) | -0.42 (253) | -0.82 (69) |
| `EMERGING_LEADER` | - (0) | -0.24 (214) | -0.01 (277) | -0.18 (19) | -0.83 (30) | -0.34 (218) |

Total R by year (all strategies): 2017 +92.2, 2018 -267.1, 2019 -60.4, 2020 +153.1, 2021 -204.6, 2022 -161.2.

## By regime

Expectancy (R) and closed trades by the regime computed the evening before the entry.

| Strategy | `CONFIRMED_UPTREND` | `UNDER_PRESSURE` | `EARLY_UPTREND` | `CORRECTION` |
|---|---|---|---|---|
| `VCP_BREAKOUT` | -0.05 (1003) | +0.93 (23) | - (0) | - (0) |
| `MOMENTUM_BREAKOUT` | -0.51 (130) | - (0) | - (0) | - (0) |
| `EPISODIC_PIVOT` | +0.01 (525) | -1.00 (9) | +1.46 (91) | - (0) |
| `LEADER_PULLBACK` | -0.23 (1348) | -0.05 (165) | - (0) | - (0) |
| `EMERGING_LEADER` | - (0) | - (0) | -0.21 (758) | - (0) |

## Comparisons (tuning period only)

Each variant changes one dimension against the reference (the defaults; for exits each strategy's single-trader set) and re-plays the pool (`backtest/comparisons.py`). The difference is the variant's expectancy minus the reference's, on the strategies it changes; its 90% interval resamples whole entry sessions of both. **Beyond noise** = the interval excludes zero. Nothing is adopted here: adoption also needs the same sign on the holdout, which stays closed while the pool fails its gate. Cells under 30 trades keep the default.

| # | Variant | Strategies | Reference (R) | Variant (R) | Difference | 90% interval | Beyond noise |
|---|---|---|---|---|---|---|---|
| 2 | one tranche instead of two | together | -0.16 (3297) | -0.13 (3297) | +0.03 | +0.02 to +0.04 | **better** |
| 2 | one tranche instead of two | `VCP_BREAKOUT` | -0.03 (1026) | -0.01 (1026) | +0.02 | +0.01 to +0.03 | **better** |
| 2 | one tranche instead of two | `LEADER_PULLBACK` | -0.21 (1513) | -0.17 (1513) | +0.05 | +0.03 to +0.06 | **better** |
| 2 | one tranche instead of two | `EMERGING_LEADER` | -0.21 (758) | -0.20 (758) | +0.01 | -0.00 to +0.02 | no |
| 3 | every rule of the three sets | together | -0.11 (4052) | -0.11 (4067) | +0.01 | -0.04 to +0.05 | no |
| 3 | every rule of the three sets | `VCP_BREAKOUT` | -0.03 (1026) | -0.09 (1029) | -0.06 | -0.13 to +0.00 | no |
| 3 | every rule of the three sets | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.22 (130) | +0.28 | +0.11 to +0.50 | **better** |
| 3 | every rule of the three sets | `EPISODIC_PIVOT` | +0.21 (625) | +0.19 (625) | -0.02 | -0.27 to +0.18 | no |
| 3 | every rule of the three sets | `LEADER_PULLBACK` | -0.21 (1513) | -0.18 (1525) | +0.03 | -0.02 to +0.09 | no |
| 3 | every rule of the three sets | `EMERGING_LEADER` | -0.21 (758) | -0.20 (758) | +0.01 | -0.02 to +0.04 | no |
| 3 | the hard stop and the trail only | together | -0.11 (4052) | -0.13 (4035) | -0.02 | -0.07 to +0.03 | no |
| 3 | the hard stop and the trail only | `VCP_BREAKOUT` | -0.03 (1026) | -0.10 (1026) | -0.07 | -0.13 to -0.00 | **worse** |
| 3 | the hard stop and the trail only | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.57 (130) | -0.06 | -0.12 to -0.02 | **worse** |
| 3 | the hard stop and the trail only | `EPISODIC_PIVOT` | +0.21 (625) | +0.18 (625) | -0.02 | -0.06 to +0.02 | no |
| 3 | the hard stop and the trail only | `LEADER_PULLBACK` | -0.21 (1513) | -0.19 (1507) | +0.02 | -0.07 to +0.11 | no |
| 3 | the hard stop and the trail only | `EMERGING_LEADER` | -0.21 (758) | -0.24 (747) | -0.03 | -0.16 to +0.10 | no |
| 4 | the round-trip rule added | together | -0.09 (3294) | -0.09 (3294) | -0.01 | -0.02 to +0.00 | no |
| 4 | the round-trip rule added | `VCP_BREAKOUT` | -0.03 (1026) | -0.04 (1026) | -0.00 | -0.01 to +0.00 | no |
| 4 | the round-trip rule added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.52 (130) | -0.02 | -0.09 to +0.04 | no |
| 4 | the round-trip rule added | `EPISODIC_PIVOT` | +0.21 (625) | +0.21 (625) | +0.00 | -0.04 to +0.04 | no |
| 4 | the round-trip rule added | `LEADER_PULLBACK` | -0.21 (1513) | -0.22 (1513) | -0.01 | -0.02 to +0.00 | no |
| 4 | breakeven at +3R added | together | -0.06 (1513) | -0.06 (1513) | -0.00 | -0.02 to +0.01 | no |
| 4 | breakeven at +3R added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.47 (130) | +0.03 | +0.01 to +0.06 | **better** |
| 4 | breakeven at +3R added | `EPISODIC_PIVOT` | +0.21 (625) | +0.20 (625) | -0.01 | -0.05 to +0.03 | no |
| 4 | breakeven at +3R added | `EMERGING_LEADER` | -0.21 (758) | -0.21 (758) | +0.00 | +0.00 to +0.00 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | together | -0.11 (4052) | -0.11 (4055) | -0.00 | -0.01 to +0.01 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | `VCP_BREAKOUT` | -0.03 (1026) | -0.04 (1027) | -0.01 | -0.03 to +0.01 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | `EPISODIC_PIVOT` | +0.21 (625) | +0.20 (625) | -0.00 | -0.01 to +0.00 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | `LEADER_PULLBACK` | -0.21 (1513) | -0.20 (1515) | +0.01 | +0.00 to +0.02 | **better** |
| 4 | the time stop (half at 10 sessions, out at 20) added | `EMERGING_LEADER` | -0.21 (758) | -0.22 (758) | -0.01 | -0.02 to +0.00 | no |
| 4 | the heavy-volume-reversal exit added | together | -0.09 (3294) | -0.09 (3297) | +0.00 | -0.01 to +0.01 | no |
| 4 | the heavy-volume-reversal exit added | `VCP_BREAKOUT` | -0.03 (1026) | -0.03 (1027) | +0.00 | -0.01 to +0.02 | no |
| 4 | the heavy-volume-reversal exit added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.50 (130) | +0.00 | +0.00 to +0.01 | no |
| 4 | the heavy-volume-reversal exit added | `EPISODIC_PIVOT` | +0.21 (625) | +0.20 (625) | -0.00 | -0.03 to +0.02 | no |
| 4 | the heavy-volume-reversal exit added | `LEADER_PULLBACK` | -0.21 (1513) | -0.21 (1515) | +0.00 | -0.01 to +0.01 | no |
| 4 | climax sales added | together | -0.09 (3294) | -0.08 (3307) | +0.00 | -0.05 to +0.05 | no |
| 4 | climax sales added | `VCP_BREAKOUT` | -0.03 (1026) | -0.10 (1029) | -0.07 | -0.13 to -0.00 | **worse** |
| 4 | climax sales added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.22 (130) | +0.28 | +0.14 to +0.48 | **better** |
| 4 | climax sales added | `EPISODIC_PIVOT` | +0.21 (625) | +0.22 (625) | +0.01 | -0.24 to +0.19 | no |
| 4 | climax sales added | `LEADER_PULLBACK` | -0.21 (1513) | -0.19 (1523) | +0.02 | -0.03 to +0.08 | no |
| 4 | the 8-week hold added | together | -0.09 (3294) | -0.11 (3294) | -0.02 | -0.04 to +0.00 | no |
| 4 | the 8-week hold added | `VCP_BREAKOUT` | -0.03 (1026) | -0.03 (1026) | +0.00 | +0.00 to +0.00 | no |
| 4 | the 8-week hold added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.53 (130) | -0.03 | -0.14 to +0.09 | no |
| 4 | the 8-week hold added | `EPISODIC_PIVOT` | +0.21 (625) | +0.13 (625) | -0.07 | -0.18 to +0.02 | no |
| 4 | the 8-week hold added | `LEADER_PULLBACK` | -0.21 (1513) | -0.22 (1513) | -0.00 | -0.01 to -0.00 | **worse** |
| 5 | the earnings cushion off | together | -0.11 (4052) | -0.11 (4052) | -0.00 | -0.01 to +0.01 | no |
| 5 | the earnings cushion off | `VCP_BREAKOUT` | -0.03 (1026) | -0.05 (1026) | -0.02 | -0.05 to +0.01 | no |
| 5 | the earnings cushion off | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 5 | the earnings cushion off | `EPISODIC_PIVOT` | +0.21 (625) | +0.20 (625) | -0.00 | -0.01 to +0.00 | no |
| 5 | the earnings cushion off | `LEADER_PULLBACK` | -0.21 (1513) | -0.20 (1513) | +0.01 | -0.00 to +0.03 | no |
| 5 | the earnings cushion off | `EMERGING_LEADER` | -0.21 (758) | -0.20 (758) | +0.00 | -0.01 to +0.02 | no |
| 5 | regime tightening off | together | -0.11 (4052) | -0.10 (4042) | +0.01 | -0.02 to +0.03 | no |
| 5 | regime tightening off | `VCP_BREAKOUT` | -0.03 (1026) | -0.06 (1026) | -0.03 | -0.06 to +0.00 | no |
| 5 | regime tightening off | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 5 | regime tightening off | `EPISODIC_PIVOT` | +0.21 (625) | +0.21 (625) | +0.00 | +0.00 to +0.00 | no |
| 5 | regime tightening off | `LEADER_PULLBACK` | -0.21 (1513) | -0.18 (1510) | +0.04 | -0.02 to +0.10 | no |
| 5 | regime tightening off | `EMERGING_LEADER` | -0.21 (758) | -0.20 (751) | +0.01 | -0.04 to +0.06 | no |
| 6 | Kullamägi's partial | together | -0.11 (4052) | -0.12 (4052) | -0.01 | -0.02 to -0.00 | **worse** |
| 6 | Kullamägi's partial | `VCP_BREAKOUT` | -0.03 (1026) | -0.02 (1026) | +0.01 | -0.01 to +0.03 | no |
| 6 | Kullamägi's partial | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 6 | Kullamägi's partial | `EPISODIC_PIVOT` | +0.21 (625) | +0.21 (625) | +0.00 | +0.00 to +0.00 | no |
| 6 | Kullamägi's partial | `LEADER_PULLBACK` | -0.21 (1513) | -0.25 (1513) | -0.04 | -0.06 to -0.02 | **worse** |
| 6 | Kullamägi's partial | `EMERGING_LEADER` | -0.21 (758) | -0.20 (758) | +0.01 | -0.02 to +0.04 | no |
| 6 | O'Neil's partial | together | -0.11 (4052) | -0.12 (4052) | -0.01 | -0.02 to +0.00 | no |
| 6 | O'Neil's partial | `VCP_BREAKOUT` | -0.03 (1026) | -0.02 (1026) | +0.01 | -0.01 to +0.03 | no |
| 6 | O'Neil's partial | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.56 (130) | -0.06 | -0.11 to -0.01 | **worse** |
| 6 | O'Neil's partial | `EPISODIC_PIVOT` | +0.21 (625) | +0.20 (625) | -0.00 | -0.03 to +0.03 | no |
| 6 | O'Neil's partial | `LEADER_PULLBACK` | -0.21 (1513) | -0.24 (1513) | -0.03 | -0.05 to -0.01 | **worse** |
| 6 | O'Neil's partial | `EMERGING_LEADER` | -0.21 (758) | -0.20 (758) | +0.01 | -0.02 to +0.04 | no |
| 6 | Minervini's partial | together | -0.11 (4052) | -0.11 (4052) | -0.00 | -0.01 to +0.01 | no |
| 6 | Minervini's partial | `VCP_BREAKOUT` | -0.03 (1026) | -0.03 (1026) | +0.00 | +0.00 to +0.00 | no |
| 6 | Minervini's partial | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.52 (130) | -0.01 | -0.13 to +0.08 | no |
| 6 | Minervini's partial | `EPISODIC_PIVOT` | +0.21 (625) | +0.20 (625) | -0.01 | -0.05 to +0.04 | no |
| 6 | Minervini's partial | `LEADER_PULLBACK` | -0.21 (1513) | -0.21 (1513) | +0.00 | +0.00 to +0.00 | no |
| 6 | Minervini's partial | `EMERGING_LEADER` | -0.21 (758) | -0.20 (758) | +0.01 | -0.03 to +0.03 | no |
| 6 | no partial | together | -0.11 (4052) | -0.13 (4052) | -0.02 | -0.03 to -0.00 | **worse** |
| 6 | no partial | `VCP_BREAKOUT` | -0.03 (1026) | -0.02 (1026) | +0.01 | -0.01 to +0.03 | no |
| 6 | no partial | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.57 (130) | -0.06 | -0.12 to -0.02 | **worse** |
| 6 | no partial | `EPISODIC_PIVOT` | +0.21 (625) | +0.19 (625) | -0.02 | -0.05 to +0.02 | no |
| 6 | no partial | `LEADER_PULLBACK` | -0.21 (1513) | -0.25 (1513) | -0.04 | -0.06 to -0.02 | **worse** |
| 6 | no partial | `EMERGING_LEADER` | -0.21 (758) | -0.21 (758) | +0.00 | +0.00 to +0.00 | no |
| 7 | the 10-day trail | together | -0.11 (4052) | -0.12 (4080) | -0.01 | -0.04 to +0.02 | no |
| 7 | the 10-day trail | `VCP_BREAKOUT` | -0.03 (1026) | -0.09 (1032) | -0.06 | -0.13 to +0.02 | no |
| 7 | the 10-day trail | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 7 | the 10-day trail | `EPISODIC_PIVOT` | +0.21 (625) | +0.21 (625) | +0.00 | +0.00 to +0.00 | no |
| 7 | the 10-day trail | `LEADER_PULLBACK` | -0.21 (1513) | -0.19 (1532) | +0.02 | -0.05 to +0.09 | no |
| 7 | the 10-day trail | `EMERGING_LEADER` | -0.21 (758) | -0.22 (761) | -0.01 | -0.05 to +0.03 | no |
| 7 | the 21-day EMA trail | together | -0.11 (4052) | -0.12 (4066) | -0.01 | -0.04 to +0.01 | no |
| 7 | the 21-day EMA trail | `VCP_BREAKOUT` | -0.03 (1026) | -0.09 (1030) | -0.06 | -0.12 to +0.00 | no |
| 7 | the 21-day EMA trail | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.49 (130) | +0.02 | -0.11 to +0.15 | no |
| 7 | the 21-day EMA trail | `EPISODIC_PIVOT` | +0.21 (625) | +0.18 (625) | -0.02 | -0.09 to +0.05 | no |
| 7 | the 21-day EMA trail | `LEADER_PULLBACK` | -0.21 (1513) | -0.19 (1521) | +0.02 | -0.03 to +0.06 | no |
| 7 | the 21-day EMA trail | `EMERGING_LEADER` | -0.21 (758) | -0.22 (760) | -0.02 | -0.04 to +0.01 | no |
| 7 | the 50-day trail | together | -0.11 (4052) | -0.12 (4051) | -0.01 | -0.03 to +0.00 | no |
| 7 | the 50-day trail | `VCP_BREAKOUT` | -0.03 (1026) | -0.03 (1026) | +0.00 | +0.00 to +0.00 | no |
| 7 | the 50-day trail | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.60 (130) | -0.09 | -0.21 to +0.03 | no |
| 7 | the 50-day trail | `EPISODIC_PIVOT` | +0.21 (625) | +0.14 (624) | -0.07 | -0.18 to +0.03 | no |
| 7 | the 50-day trail | `LEADER_PULLBACK` | -0.21 (1513) | -0.21 (1513) | +0.00 | +0.00 to +0.00 | no |
| 7 | the 50-day trail | `EMERGING_LEADER` | -0.21 (758) | -0.21 (758) | +0.00 | +0.00 to +0.00 | no |
| 8 | no base-count filter | together | -0.11 (4052) | -0.10 (4346) | +0.01 | -0.02 to +0.03 | no |
| 8 | no base-count filter | `VCP_BREAKOUT` | -0.03 (1026) | -0.03 (1096) | +0.01 | -0.02 to +0.03 | no |
| 8 | no base-count filter | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.40 (162) | +0.11 | -0.05 to +0.37 | no |
| 8 | no base-count filter | `EPISODIC_PIVOT` | +0.21 (625) | +0.23 (675) | +0.02 | -0.10 to +0.13 | no |
| 8 | no base-count filter | `LEADER_PULLBACK` | -0.21 (1513) | -0.21 (1580) | +0.01 | -0.01 to +0.02 | no |
| 8 | no base-count filter | `EMERGING_LEADER` | -0.21 (758) | -0.23 (833) | -0.02 | -0.04 to -0.00 | **worse** |
| 13 | `UNDER_PRESSURE` VCPs and pullbacks: all, not the top quarter | together | -0.14 (2539) | -0.13 (2914) | +0.01 | -0.02 to +0.04 | no |
| 13 | `UNDER_PRESSURE` VCPs and pullbacks: all, not the top quarter | `VCP_BREAKOUT` | -0.03 (1026) | -0.04 (1209) | -0.01 | -0.04 to +0.02 | no |
| 13 | `UNDER_PRESSURE` VCPs and pullbacks: all, not the top quarter | `LEADER_PULLBACK` | -0.21 (1513) | -0.19 (1705) | +0.02 | -0.02 to +0.07 | no |
| 16 | EP stop cap 1 x the pre-gap ADR | `EPISODIC_PIVOT` | +0.21 (625) | +0.25 (428) | +0.05 | -0.12 to +0.23 | no |
| 17 | `LeaderPullback` on the breakout gate | `LEADER_PULLBACK` | -0.21 (1513) | -0.19 (1513) | +0.02 | +0.01 to +0.04 | **better** |
| 18 | climax fast gain over >= 10 sessions | together | -0.11 (4052) | -0.11 (4052) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 10 sessions | `VCP_BREAKOUT` | -0.03 (1026) | -0.03 (1026) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 10 sessions | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 10 sessions | `EPISODIC_PIVOT` | +0.21 (625) | +0.21 (625) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 10 sessions | `LEADER_PULLBACK` | -0.21 (1513) | -0.21 (1513) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 10 sessions | `EMERGING_LEADER` | -0.21 (758) | -0.21 (758) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | together | -0.11 (4052) | -0.11 (4052) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | `VCP_BREAKOUT` | -0.03 (1026) | -0.03 (1026) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | `EPISODIC_PIVOT` | +0.21 (625) | +0.21 (625) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | `LEADER_PULLBACK` | -0.21 (1513) | -0.21 (1513) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | `EMERGING_LEADER` | -0.21 (758) | -0.21 (758) | +0.00 | +0.00 to +0.00 | no |

Not run: #1 (the code has no trigger that stays at the pivot on a gap open), #15 (the code exempts `MomentumBreakout` from the 50-day rule, so "with" needs a code switch), and the book-measured #9-#12, #14 and the book halves of #5 and #13 (the baseline book's portfolio simulation isn't built).

### #19: momentum results by consolidation depth

| Depth | Trades | Expectancy (R) |
|---|---|---|
| <= 15% | 49 | -0.60 |
| 15-25% | 48 | -0.59 |
| > 25% | 33 | -0.23 |

## Diagnostics

### Guardrail rejections

The rule that rejected each attempt (the first rejection ends the check), per strategy.

| Strategy | `BASE_COUNT` | `EARNINGS_BLACKOUT` | `ENTRIES_BLOCKED` | `FUNDABLE` | `PRICE_LEVELS` | `STOP_WIDTH` | `WHOLE_SHARES` |
|---|---|---|---|---|---|---|---|
| `VCP_BREAKOUT` | 413 | 555 | 193 | 2,132 | 5 | 2,097 | 64 |
| `MOMENTUM_BREAKOUT` | 357 | 91 | 31 | 0 | 15 | 0 | 66 |
| `EPISODIC_PIVOT` | 74 | 0 | 57 | 355 | 3 | 0 | 116 |
| `LEADER_PULLBACK` | 385 | 366 | 373 | 2,121 | 92 | 2,666 | 56 |
| `EMERGING_LEADER` | 129 | 242 | 501 | 0 | 3 | 3,632 | 458 |

### Whole shares at $5,000

| Strategy | Rejected at 0 shares | One tranche instead of two | 09:36 skips |
|---|---|---|---|
| `VCP_BREAKOUT` | 64 | 144 | INVALID_INPUT 168, STOP_WIDTH 35, WHOLE_SHARES 10, MAX_CHASE 7, PRICE_LEVELS 1 |
| `MOMENTUM_BREAKOUT` | 66 | 0 | STOP_WIDTH 24, INVALID_INPUT 4, MAX_CHASE 3, WHOLE_SHARES 3 |
| `EPISODIC_PIVOT` | 116 | 0 | STOP_WIDTH 330, WHOLE_SHARES 90, PRICE_LEVELS 34, INVALID_INPUT 8 |
| `LEADER_PULLBACK` | 56 | 82 | INVALID_INPUT 304, STOP_WIDTH 242, MAX_CHASE 13, WHOLE_SHARES 11, PRICE_LEVELS 4 |
| `EMERGING_LEADER` | 458 | 691 | WHOLE_SHARES 135, INVALID_INPUT 95, STOP_WIDTH 66, MAX_CHASE 3, PRICE_LEVELS 1 |

Placed at 09:36 but never filled (not triggered after the open, or past the limit): `VCP_BREAKOUT` 143 of 1181, `MOMENTUM_BREAKOUT` 13 of 143, `EPISODIC_PIVOT` 277 of 903, `LEADER_PULLBACK` 213 of 1751, `EMERGING_LEADER` 109 of 874.

## Reading

*Written 2026-10-24, after the third run: the EP proxy at 09:36 (#143). The data fixes (7e3fc62) and the comparisons are as in the second run; the first two runs' EP numbers looked ahead and are superseded (git history keeps them). Why the pool loses: [baseline-diagnostics.md](baseline-diagnostics.md). What happens next: [docs/strategy-decision.md](../strategy-decision.md).*

**The baseline gate fails on the tuning period.** Every strategy fails, and the pool overall fails clearly:
- **The pool:** -0.11R a trade (90% -0.18 to -0.04) over 4,052 closed trades.
- **Survivors only** (the live blackout rule, which drops almost every delisted name): -0.08R (-0.16 to +0.00). Adding the delisted names makes the pool worse, as survivorship bias predicts.
- **`EPISODIC_PIVOT`, fixed:** +0.21R (-0.09 to +0.55) over 625 trades: fails, by the rule fixed before the run (docs/backtests/baseline-diagnostics-preregistration.md, addendum). The point estimate barely moved from the look-ahead version's +0.23R; the interval widened. It is positive in 4 of 6 years, but 2020 (+0.86R over 132 trades) carries much of it, and it is about flat in `CONFIRMED_UPTREND` (+0.01R, 525 trades) against +1.46R in `EARLY_UPTREND` (91 trades).
- **`VCP_BREAKOUT`** is flat: -0.03R (-0.14 to +0.07).
- **`LEADER_PULLBACK`, `EMERGING_LEADER` and `MOMENTUM_BREAKOUT`** are clearly negative: -0.21R, -0.21R and -0.51R.
- **The holdout has not been opened**, and nothing has been adopted.

**The EPs at 09:36.** Of 4,372 gaps of 10% or more at the open, 2,347 traded at least 25% of their average daily volume in the first 6 minutes; 1,970 were listed. Most of the listed ones never became trades: 462 were skipped at 09:36, 330 of them because the low of the day sat more than 1.5 x the pre-gap ADR below the trigger, and 277 never filled. Of the EP listings, 111 were chosen under a gap-session regime that differs from the evening before's (the known residual).

**The comparisons** (diagnostics, not adoptions) say what the second run said: no single change turns the pool or a losing strategy positive.
- **Momentum** improves most with climax sales or every rule of the three sets on, and is still negative.
- **One tranche instead of two** is a little better (+0.03R together).
- **Partials help:** no partial, or Kullamägi's partial on pullbacks, is worse.
- **EP variants** (stop cap 1 x ADR, trails, the 8-week hold, climax sales) all stay within noise.

**Not run:** #1, #15 and the book-measured comparisons (see the table's note).

**What this says:** the losses come from the entries, not the exits; every exit and plan variation moves expectancy by hundredths of an R, while the pool needs about +0.15R to pass. The diagnostics add costs (+0.13R a trade, heaviest on the tight 09:36 stops) and early failures (39% stopped out within 5 sessions). EPs are the only strategy with a positive estimate, and it isn't distinguishable from zero. Phase 4 stays blocked until the user decides how the strategy changes ([docs/strategy-decision.md](../strategy-decision.md)).
