# Regime backtest (task 2.4)

**Run:** 2026-10-23 15:24 UTC with `uv run python backtest/regime.py`, risk.yaml `c3b091daf01f`, 36 s. Generated; only the Reading at the end is written by hand.

## Setup

- **The tuning period only:** sessions 2016-01-04 to 2022-12-30 (1762). Nothing after 2022-12-30 is loaded; the holdout (2023 on) is opened once, for the 5-pillar model, in the 3.8 re-run (README §15).
- **Warm-up:** the machine starts in `CORRECTION` on the second session, as a replay does; 2016 fills the 200-day and 252-session windows, so results are reported from 2017-01-03 (1510 sessions).
- **4 pillars:** leadership health needs the breakout tracker (3.7) and scores 0 throughout, so the score runs from -4 to +4.
- **Universe:** point in time (README §5.1): 3747 securities were in it at some session, 1365 of which no longer trade. Delisted names the vendor doesn't cover are missing; they were more often falling stocks, so breadth reads somewhat stronger than it was.
- **SPY returns** are price returns from split-adjusted closes (no dividends).

**Pre-registered** (set in the script before the first run on real data):
- declines: `CORRECTION` is reached while SPY's drawdown from the peak is under 50% of the decline's full drawdown;
- the 2020 recovery: `EARLY_UPTREND` within 15 sessions of the 2020-03-23 low and `CONFIRMED_UPTREND` within 60;
- at most 12 switches in any year, and at most 1 false `CORRECTION` spell a year on average;
- **`CONFIRMED_UPTREND` covers at least 60%** of each strong uptrend window: 2017 (2017-01-03 to 2017-12-29), 2019 (2019-01-02 to 2019-12-31), H2 2020-2021 (2020-07-01 to 2021-12-31).

## Comparisons

| Variant | CORR | EU | UP | CU | Switches/yr (max) | Max/yr besides failed FTDs | False CORR | CU in 2017 | CU in 2019 | CU in H2 2020-2021 | CORR in Q4 2018 (SPY) | CORR in Feb-Mar 2020 (SPY) | CORR in 2022 (SPY) | 2020: EU / CU after | Failed EU |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `baseline` | 22% | 19% | 15% | 44% | 16.8 (21) | 18 | 1 | 83% | 49% | 55% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 14 of 23 |
| `ibd_published` | 38% | 25% | 17% | 20% | 16.8 (20) | 15 | 7 | 34% | 36% | 17% | 2018-10-05 (-1.9%) | 2020-02-24 (-4.7%) | 2022-01-10 (-2.6%) | 8 / 82 | 16 of 29 |
| `exclude_expirations` | 21% | 18% | 16% | 44% | 16.5 (19) | 16 | 1 | 83% | 49% | 57% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 14 of 22 |
| `no_ftd_5` | 22% | 19% | 15% | 44% | 16.8 (21) | 18 | 1 | 83% | 49% | 55% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 14 of 23 |
| `no_ftd_15` | 22% | 19% | 15% | 44% | 16.5 (21) | 18 | 2 | 83% | 49% | 55% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 13 of 22 |
| `no_ftd_20` | 22% | 19% | 15% | 44% | 16.5 (21) | 18 | 2 | 83% | 49% | 55% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 13 of 22 |
| `no_eu_minimum` | 22% | 16% | 16% | 47% | 17.2 (22) | 20 | 1 | 83% | 52% | 59% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-20 (-6.5%) | 8 / 46 | 13 of 23 |
| `correction_2` | 19% | 18% | 19% | 44% | 16.3 (20) | 16 | 0 | 83% | 49% | 56% | 2018-10-08 (-2.0%) | 2020-02-26 (-7.9%) | 2022-01-10 (-2.6%) | 8 / 51 | 13 of 21 |
| `diag_correction_-2` | 35% | 24% | 22% | 18% | 16.2 (18) | 16 | 6 | 34% | 36% | 14% | 2018-10-05 (-1.9%) | 2020-02-24 (-4.7%) | 2022-01-10 (-2.6%) | 8 / 82 | 16 of 27 |
| `diag_distribution_8` | 34% | 25% | 19% | 22% | 17.3 (18) | 16 | 7 | 48% | 36% | 19% | 2018-10-05 (-1.9%) | 2020-02-24 (-4.7%) | 2022-01-10 (-2.6%) | 8 / 82 | 16 of 28 |
| `diag_vix_ratio_1.1` | 38% | 25% | 17% | 20% | 17.0 (20) | 16 | 7 | 34% | 36% | 17% | 2018-10-05 (-1.9%) | 2020-02-24 (-4.7%) | 2022-01-10 (-2.6%) | 8 / 82 | 16 of 29 |
| `diag_confirmed_+2` | 34% | 23% | 10% | 32% | 18.8 (23) | 21 | 7 | 58% | 48% | 36% | 2018-10-05 (-1.9%) | 2020-02-24 (-4.7%) | 2022-01-10 (-2.6%) | 8 / 51 | 15 of 30 |

