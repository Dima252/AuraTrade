# Regime re-run on 5 pillars (task 3.8, step 3)

**Run:** 2026-10-24 15:05 UTC with `uv run python -m backtest.baseline regime`, risk.yaml `0020a669e66e`, 374 s. Generated; only the Reading at the end is written by hand. The 4-pillar run of task 2.4 is in [regime.md](regime.md).

## Setup

- **The tuning period only:** sessions 2016-01-04 to 2022-12-30. Nothing after 2022-12-30 is loaded; the holdout is opened once, after the thresholds are frozen (pre-registration step 5).
- **Leadership health** (README §5.2) from the breakout tracker over the historical full scan (every strategy on every point-in-time member from 2017; `backtest/history_scan.py`), by the live rules (#142). It scores from 10 complete breakouts in its window, so it starts in February 2017. The score runs from -5 to +5.
- **The loop:** the `EmergingLeader` scan reads the regime's `CORRECTION` spells, so each variant is iterated to its fixed point (`backtest/regime_search.py`). Iterations: 9 variant(s) in 2, 7 variant(s) in 3.
- **Pre-registered** (docs/backtests/baseline-preregistration.md): the 16 variants and 2.4's five targets; the choice is the variant passing the most, ties to the fewest switches a year, then the closest to IBD's values (+3, <= -1, 6 days). Moving the `CONFIRMED_UPTREND` threshold also moves the CU -> UP debounce (one below it) and the `no_ftd` fallback (at it), as 2.4's variants did.

## The tracker

| Year | Triggers | Per session (median) | Above the pivot 5 sessions later |
|---|---|---|---|
| 2017 | 9,690 | 34 | 55% |
| 2018 | 10,755 | 39 | 53% |
| 2019 | 12,123 | 44 | 53% |
| 2020 | 10,225 | 36 | 57% |
| 2021 | 9,649 | 32 | 52% |
| 2022 | 8,701 | 29 | 46% |

By strategy: `LEADER_PULLBACK` 39,834, `VCP_BREAKOUT` 5,289, `MOMENTUM_BREAKOUT` 524, `EPISODIC_PIVOT` 1,951, `EMERGING_LEADER` 13,545.

Leadership pillar over the reported sessions (cu+2_corr-2_c2_dist8): -1 8%, +0 51%, +1 42%.

## The search

| Variant | CORR | EU | UP | CU | Switches/yr (max) | Max/yr besides failed FTDs | False CORR | CU in 2017 | CU in 2019 | CU in H2 2020-2021 | CORR in Q4 2018 (SPY) | CORR in Feb-Mar 2020 (SPY) | CORR in 2022 (SPY) | 2020: EU / CU after | Failed EU |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `cu+2_corr-1_c1_dist6` | 31% | 21% | 8% | 40% | 17.5 (22) | 19 | 7 | 77% | 54% | 40% | 2018-10-05 (-1.9%) | 2020-02-24 (-4.7%) | 2022-01-10 (-2.6%) | 8 / 51 | 13 of 29 |
| `cu+2_corr-1_c1_dist8` | 25% | 20% | 9% | 46% | 17.2 (23) | 20 | 3 | 79% | 54% | 54% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 13 of 27 |
| `cu+2_corr-1_c2_dist6` | 25% | 20% | 14% | 42% | 18.0 (23) | 20 | 2 | 77% | 54% | 46% | 2018-10-08 (-2.0%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 12 of 26 |
| `cu+2_corr-1_c2_dist8` | 24% | 20% | 11% | 46% | 17.3 (21) | 20 | 1 | 79% | 54% | 54% | 2018-10-08 (-2.0%) | 2020-02-26 (-7.9%) | 2022-01-10 (-2.6%) | 8 / 51 | 12 of 26 |
| `cu+2_corr-2_c1_dist6` | 24% | 18% | 15% | 43% | 17.7 (22) | 18 | 2 | 77% | 54% | 51% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 12 of 24 |
| `cu+2_corr-2_c1_dist8` | 21% | 17% | 13% | 48% | 15.8 (19) | 16 | 1 | 86% | 54% | 60% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 13 of 23 |
| `cu+2_corr-2_c2_dist6` | 20% | 17% | 19% | 44% | 17.0 (22) | 18 | 1 | 84% | 54% | 51% | 2018-10-08 (-2.0%) | 2020-02-26 (-7.9%) | 2022-01-10 (-2.6%) | 8 / 51 | 12 of 22 |
| `cu+2_corr-2_c2_dist8` | 18% | 16% | 17% | 49% | 15.7 (20) | 14 | 0 | 86% | 54% | 61% | 2018-10-08 (-2.0%) | 2020-02-26 (-7.9%) | 2022-01-10 (-2.6%) | 8 / 51 | 12 of 21 |
| `cu+3_corr-1_c1_dist6` | 38% | 24% | 13% | 25% | 19.7 (24) | 22 | 8 | 35% | 45% | 29% | 2018-10-05 (-1.9%) | 2020-02-24 (-4.7%) | 2022-01-10 (-2.6%) | 8 / 51 | 17 of 32 |
| `cu+3_corr-1_c1_dist8` | 26% | 22% | 18% | 34% | 18.8 (22) | 20 | 4 | 67% | 45% | 38% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 13 of 26 |
| `cu+3_corr-1_c2_dist6` | 32% | 23% | 21% | 25% | 18.8 (21) | 18 | 5 | 35% | 45% | 28% | 2018-10-08 (-2.0%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 17 of 30 |
| `cu+3_corr-1_c2_dist8` | 24% | 21% | 20% | 34% | 18.5 (22) | 20 | 2 | 67% | 45% | 38% | 2018-10-08 (-2.0%) | 2020-02-26 (-7.9%) | 2022-01-10 (-2.6%) | 8 / 51 | 12 of 25 |
| `cu+3_corr-2_c1_dist6` | 30% | 21% | 24% | 24% | 18.5 (20) | 18 | 5 | 35% | 45% | 27% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 17 of 28 |
| `cu+3_corr-2_c1_dist8` | 22% | 19% | 24% | 35% | 18.5 (21) | 18 | 2 | 73% | 45% | 39% | 2018-10-05 (-1.9%) | 2020-02-25 (-7.6%) | 2022-01-10 (-2.6%) | 8 / 51 | 13 of 22 |
| `cu+3_corr-2_c2_dist6` | 21% | 19% | 32% | 29% | 18.2 (20) | 19 | 1 | 65% | 45% | 26% | 2018-10-08 (-2.0%) | 2020-02-26 (-7.9%) | 2022-01-10 (-2.6%) | 8 / 51 | 13 of 22 |
| `cu+3_corr-2_c2_dist8` | 20% | 18% | 27% | 35% | 17.8 (20) | 18 | 1 | 73% | 45% | 39% | 2018-10-08 (-2.0%) | 2020-02-26 (-7.9%) | 2022-01-10 (-2.6%) | 8 / 51 | 12 of 20 |
| `cu+2_corr-2_c2_dist8_4_pillars` | 19% | 18% | 19% | 44% | 16.3 (20) | 16 | 0 | 83% | 49% | 56% | 2018-10-08 (-2.0%) | 2020-02-26 (-7.9%) | 2022-01-10 (-2.6%) | 8 / 51 | 13 of 21 |

| Variant | declines early | 2020 recovery | switches <= 12/yr, failed FTDs apart | CU share >= target | false CORR <= 1/yr |
|---|---|---|---|---|---|
| `cu+2_corr-1_c1_dist6` | yes | yes | **no** | **no** | **no** |
| `cu+2_corr-1_c1_dist8` | yes | yes | **no** | **no** | yes |
| `cu+2_corr-1_c2_dist6` | yes | yes | **no** | **no** | yes |
| `cu+2_corr-1_c2_dist8` | yes | yes | **no** | **no** | yes |
| `cu+2_corr-2_c1_dist6` | yes | yes | **no** | **no** | yes |
| `cu+2_corr-2_c1_dist8` | yes | yes | **no** | **no** | yes |
| `cu+2_corr-2_c2_dist6` | yes | yes | **no** | **no** | yes |
| `cu+2_corr-2_c2_dist8` | yes | yes | **no** | **no** | yes |
| `cu+3_corr-1_c1_dist6` | yes | yes | **no** | **no** | **no** |
| `cu+3_corr-1_c1_dist8` | yes | yes | **no** | **no** | yes |
| `cu+3_corr-1_c2_dist6` | yes | yes | **no** | **no** | yes |
| `cu+3_corr-1_c2_dist8` | yes | yes | **no** | **no** | yes |
| `cu+3_corr-2_c1_dist6` | yes | yes | **no** | **no** | yes |
| `cu+3_corr-2_c1_dist8` | yes | yes | **no** | **no** | yes |
| `cu+3_corr-2_c2_dist6` | yes | yes | **no** | **no** | yes |
| `cu+3_corr-2_c2_dist8` | yes | yes | **no** | **no** | yes |
| `cu+2_corr-2_c2_dist8_4_pillars` | yes | yes | **no** | **no** | yes |

Variants:
- `cu+2_corr-1_c1_dist6`: `CONFIRMED_UPTREND` from +2, `CORRECTION` at <= -1 after 1 session(s), distribution -1 from 6 days.
- `cu+2_corr-1_c1_dist8`: `CONFIRMED_UPTREND` from +2, `CORRECTION` at <= -1 after 1 session(s), distribution -1 from 8 days.
- `cu+2_corr-1_c2_dist6`: `CONFIRMED_UPTREND` from +2, `CORRECTION` at <= -1 after 2 session(s), distribution -1 from 6 days.
- `cu+2_corr-1_c2_dist8`: `CONFIRMED_UPTREND` from +2, `CORRECTION` at <= -1 after 2 session(s), distribution -1 from 8 days.
- `cu+2_corr-2_c1_dist6`: `CONFIRMED_UPTREND` from +2, `CORRECTION` at <= -2 after 1 session(s), distribution -1 from 6 days.
- `cu+2_corr-2_c1_dist8`: `CONFIRMED_UPTREND` from +2, `CORRECTION` at <= -2 after 1 session(s), distribution -1 from 8 days.
- `cu+2_corr-2_c2_dist6`: `CONFIRMED_UPTREND` from +2, `CORRECTION` at <= -2 after 2 session(s), distribution -1 from 6 days.
- `cu+2_corr-2_c2_dist8`: `CONFIRMED_UPTREND` from +2, `CORRECTION` at <= -2 after 2 session(s), distribution -1 from 8 days.
- `cu+3_corr-1_c1_dist6`: `CONFIRMED_UPTREND` from +3, `CORRECTION` at <= -1 after 1 session(s), distribution -1 from 6 days.
- `cu+3_corr-1_c1_dist8`: `CONFIRMED_UPTREND` from +3, `CORRECTION` at <= -1 after 1 session(s), distribution -1 from 8 days.
- `cu+3_corr-1_c2_dist6`: `CONFIRMED_UPTREND` from +3, `CORRECTION` at <= -1 after 2 session(s), distribution -1 from 6 days.
- `cu+3_corr-1_c2_dist8`: `CONFIRMED_UPTREND` from +3, `CORRECTION` at <= -1 after 2 session(s), distribution -1 from 8 days.
- `cu+3_corr-2_c1_dist6`: `CONFIRMED_UPTREND` from +3, `CORRECTION` at <= -2 after 1 session(s), distribution -1 from 6 days.
- `cu+3_corr-2_c1_dist8`: `CONFIRMED_UPTREND` from +3, `CORRECTION` at <= -2 after 1 session(s), distribution -1 from 8 days.
- `cu+3_corr-2_c2_dist6`: `CONFIRMED_UPTREND` from +3, `CORRECTION` at <= -2 after 2 session(s), distribution -1 from 6 days.
- `cu+3_corr-2_c2_dist8`: `CONFIRMED_UPTREND` from +3, `CORRECTION` at <= -2 after 2 session(s), distribution -1 from 8 days.
- `cu+2_corr-2_c2_dist8_4_pillars`: `CONFIRMED_UPTREND` from +2, `CORRECTION` at <= -2 after 2 session(s), distribution -1 from 8 days; leadership at 0 (4 pillars).

**Chosen: `cu+2_corr-2_c2_dist8`** (`CONFIRMED_UPTREND` from +2, `CORRECTION` at <= -2 after 2 session(s), distribution -1 from 8 days): 3 of 5 targets, 15.7 switches a year.

## The chosen variant

### `cu+2_corr-2_c2_dist8` in detail

![Regime timeline over SPY](regime-timeline.svg)

**Sessions per regime and year**, with regime switches:

| Year | CORR | EU | UP | CU | Switches | Of failed FTDs |
|---|---|---|---|---|---|---|
| 2017 | 0 | 0 | 35 | 216 | 8 | 0 |
| 2018 | 88 | 47 | 18 | 98 | 20 | 6 |
| 2019 | 38 | 53 | 24 | 137 | 16 | 4 |
| 2020 | 36 | 48 | 54 | 115 | 16 | 2 |
| 2021 | 8 | 5 | 84 | 155 | 15 | 1 |
| 2022 | 109 | 93 | 37 | 12 | 19 | 11 |

**Switches by kind:** CU → UP 26, CORR → EU 21, UP → CU 16, EU → CORR 12, UP → CORR 10, EU → CU 9.

**SPY after each label** (price return from the label's close: mean / median / share positive / mean worst close over the horizon):

| Regime | 5 sessions | 20 sessions | 60 sessions |
|---|---|---|---|
| CORR | +0.1% / +0.6% / 59% / -2.2% (n 274) | +0.6% / +0.9% / 54% / -4.6% (n 274) | +3.3% / +3.8% / 67% / -7.3% (n 263) |
| EU | +0.4% / +0.4% / 57% / -1.4% (n 246) | +1.2% / +2.1% / 64% / -3.3% (n 246) | +2.5% / +3.4% / 62% / -6.0% (n 230) |
| UP | +0.5% / +0.8% / 63% / -1.1% (n 252) | +2.3% / +3.0% / 85% / -2.1% (n 237) | +5.2% / +6.1% / 88% / -2.6% (n 228) |
| CU | +0.1% / +0.3% / 62% / -0.8% (n 733) | +0.4% / +1.4% / 68% / -2.3% (n 733) | +1.2% / +3.1% / 71% / -5.0% (n 729) |

**Pillar scores** (share of sessions):

| Pillar | -1 | 0 | +1 |
|---|---|---|---|
| index_trend | 21% | 27% | 52% |
| breadth | 22% | 30% | 47% |
| volatility | 14% | 42% | 44% |
| distribution | 18% | 53% | 29% |
| leadership | 8% | 51% | 42% |

**Total score** (sessions), overall and inside each uptrend window:

| | -4 | -3 | -2 | -1 | +0 | +1 | +2 | +3 | +4 |
|---|---|---|---|---|---|---|---|---|---|
| all | 51 | 63 | 112 | 106 | 147 | 138 | 219 | 331 | 219 |
| 2017 | 0 | 0 | 0 | 2 | 13 | 14 | 26 | 74 | 89 |
| 2019 | 0 | 2 | 19 | 19 | 22 | 17 | 37 | 64 | 52 |
| H2 2020-2021 | 0 | 2 | 11 | 25 | 45 | 42 | 78 | 130 | 41 |

**Uptrend windows** (share of sessions):

| Window | CORR | EU | UP | CU |
|---|---|---|---|---|
| 2017 (2017-01-03 to 2017-12-29) | 0% | 0% | 14% | 86% |
| 2019 (2019-01-02 to 2019-12-31) | 15% | 21% | 10% | 54% |
| H2 2020-2021 (2020-07-01 to 2021-12-31) | 3% | 4% | 32% | 61% |

**Declines** (SPY's drawdown from the peak close at the first such label):

| Decline | Peak → low | First below CU | First CORRECTION | Early |
|---|---|---|---|---|
| Q4 2018 | 2018-09-20 → 2018-12-24 (-20.2%) | 2018-10-05 (-1.9%) | 2018-10-08 (-2.0%) | yes |
| Feb-Mar 2020 | 2020-02-19 → 2020-03-23 (-34.2%) | 2020-02-24 (-4.7%) | 2020-02-26 (-7.9%) | yes |
| 2022 | 2022-01-03 → 2022-10-12 (-25.4%) | 2022-01-03 (+0.0%) | 2022-01-10 (-2.6%) | yes |

**2020 recovery:** the first label above `CORRECTION` came 8 sessions after the 2020-03-23 low (SPY +13.1% from the low), the first `CONFIRMED_UPTREND` after 51 (SPY +39.8%).

**False `CORRECTION` spells** (more than 15 sessions, SPY never 8% below its prior 52-week high):

None.

**`EARLY_UPTREND` spells** (SPY over the sessions each one governed):

| From | To | Sessions | `no_ftd` | Then | SPY |
|---|---|---|---|---|---|
| 2018-02-23 | 2018-03-08 | 10 |  | `CONFIRMED_UPTREND` | +1.5% |
| 2018-04-10 | 2018-05-01 | 16 |  | `CORRECTION` | -0.7% |
| 2018-06-01 | 2018-06-14 | 10 |  | `CONFIRMED_UPTREND` | +1.2% |
| 2018-11-07 | 2018-11-19 | 9 |  | `CORRECTION` | -6.0% |
| 2018-12-03 | 2018-12-04 | 2 |  | `CORRECTION` | -3.4% |
| 2019-01-04 | 2019-01-28 | 16 |  | `CONFIRMED_UPTREND` | +4.4% |
| 2019-06-07 | 2019-06-20 | 10 |  | `CONFIRMED_UPTREND` | +2.2% |
| 2019-08-13 | 2019-08-30 | 14 |  | `CORRECTION` | -0.6% |
| 2019-09-19 | 2019-09-23 | 3 | yes | `CORRECTION` | -1.7% |
| 2019-10-11 | 2019-10-24 | 10 |  | `CONFIRMED_UPTREND` | +1.8% |
| 2020-04-02 | 2020-05-12 | 28 |  | `CORRECTION` | +11.7% |
| 2020-05-20 | 2020-06-03 | 10 |  | `CONFIRMED_UPTREND` | +4.8% |
| 2020-10-01 | 2020-10-14 | 10 |  | `CONFIRMED_UPTREND` | +3.1% |
| 2021-12-27 | 2022-01-07 | 10 |  | `CORRECTION` | -2.5% |
| 2022-02-09 | 2022-02-22 | 9 |  | `CORRECTION` | -7.8% |
| 2022-03-02 | 2022-03-11 | 8 |  | `CORRECTION` | -4.8% |
| 2022-03-18 | 2022-04-08 | 16 |  | `CORRECTION` | -1.0% |
| 2022-05-27 | 2022-06-10 | 10 |  | `CORRECTION` | -9.7% |
| 2022-06-24 | 2022-07-21 | 19 |  | `CORRECTION` | +1.3% |
| 2022-08-03 | 2022-08-16 | 10 |  | `CONFIRMED_UPTREND` | +2.9% |
| 2022-10-21 | 2022-11-11 | 16 |  | `CONFIRMED_UPTREND` | +5.6% |

The crisis override was active on 102 of 1510 sessions.

## Reading

*Written 2026-10-24.* By the pre-registered rule the choice is `cu+2_corr-2_c2_dist8`: `risk.yaml`'s provisional thresholds (#135) with `correction_2` on. It passes 3 of the 5 targets: declines are caught early, the 2020 recovery is picked up, and there are no false `CORRECTION` spells. No variant passes more than 3, and two misses are left:
- **Switches:** at most 14 in a year, besides failed follow-through days (target 12). It is the lowest of the 16 variants.
- **`CONFIRMED_UPTREND` coverage of 2019:** 54% (target 60%). 2017 is at 86% and H2 2020-2021 at 61%.

Leadership health helps a little against the same thresholds on 4 pillars: H2 2020-21 coverage rises from 56% to 61%, false spells fall from 1 to 0, and the worst year's switches from 16 to 14. The pillar scores +1 on 42% of sessions and -1 on 8%. **Not frozen in `risk.yaml` and the holdout not opened:** the baseline gate failed on the tuning period first (docs/backtests/baseline.md), so step 5 waits for the user.
