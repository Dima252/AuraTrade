# Baseline backtest (task 3.8)

**Run:** 2026-10-24 16:42 UTC with `uv run python -m backtest.baseline pool`, risk.yaml `0020a669e66e`, regime `cu+2_corr-2_c2_dist8` (docs/backtests/regime-5-pillar.md), 1551 s. Generated; only the Reading at the end is written by hand. Method: docs/backtests/baseline-preregistration.md.

**The tuning period only (2017-2022).** The holdout (2023 on) is not loaded. The gate's second half, a positive holdout estimate, is checked once, after the regime and the defaults are frozen (step 5).

## The pool

Every unit with a rank in a session's capped list, attempted on each following session while listed, until it fills (`backtest/pool.py`). Attempts by outcome:

| Strategy | REJECTED | NOT_REACHED | NO_MINUTES | SKIPPED | MISSED | CLOSED | OPEN |
|---|---|---|---|---|---|---|---|
| `VCP_BREAKOUT` | 5,449 | 4,830 | 0 | 221 | 142 | 1,023 | 12 |
| `MOMENTUM_BREAKOUT` | 560 | 1,335 | 5 | 34 | 13 | 130 | 0 |
| `EPISODIC_PIVOT` | 673 | 0 | 13 | 552 | 269 | 747 | 1 |
| `LEADER_PULLBACK` | 6,073 | 2,028 | 15 | 569 | 211 | 1,500 | 25 |
| `EMERGING_LEADER` | 4,923 | 1,095 | 0 | 299 | 109 | 754 | 7 |

## The baseline gate (tuning period)

Closed trades only (a trade still open on 2022-12-30 is reported apart). R is the P&L over the dollars put at risk; the interval resamples whole entry sessions (2,000 draws, seed 38, 90%, nearest rank). **Pass:** the interval's low end above zero.

| Pool | Trades | Sessions | Expectancy (R) | 90% interval | Tuning half | Win rate | Avg gain (R) | Avg loss (R) |
|---|---|---|---|---|---|---|---|---|
| **All** | 4,154 | 1,010 | -0.10 | -0.16 to -0.03 | fails | 29% | +1.98 | -0.94 |
| `VCP_BREAKOUT` | 1,023 | 471 | -0.03 | -0.14 to +0.07 | fails | 33% | +1.81 | -0.93 |
| `MOMENTUM_BREAKOUT` | 130 | 109 | -0.51 | -0.68 to -0.30 | fails | 19% | +1.77 | -1.05 |
| `EPISODIC_PIVOT` | 747 | 471 | +0.23 | -0.03 to +0.55 | fails | 28% | +3.51 | -1.07 |
| `LEADER_PULLBACK` | 1,500 | 652 | -0.22 | -0.31 to -0.14 | fails | 26% | +2.02 | -1.01 |
| `EMERGING_LEADER` | 754 | 161 | -0.21 | -0.28 to -0.14 | fails | 30% | +0.79 | -0.64 |

**Survivors only** (the live rule: a security without report dates is rejected by the earnings blackout, so almost every delisted name is out): -0.07 over 2,998 trades (-0.15 to +0.01).

Open at the end: `VCP_BREAKOUT` 12, `MOMENTUM_BREAKOUT` 0, `EPISODIC_PIVOT` 1, `LEADER_PULLBACK` 25, `EMERGING_LEADER` 7.

## Per year

Expectancy (R) and closed trades by entry year.

| Strategy | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|
| **All** | +0.08 (1060) | -0.41 (655) | -0.07 (852) | +0.30 (540) | -0.28 (675) | -0.41 (372) |
| `VCP_BREAKOUT` | +0.27 (402) | -0.40 (161) | +0.07 (191) | -0.09 (112) | -0.52 (149) | -0.62 (8) |
| `MOMENTUM_BREAKOUT` | -0.76 (34) | -0.69 (22) | -0.37 (18) | -0.27 (23) | -0.30 (31) | -1.15 (2) |
| `EPISODIC_PIVOT` | +0.19 (119) | -0.27 (71) | +0.13 (96) | +0.80 (165) | +0.17 (219) | -0.20 (77) |
| `LEADER_PULLBACK` | -0.04 (505) | -0.64 (187) | -0.29 (272) | +0.23 (221) | -0.45 (246) | -0.82 (69) |
| `EMERGING_LEADER` | - (0) | -0.24 (214) | -0.01 (275) | -0.18 (19) | -0.83 (30) | -0.35 (216) |