| Variant | declines early | 2020 recovery | switches <= 12/yr, failed FTDs apart | CU share >= target | false CORR <= 1/yr |
|---|---|---|---|---|---|
| `baseline` | yes | yes | **no** | **no** | yes |
| `ibd_published` | yes | **no** | **no** | **no** | **no** |
| `exclude_expirations` | yes | yes | **no** | **no** | yes |
| `no_ftd_5` | yes | yes | **no** | **no** | yes |
| `no_ftd_15` | yes | yes | **no** | **no** | yes |
| `no_ftd_20` | yes | yes | **no** | **no** | yes |
| `no_eu_minimum` | yes | yes | **no** | **no** | yes |
| `correction_2` | yes | yes | **no** | **no** | yes |
| `diag_correction_-2` | yes | **no** | **no** | **no** | yes |
| `diag_distribution_8` | yes | **no** | **no** | **no** | **no** |
| `diag_vix_ratio_1.1` | yes | **no** | **no** | **no** | **no** |
| `diag_confirmed_+2` | yes | yes | **no** | **no** | **no** |

Variants:
- `baseline`: risk.yaml as committed: the provisional 4-pillar thresholds adopted after the first run (#135): `CORRECTION` at <= -2, distribution -1 from 8 days, `CONFIRMED_UPTREND` from +2.
- `ibd_published`: IBD's published thresholds (the first run's baseline): `CORRECTION` at <= -1, distribution -1 from 6 days, `CONFIRMED_UPTREND` from +3.
- `exclude_expirations`: distribution days on expiration / rebalance sessions not counted.
- `no_ftd_5`: `no_ftd` fallback after 5 sessions.
- `no_ftd_15`: `no_ftd` fallback after 15 sessions.
- `no_ftd_20`: `no_ftd` fallback after 20 sessions.
- `no_eu_minimum`: no 10-session minimum in `EARLY_UPTREND` (1: the 3-session upgrade bounds it).
- `correction_2`: `CORRECTION` after 2 sessions at its score (index trend at -1 still immediate).
- `diag_correction_-2`: diagnostic, on `ibd_published`: `CORRECTION` at <= -2.
- `diag_distribution_8`: diagnostic, on `ibd_published`: distribution -1 from 8 days.
- `diag_vix_ratio_1.1`: diagnostic, on `ibd_published`: volatility -1 from VIX/VIX3M > 1.1 (not 1.0).
- `diag_confirmed_+2`: diagnostic, on `ibd_published`: `CONFIRMED_UPTREND` and the `no_ftd` fallback from +2.

## Baseline

### `baseline` in detail

![Regime timeline over SPY](regime-timeline.svg)

**Sessions per regime and year**, with regime switches:

| Year | CORR | EU | UP | CU | Switches | Of failed FTDs |
|---|---|---|---|---|---|---|
| 2017 | 0 | 0 | 42 | 209 | 10 | 0 |
| 2018 | 94 | 48 | 20 | 89 | 19 | 6 |
| 2019 | 52 | 62 | 15 | 123 | 18 | 4 |
| 2020 | 40 | 48 | 68 | 97 | 16 | 2 |
| 2021 | 24 | 18 | 70 | 140 | 21 | 3 |
| 2022 | 123 | 108 | 17 | 3 | 17 | 13 |