Total R by year (all strategies): 2017 +86.0, 2018 -268.7, 2019 -62.5, 2020 +163.7, 2021 -186.3, 2022 -154.1.

## By regime

Expectancy (R) and closed trades by the regime computed the evening before the entry.

| Strategy | `CONFIRMED_UPTREND` | `UNDER_PRESSURE` | `EARLY_UPTREND` | `CORRECTION` |
|---|---|---|---|---|
| `VCP_BREAKOUT` | -0.06 (1001) | +1.02 (22) | - (0) | - (0) |
| `MOMENTUM_BREAKOUT` | -0.51 (130) | - (0) | - (0) | - (0) |
| `EPISODIC_PIVOT` | +0.06 (592) | +0.43 (37) | +1.00 (118) | - (0) |
| `LEADER_PULLBACK` | -0.23 (1343) | -0.13 (157) | - (0) | - (0) |
| `EMERGING_LEADER` | - (0) | - (0) | -0.21 (754) | - (0) |

## Comparisons (tuning period only)

Each variant changes one dimension against the reference (the defaults; for exits each strategy's single-trader set) and re-plays the pool (`backtest/comparisons.py`). The difference is the variant's expectancy minus the reference's, on the strategies it changes; its 90% interval resamples whole entry sessions of both. **Beyond noise** = the interval excludes zero. Nothing is adopted here: adoption also needs the same sign on the holdout, which stays closed while the pool fails its gate. Cells under 30 trades keep the default.

| # | Variant | Strategies | Reference (R) | Variant (R) | Difference | 90% interval | Beyond noise |
|---|---|---|---|---|---|---|---|
| 2 | one tranche instead of two | together | -0.16 (3277) | -0.13 (3277) | +0.03 | +0.02 to +0.04 | **better** |
| 2 | one tranche instead of two | `VCP_BREAKOUT` | -0.03 (1023) | -0.02 (1023) | +0.02 | +0.01 to +0.03 | **better** |
| 2 | one tranche instead of two | `LEADER_PULLBACK` | -0.22 (1500) | -0.18 (1500) | +0.05 | +0.03 to +0.06 | **better** |
| 2 | one tranche instead of two | `EMERGING_LEADER` | -0.21 (754) | -0.20 (754) | +0.01 | -0.00 to +0.02 | no |
| 3 | every rule of the three sets | together | -0.10 (4154) | -0.08 (4168) | +0.02 | -0.03 to +0.07 | no |
| 3 | every rule of the three sets | `VCP_BREAKOUT` | -0.03 (1023) | -0.09 (1026) | -0.06 | -0.13 to +0.00 | no |
| 3 | every rule of the three sets | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.22 (130) | +0.28 | +0.11 to +0.50 | **better** |
| 3 | every rule of the three sets | `EPISODIC_PIVOT` | +0.23 (747) | +0.30 (746) | +0.07 | -0.19 to +0.27 | no |
| 3 | every rule of the three sets | `LEADER_PULLBACK` | -0.22 (1500) | -0.18 (1512) | +0.04 | -0.01 to +0.10 | no |
| 3 | every rule of the three sets | `EMERGING_LEADER` | -0.21 (754) | -0.20 (754) | +0.01 | -0.02 to +0.04 | no |
| 3 | the hard stop and the trail only | together | -0.10 (4154) | -0.13 (4138) | -0.02 | -0.07 to +0.02 | no |
| 3 | the hard stop and the trail only | `VCP_BREAKOUT` | -0.03 (1023) | -0.10 (1023) | -0.07 | -0.13 to -0.01 | **worse** |
| 3 | the hard stop and the trail only | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.57 (130) | -0.06 | -0.12 to -0.02 | **worse** |
| 3 | the hard stop and the trail only | `EPISODIC_PIVOT` | +0.23 (747) | +0.19 (747) | -0.03 | -0.07 to +0.00 | no |
| 3 | the hard stop and the trail only | `LEADER_PULLBACK` | -0.22 (1500) | -0.21 (1494) | +0.02 | -0.07 to +0.12 | no |
| 3 | the hard stop and the trail only | `EMERGING_LEADER` | -0.21 (754) | -0.24 (744) | -0.03 | -0.15 to +0.10 | no |
| 4 | the round-trip rule added | together | -0.08 (3400) | -0.08 (3400) | -0.00 | -0.01 to +0.01 | no |
| 4 | the round-trip rule added | `VCP_BREAKOUT` | -0.03 (1023) | -0.04 (1023) | -0.00 | -0.01 to +0.00 | no |
| 4 | the round-trip rule added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.52 (130) | -0.02 | -0.09 to +0.04 | no |
| 4 | the round-trip rule added | `EPISODIC_PIVOT` | +0.23 (747) | +0.24 (747) | +0.01 | -0.03 to +0.04 | no |
| 4 | the round-trip rule added | `LEADER_PULLBACK` | -0.22 (1500) | -0.23 (1500) | -0.00 | -0.02 to +0.01 | no |
| 4 | breakeven at +3R added | together | -0.03 (1631) | -0.03 (1631) | -0.00 | -0.02 to +0.01 | no |
| 4 | breakeven at +3R added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.47 (130) | +0.03 | +0.01 to +0.06 | **better** |
| 4 | breakeven at +3R added | `EPISODIC_PIVOT` | +0.23 (747) | +0.22 (747) | -0.01 | -0.04 to +0.02 | no |
| 4 | breakeven at +3R added | `EMERGING_LEADER` | -0.21 (754) | -0.21 (754) | +0.00 | +0.00 to +0.00 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | together | -0.10 (4154) | -0.10 (4157) | -0.00 | -0.01 to +0.00 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | `VCP_BREAKOUT` | -0.03 (1023) | -0.04 (1024) | -0.01 | -0.03 to +0.01 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | `EPISODIC_PIVOT` | +0.23 (747) | +0.22 (747) | -0.00 | -0.01 to +0.00 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | `LEADER_PULLBACK` | -0.22 (1500) | -0.22 (1502) | +0.01 | -0.00 to +0.02 | no |
| 4 | the time stop (half at 10 sessions, out at 20) added | `EMERGING_LEADER` | -0.21 (754) | -0.22 (754) | -0.01 | -0.02 to +0.00 | no |
| 4 | the heavy-volume-reversal exit added | together | -0.08 (3400) | -0.07 (3403) | +0.00 | -0.00 to +0.01 | no |
| 4 | the heavy-volume-reversal exit added | `VCP_BREAKOUT` | -0.03 (1023) | -0.03 (1024) | +0.00 | -0.01 to +0.02 | no |
| 4 | the heavy-volume-reversal exit added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.50 (130) | +0.00 | +0.00 to +0.01 | no |
| 4 | the heavy-volume-reversal exit added | `EPISODIC_PIVOT` | +0.23 (747) | +0.23 (747) | -0.00 | -0.02 to +0.01 | no |
| 4 | the heavy-volume-reversal exit added | `LEADER_PULLBACK` | -0.22 (1500) | -0.21 (1502) | +0.01 | -0.00 to +0.02 | no |
| 4 | climax sales added | together | -0.08 (3400) | -0.05 (3413) | +0.02 | -0.04 to +0.08 | no |
| 4 | climax sales added | `VCP_BREAKOUT` | -0.03 (1023) | -0.10 (1026) | -0.07 | -0.13 to -0.00 | **worse** |
| 4 | climax sales added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.22 (130) | +0.28 | +0.14 to +0.48 | **better** |
| 4 | climax sales added | `EPISODIC_PIVOT` | +0.23 (747) | +0.32 (747) | +0.09 | -0.15 to +0.27 | no |
| 4 | climax sales added | `LEADER_PULLBACK` | -0.22 (1500) | -0.19 (1510) | +0.03 | -0.02 to +0.09 | no |
| 4 | the 8-week hold added | together | -0.08 (3400) | -0.09 (3399) | -0.01 | -0.03 to +0.01 | no |
| 4 | the 8-week hold added | `VCP_BREAKOUT` | -0.03 (1023) | -0.03 (1023) | +0.00 | +0.00 to +0.00 | no |
| 4 | the 8-week hold added | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.53 (130) | -0.03 | -0.14 to +0.09 | no |
| 4 | the 8-week hold added | `EPISODIC_PIVOT` | +0.23 (747) | +0.18 (746) | -0.04 | -0.14 to +0.05 | no |
| 4 | the 8-week hold added | `LEADER_PULLBACK` | -0.22 (1500) | -0.23 (1500) | -0.00 | -0.01 to -0.00 | **worse** |
| 5 | the earnings cushion off | together | -0.10 (4154) | -0.10 (4154) | -0.00 | -0.01 to +0.01 | no |
| 5 | the earnings cushion off | `VCP_BREAKOUT` | -0.03 (1023) | -0.05 (1023) | -0.02 | -0.04 to +0.01 | no |
| 5 | the earnings cushion off | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 5 | the earnings cushion off | `EPISODIC_PIVOT` | +0.23 (747) | +0.22 (747) | -0.00 | -0.01 to +0.00 | no |
| 5 | the earnings cushion off | `LEADER_PULLBACK` | -0.22 (1500) | -0.21 (1500) | +0.01 | -0.01 to +0.03 | no |
| 5 | the earnings cushion off | `EMERGING_LEADER` | -0.21 (754) | -0.21 (754) | -0.00 | -0.01 to +0.01 | no |
| 5 | regime tightening off | together | -0.10 (4154) | -0.09 (4145) | +0.01 | -0.02 to +0.03 | no |
| 5 | regime tightening off | `VCP_BREAKOUT` | -0.03 (1023) | -0.06 (1023) | -0.03 | -0.06 to +0.00 | no |
| 5 | regime tightening off | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 5 | regime tightening off | `EPISODIC_PIVOT` | +0.23 (747) | +0.23 (747) | +0.00 | +0.00 to +0.00 | no |
| 5 | regime tightening off | `LEADER_PULLBACK` | -0.22 (1500) | -0.19 (1497) | +0.03 | -0.02 to +0.10 | no |
| 5 | regime tightening off | `EMERGING_LEADER` | -0.21 (754) | -0.20 (748) | +0.01 | -0.04 to +0.06 | no |
| 6 | Kullamägi's partial | together | -0.10 (4154) | -0.11 (4154) | -0.01 | -0.02 to -0.00 | **worse** |
| 6 | Kullamägi's partial | `VCP_BREAKOUT` | -0.03 (1023) | -0.02 (1023) | +0.01 | -0.01 to +0.03 | no |
| 6 | Kullamägi's partial | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 6 | Kullamägi's partial | `EPISODIC_PIVOT` | +0.23 (747) | +0.23 (747) | +0.00 | +0.00 to +0.00 | no |
| 6 | Kullamägi's partial | `LEADER_PULLBACK` | -0.22 (1500) | -0.27 (1500) | -0.04 | -0.06 to -0.02 | **worse** |
| 6 | Kullamägi's partial | `EMERGING_LEADER` | -0.21 (754) | -0.20 (754) | +0.01 | -0.02 to +0.04 | no |
| 6 | O'Neil's partial | together | -0.10 (4154) | -0.11 (4154) | -0.01 | -0.02 to +0.00 | no |
| 6 | O'Neil's partial | `VCP_BREAKOUT` | -0.03 (1023) | -0.03 (1023) | +0.01 | -0.01 to +0.03 | no |
| 6 | O'Neil's partial | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.56 (130) | -0.06 | -0.11 to -0.01 | **worse** |
| 6 | O'Neil's partial | `EPISODIC_PIVOT` | +0.23 (747) | +0.22 (747) | -0.01 | -0.03 to +0.02 | no |
| 6 | O'Neil's partial | `LEADER_PULLBACK` | -0.22 (1500) | -0.25 (1500) | -0.03 | -0.05 to -0.01 | **worse** |
| 6 | O'Neil's partial | `EMERGING_LEADER` | -0.21 (754) | -0.20 (754) | +0.01 | -0.02 to +0.04 | no |
| 6 | Minervini's partial | together | -0.10 (4154) | -0.10 (4154) | +0.00 | -0.01 to +0.01 | no |
| 6 | Minervini's partial | `VCP_BREAKOUT` | -0.03 (1023) | -0.03 (1023) | +0.00 | +0.00 to +0.00 | no |
| 6 | Minervini's partial | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.52 (130) | -0.01 | -0.13 to +0.08 | no |
| 6 | Minervini's partial | `EPISODIC_PIVOT` | +0.23 (747) | +0.23 (747) | +0.00 | -0.04 to +0.04 | no |
| 6 | Minervini's partial | `LEADER_PULLBACK` | -0.22 (1500) | -0.22 (1500) | +0.00 | +0.00 to +0.00 | no |
| 6 | Minervini's partial | `EMERGING_LEADER` | -0.21 (754) | -0.20 (754) | +0.01 | -0.03 to +0.04 | no |
| 6 | no partial | together | -0.10 (4154) | -0.12 (4154) | -0.02 | -0.03 to -0.01 | **worse** |
| 6 | no partial | `VCP_BREAKOUT` | -0.03 (1023) | -0.02 (1023) | +0.01 | -0.01 to +0.03 | no |
| 6 | no partial | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.57 (130) | -0.06 | -0.12 to -0.02 | **worse** |
| 6 | no partial | `EPISODIC_PIVOT` | +0.23 (747) | +0.20 (747) | -0.03 | -0.06 to +0.01 | no |
| 6 | no partial | `LEADER_PULLBACK` | -0.22 (1500) | -0.26 (1500) | -0.04 | -0.06 to -0.02 | **worse** |
| 6 | no partial | `EMERGING_LEADER` | -0.21 (754) | -0.21 (754) | +0.00 | +0.00 to +0.00 | no |
| 7 | the 10-day trail | together | -0.10 (4154) | -0.11 (4182) | -0.01 | -0.04 to +0.02 | no |
| 7 | the 10-day trail | `VCP_BREAKOUT` | -0.03 (1023) | -0.09 (1029) | -0.06 | -0.13 to +0.02 | no |
| 7 | the 10-day trail | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 7 | the 10-day trail | `EPISODIC_PIVOT` | +0.23 (747) | +0.23 (747) | +0.00 | +0.00 to +0.00 | no |
| 7 | the 10-day trail | `LEADER_PULLBACK` | -0.22 (1500) | -0.20 (1519) | +0.02 | -0.05 to +0.09 | no |
| 7 | the 10-day trail | `EMERGING_LEADER` | -0.21 (754) | -0.22 (757) | -0.01 | -0.05 to +0.03 | no |
| 7 | the 21-day EMA trail | together | -0.10 (4154) | -0.12 (4167) | -0.01 | -0.04 to +0.01 | no |
| 7 | the 21-day EMA trail | `VCP_BREAKOUT` | -0.03 (1023) | -0.09 (1027) | -0.05 | -0.11 to +0.00 | no |
| 7 | the 21-day EMA trail | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.49 (130) | +0.02 | -0.11 to +0.15 | no |
| 7 | the 21-day EMA trail | `EPISODIC_PIVOT` | +0.23 (747) | +0.19 (746) | -0.04 | -0.11 to +0.04 | no |
| 7 | the 21-day EMA trail | `LEADER_PULLBACK` | -0.22 (1500) | -0.20 (1508) | +0.02 | -0.02 to +0.07 | no |
| 7 | the 21-day EMA trail | `EMERGING_LEADER` | -0.21 (754) | -0.22 (756) | -0.01 | -0.04 to +0.01 | no |
| 7 | the 50-day trail | together | -0.10 (4154) | -0.10 (4153) | -0.00 | -0.02 to +0.02 | no |
| 7 | the 50-day trail | `VCP_BREAKOUT` | -0.03 (1023) | -0.03 (1023) | +0.00 | +0.00 to +0.00 | no |
| 7 | the 50-day trail | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.60 (130) | -0.09 | -0.21 to +0.03 | no |
| 7 | the 50-day trail | `EPISODIC_PIVOT` | +0.23 (747) | +0.22 (746) | -0.00 | -0.11 to +0.10 | no |
| 7 | the 50-day trail | `LEADER_PULLBACK` | -0.22 (1500) | -0.22 (1500) | +0.00 | +0.00 to +0.00 | no |
| 7 | the 50-day trail | `EMERGING_LEADER` | -0.21 (754) | -0.21 (754) | +0.00 | +0.00 to +0.00 | no |
| 8 | no base-count filter | together | -0.10 (4154) | -0.10 (4469) | +0.00 | -0.02 to +0.02 | no |
| 8 | no base-count filter | `VCP_BREAKOUT` | -0.03 (1023) | -0.03 (1093) | +0.01 | -0.02 to +0.03 | no |
| 8 | no base-count filter | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.40 (162) | +0.11 | -0.05 to +0.37 | no |
| 8 | no base-count filter | `EPISODIC_PIVOT` | +0.23 (747) | +0.23 (820) | +0.00 | -0.09 to +0.08 | no |
| 8 | no base-count filter | `LEADER_PULLBACK` | -0.22 (1500) | -0.22 (1567) | +0.00 | -0.01 to +0.02 | no |
| 8 | no base-count filter | `EMERGING_LEADER` | -0.21 (754) | -0.23 (827) | -0.02 | -0.03 to -0.00 | **worse** |
| 13 | `UNDER_PRESSURE` VCPs and pullbacks: all, not the top quarter | together | -0.15 (2523) | -0.13 (2904) | +0.02 | -0.01 to +0.05 | no |
| 13 | `UNDER_PRESSURE` VCPs and pullbacks: all, not the top quarter | `VCP_BREAKOUT` | -0.03 (1023) | -0.05 (1206) | -0.01 | -0.04 to +0.02 | no |
| 13 | `UNDER_PRESSURE` VCPs and pullbacks: all, not the top quarter | `LEADER_PULLBACK` | -0.22 (1500) | -0.19 (1698) | +0.03 | -0.01 to +0.08 | no |
| 16 | EP stop cap 1 x the pre-gap ADR | `EPISODIC_PIVOT` | +0.23 (747) | +0.26 (519) | +0.03 | -0.11 to +0.18 | no |
| 17 | `LeaderPullback` on the breakout gate | `LEADER_PULLBACK` | -0.22 (1500) | -0.20 (1500) | +0.02 | +0.01 to +0.03 | **better** |
| 18 | climax fast gain over >= 10 sessions | together | -0.10 (4154) | -0.10 (4154) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 10 sessions | `VCP_BREAKOUT` | -0.03 (1023) | -0.03 (1023) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 10 sessions | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 10 sessions | `EPISODIC_PIVOT` | +0.23 (747) | +0.23 (747) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 10 sessions | `LEADER_PULLBACK` | -0.22 (1500) | -0.22 (1500) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 10 sessions | `EMERGING_LEADER` | -0.21 (754) | -0.21 (754) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | together | -0.10 (4154) | -0.10 (4154) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | `VCP_BREAKOUT` | -0.03 (1023) | -0.03 (1023) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | `MOMENTUM_BREAKOUT` | -0.51 (130) | -0.51 (130) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | `EPISODIC_PIVOT` | +0.23 (747) | +0.23 (747) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | `LEADER_PULLBACK` | -0.22 (1500) | -0.22 (1500) | +0.00 | +0.00 to +0.00 | no |
| 18 | climax fast gain over >= 20 sessions | `EMERGING_LEADER` | -0.21 (754) | -0.21 (754) | +0.00 | +0.00 to +0.00 | no |

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
| `VCP_BREAKOUT` | 411 | 550 | 193 | 2,134 | 5 | 2,092 | 64 |
| `MOMENTUM_BREAKOUT` | 357 | 91 | 31 | 0 | 15 | 0 | 66 |
| `EPISODIC_PIVOT` | 117 | 0 | 65 | 349 | 6 | 0 | 136 |
| `LEADER_PULLBACK` | 386 | 366 | 375 | 2,147 | 92 | 2,652 | 55 |
| `EMERGING_LEADER` | 128 | 242 | 501 | 0 | 3 | 3,596 | 453 |

### Whole shares at $5,000

| Strategy | Rejected at 0 shares | One tranche instead of two | 09:36 skips |
|---|---|---|---|
| `VCP_BREAKOUT` | 64 | 143 | INVALID_INPUT 168, STOP_WIDTH 35, WHOLE_SHARES 10, MAX_CHASE 7, PRICE_LEVELS 1 |
| `MOMENTUM_BREAKOUT` | 66 | 0 | STOP_WIDTH 24, INVALID_INPUT 4, MAX_CHASE 3, WHOLE_SHARES 3 |
| `EPISODIC_PIVOT` | 136 | 0 | STOP_WIDTH 366, WHOLE_SHARES 114, PRICE_LEVELS 58, INVALID_INPUT 14 |
| `LEADER_PULLBACK` | 55 | 78 | INVALID_INPUT 302, STOP_WIDTH 241, MAX_CHASE 12, WHOLE_SHARES 11, PRICE_LEVELS 3 |
| `EMERGING_LEADER` | 453 | 684 | WHOLE_SHARES 135, INVALID_INPUT 95, STOP_WIDTH 65, MAX_CHASE 3, PRICE_LEVELS 1 |

Placed at 09:36 but never filled (not triggered after the open, or past the limit): `VCP_BREAKOUT` 142 of 1177, `MOMENTUM_BREAKOUT` 13 of 143, `EPISODIC_PIVOT` 269 of 1017, `LEADER_PULLBACK` 211 of 1736, `EMERGING_LEADER` 109 of 870.

## Reading

*Written 2026-10-24, after the second run. The data were fixed (commit 7e3fc62) before the comparisons ran.*

**The baseline gate fails on the tuning period.** Every strategy fails, and the pool overall fails clearly:
- **The pool:** -0.10R a trade, with a 90% interval of -0.16 to -0.03, over 4,154 closed trades.
- **Survivors only** (the live blackout rule, which drops almost every delisted name): -0.07R (-0.15 to +0.01). Adding the delisted names made the pool worse, as their survivorship bias predicts.
- **`EPISODIC_PIVOT`** no longer passes: +0.23R (-0.03 to +0.55), and its gap-session proxy still has a look-ahead.
- **`VCP_BREAKOUT`** is flat: -0.03R.
- **`LEADER_PULLBACK`, `EMERGING_LEADER` and `MOMENTUM_BREAKOUT`** are clearly negative: -0.22R, -0.21R and -0.51R.
- **The holdout has not been opened**, and nothing has been adopted.

**The two data fixes** (both flattered the first run):
- **Dead exchange-traded products** found by the ticker sweep were typed as common stock. 52 pieces were retyped and taken out of the universe (`history.securities.DELISTED_FUNDS`, backfill stage `funds`).
- **Securities without EDGAR report dates** (the delisted names) were all rejected by the earnings blackout. They are now read as having no report in the blackout, because live every ticker has an earnings calendar. The survivors-only figure above keeps the live rule.

**Comparisons (tuning only; diagnostics, not adoptions).** No single change turns the pool or any losing strategy positive. The largest effects:
- **`MOMENTUM_BREAKOUT`** improves by +0.28R with climax sales, or with every rule of the three sets on (-0.51 -> -0.22). It is still negative, and on only 130 trades.
- **One tranche instead of two** is a little better (+0.03R together; +0.05R for pullbacks).
- **The breakout gate on pullbacks** is +0.02R.
- **Partials help:** no partial is worse (-0.02R), and Kullamägi's or O'Neil's partials are worse for pullbacks than Minervini's.
- **The floor** (stop and trail only) is worse for VCPs (-0.07R).
- **Base count, trails and the time stop** make no difference beyond noise. Neither do the safety rules (the cushion and regime tightening), the `UNDER_PRESSURE` top quarter or the climax window. The base-count filter does help `EMERGING_LEADER` (removing it costs -0.02R).
- **#19:** momentum is negative at every depth (-0.60 / -0.59 / -0.23R for <= 15%, 15-25% and > 25%).

**Not run:** #1, #15 and the book-measured comparisons (see the table's note).

**What this says:** the losses come from the entries, not the exits. Every exit and plan variation moves expectancy by a few hundredths of an R, while the gap to a pass is about +0.15R for the pool. Each strategy wins 19-33% of the time, at about +1.8 to +2.0R against -0.9 to -1.0R (EPs +3.5R; `EMERGING_LEADER` only +0.8R against -0.6R). That is below breakeven after the fill model's slippage. It agrees with spike 0.3h, which found these setups about breakeven before costs. Phase 4 stays blocked until the user decides what to do next.