**Switches by kind:** CU → UP 27, CORR → EU 23, UP → CU 18, EU → CORR 14, UP → CORR 9, EU → CU 9, CU → CORR 1.

**SPY after each label** (price return from the label's close: mean / median / share positive / mean worst close over the horizon):

| Regime | 5 sessions | 20 sessions | 60 sessions |
|---|---|---|---|
| CORR | +0.0% / +0.5% / 58% / -2.1% (n 328) | +0.9% / +1.4% / 59% / -4.4% (n 315) | +3.2% / +3.6% / 68% / -6.9% (n 304) |
| EU | +0.4% / +0.5% / 60% / -1.3% (n 284) | +1.1% / +2.0% / 64% / -3.1% (n 282) | +2.5% / +3.4% / 62% / -5.6% (n 253) |
| UP | +0.6% / +0.9% / 65% / -1.1% (n 232) | +1.3% / +2.6% / 79% / -2.6% (n 232) | +4.6% / +6.0% / 87% / -4.1% (n 232) |
| CU | +0.1% / +0.3% / 62% / -0.8% (n 661) | +0.6% / +1.5% / 69% / -2.1% (n 661) | +1.2% / +3.1% / 72% / -4.6% (n 661) |

**Pillar scores** (share of sessions):

| Pillar | -1 | 0 | +1 |
|---|---|---|---|
| index_trend | 21% | 27% | 52% |
| breadth | 22% | 30% | 47% |
| volatility | 14% | 42% | 44% |
| distribution | 17% | 52% | 31% |
| leadership | 0% | 100% | 0% |

**Total score** (sessions), overall and inside each uptrend window:

| | -4 | -3 | -2 | -1 | +0 | +1 | +2 | +3 | +4 |
|---|---|---|---|---|---|---|---|---|---|
| all | 66 | 76 | 99 | 149 | 157 | 177 | 329 | 304 | 153 |
| 2017 | 0 | 0 | 0 | 2 | 14 | 17 | 44 | 121 | 53 |
| 2019 | 2 | 2 | 10 | 26 | 25 | 34 | 41 | 67 | 45 |
| H2 2020-2021 | 0 | 0 | 6 | 37 | 38 | 50 | 160 | 67 | 22 |

**Uptrend windows** (share of sessions):

| Window | CORR | EU | UP | CU |
|---|---|---|---|---|
| 2017 (2017-01-03 to 2017-12-29) | 0% | 0% | 17% | 83% |
| 2019 (2019-01-02 to 2019-12-31) | 21% | 25% | 6% | 49% |
| H2 2020-2021 (2020-07-01 to 2021-12-31) | 8% | 7% | 29% | 55% |

**Declines** (SPY's drawdown from the peak close at the first such label):

| Decline | Peak → low | First below CU | First CORRECTION | Early |
|---|---|---|---|---|
| Q4 2018 | 2018-09-20 → 2018-12-24 (-20.2%) | 2018-10-05 (-1.9%) | 2018-10-05 (-1.9%) | yes |
| Feb-Mar 2020 | 2020-02-19 → 2020-03-23 (-34.2%) | 2020-02-24 (-4.7%) | 2020-02-25 (-7.6%) | yes |
| 2022 | 2022-01-03 → 2022-10-12 (-25.4%) | 2022-01-03 (+0.0%) | 2022-01-10 (-2.6%) | yes |

**2020 recovery:** the first label above `CORRECTION` came 8 sessions after the 2020-03-23 low (SPY +13.1% from the low), the first `CONFIRMED_UPTREND` after 51 (SPY +39.8%).

**False `CORRECTION` spells** (more than 15 sessions, SPY never 8% below its prior 52-week high):

| From | To | Sessions | Deepest close vs the 52-week high | Then |
|---|---|---|---|---|
| 2019-05-13 | 2019-06-06 | 18 | -6.6% | `EARLY_UPTREND` |

**`EARLY_UPTREND` spells** (SPY over the sessions each one governed):

| From | To | Sessions | `no_ftd` | Then | SPY |
|---|---|---|---|---|---|
| 2018-02-23 | 2018-03-09 | 11 |  | `CONFIRMED_UPTREND` | +1.4% |
| 2018-04-10 | 2018-05-01 | 16 |  | `CORRECTION` | -0.7% |
| 2018-06-01 | 2018-06-14 | 10 |  | `CONFIRMED_UPTREND` | +1.2% |
| 2018-11-07 | 2018-11-19 | 9 |  | `CORRECTION` | -6.0% |
| 2018-12-03 | 2018-12-04 | 2 |  | `CORRECTION` | -3.4% |
| 2019-01-04 | 2019-02-11 | 26 |  | `CONFIRMED_UPTREND` | +8.6% |
| 2019-06-07 | 2019-06-20 | 10 |  | `CONFIRMED_UPTREND` | +2.2% |
| 2019-08-13 | 2019-08-30 | 14 |  | `CORRECTION` | -0.6% |
| 2019-09-20 | 2019-09-23 | 2 | yes | `CORRECTION` | -0.7% |
| 2019-10-11 | 2019-10-24 | 10 |  | `CONFIRMED_UPTREND` | +1.8% |
| 2020-04-02 | 2020-05-12 | 28 |  | `CORRECTION` | +11.7% |
| 2020-05-20 | 2020-06-03 | 10 |  | `CONFIRMED_UPTREND` | +4.8% |
| 2020-10-01 | 2020-10-14 | 10 |  | `CONFIRMED_UPTREND` | +3.1% |
| 2021-10-14 | 2021-10-27 | 10 |  | `CONFIRMED_UPTREND` | +3.6% |
| 2021-12-15 | 2021-12-17 | 3 |  | `CORRECTION` | -3.3% |
| 2021-12-27 | 2022-01-07 | 10 |  | `CORRECTION` | -2.5% |
| 2022-02-09 | 2022-02-22 | 9 |  | `CORRECTION` | -7.8% |
| 2022-03-02 | 2022-03-11 | 8 |  | `CORRECTION` | -4.8% |
| 2022-03-18 | 2022-04-08 | 16 |  | `CORRECTION` | -1.0% |
| 2022-05-27 | 2022-06-10 | 10 |  | `CORRECTION` | -9.7% |
| 2022-06-24 | 2022-07-21 | 19 |  | `CORRECTION` | +1.3% |
| 2022-08-03 | 2022-08-16 | 10 |  | `CONFIRMED_UPTREND` | +2.9% |
| 2022-10-21 | 2022-12-05 | 31 |  | `CORRECTION` | +5.2% |

The crisis override was active on 102 of 1510 sessions.

## Reading

Written by hand on 2026-10-23. The first run failed; the user chose to adopt the diagnosed thresholds as provisional 4-pillar values (decision #135). This report is the second run, with them in risk.yaml as `baseline`; the first run's baseline is the `ibd_published` row.

**First run: IBD's published thresholds failed 4 of the 5 checks.** They turned defensive early in all three declines (`CORRECTION` within 2-5% of each peak) but didn't stay invested in uptrends. `CORRECTION` covered 38% of 2017-2022 and `CONFIRMED_UPTREND` 20%: 34% of 2017, 36% of 2019 and 17% of H2 2020-2021 (target 60%). There were 7 false `CORRECTION` spells in 6 years, `CONFIRMED_UPTREND` came 82 sessions after the 2020 low, and SPY did better at 60 sessions after `CORRECTION` labels than after `CONFIRMED_UPTREND` ones. None of the plan's comparisons changed this by more than a point or two.

**Why: one pillar at -1 was enough for `CORRECTION`, and only a follow-through day ends it.** Of the 14 downgrades to `CORRECTION` from `CONFIRMED_UPTREND` / `UNDER_PRESSURE`, 10 came at a score of exactly -1, and 6 had a single pillar at -1 with the rest at 0 (2017-04-12: only VIX/VIX3M briefly above 1.0; 2018-06-27, 2021-05-11, 2021-09-17: only distribution). Three causes:
- **Leadership health is missing** (0 throughout until the breakout tracker, 3.7). The thresholds were written for 5 pillars, and on 4 the score is one healthy pillar short.
- **The distribution pillar was at -1 on 36% of sessions.** Six distribution days in 25 sessions, on the higher of the SPY and QQQ counts, is common on ETF volume, which beats the prior session far more often than IBD's exchange volume does. Excluding expiration sessions didn't help.
- **The index-trend pillar is 0 whenever either index dips under its 50-day**, so in an ordinary pullback it stops offsetting the others.

Each diagnostic alone (rows `diag_*`) moved little; the volatility threshold made no difference at all.

**Adopted (#135, provisional until the 3.8 re-run): `CORRECTION` at <= -2, distribution -1 from 8 days, `CONFIRMED_UPTREND` and the `no_ftd` fallback from +2 (debounce at +1).** It passes three checks and misses two:
- *Passes:* all three declines are still caught early (`CORRECTION` on 2018-10-05 at -1.9%, 2020-02-25 at -7.6%, 2022-01-10 at -2.6%); the 2020 recovery (`EARLY_UPTREND` on the 2020-04-02 FTD, `CONFIRMED_UPTREND` after 51 sessions); one false `CORRECTION` (2019-05-13 to 2019-06-06, SPY -6.6% at worst, just short of the 8% line).
- *Misses:* `CONFIRMED_UPTREND` holds 83% of 2017 but only 49% of 2019 and 55% of H2 2020-2021 (target 60%); 2019 lost its May and August pullbacks to `CORRECTION` / `EARLY_UPTREND`, and H2 2020-2021 spent 29% in `UNDER_PRESSURE` at a typical score of +2. **And it still switches about 17 times a year** (max 21 in 2021).
- *The forward returns* now order sensibly at 20 sessions for `UNDER_PRESSURE` and `CONFIRMED_UPTREND` vs `CORRECTION`, but `UNDER_PRESSURE` labels are followed by the best 60-session returns (+4.6%): pullbacks inside uptrends, bought back. The regime is a risk throttle, not a return forecast.

**The switch criterion as written doesn't fit an FTD machine** (#135 counts failed follow-through days apart). Every failed FTD is two switches: `CORRECTION` → `EARLY_UPTREND` → `CORRECTION` (14 of 23 `EARLY_UPTREND` spells failed, 13 of the round-trip switches in 2022 alone). Set apart, the busiest year still has 18, mostly `CONFIRMED_UPTREND` ↔ `UNDER_PRESSURE` (27 and 18 switches): the score at the new +2 line flickers across it. That is for the 3.8 re-run, where leadership health lifts healthy scores off the line; a longer debounce is the other lever.

**Comparisons around the adopted set:** `correction_2` (2 sessions at <= -2 before `CORRECTION`, the index trend at -1 still immediate) is slightly better on everything: no false `CORRECTION`, 16 switches a year, `CORRECTION` reached at most a session later in the declines. `exclude_expirations` is marginally better; `no_ftd` lengths change nothing (the fallback barely fires); dropping the `EARLY_UPTREND` minimum adds `CONFIRMED_UPTREND` time but lets 2022 reach `CORRECTION` 7 sessions later (at -6.5%). None clearly beats the source default, so per README §15 the defaults stay; `correction_2` is the first candidate for the 3.8 re-run.

**Cost of the failed follow-through days** (under starter limits, §6): the 14 failed `EARLY_UPTREND` spells governed SPY moves of -9.7% to +11.7% (median -1.8%); 6 of them began in 2022. That is the price IBD's method accepts for catching the turn.

**Limits.** Four pillars; ~4-5 major episodes, so these thresholds are a fit to few events, and the targets were missed on two counts after the change; the delisted names the vendor misses make breadth read slightly strong (§17). Both the thresholds and the switch criterion are revisited on 5 pillars in the 3.8 re-run, which also opens the 2023+ holdout once.
