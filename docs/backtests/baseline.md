# Baseline backtest (task 3.8)

**Run:** 2026-10-24 15:31 UTC with `uv run python -m backtest.baseline pool`, risk.yaml `0020a669e66e`, regime `cu+2_corr-2_c2_dist8` (docs/backtests/regime-5-pillar.md), 776 s. Generated; only the Reading at the end is written by hand. Method: docs/backtests/baseline-preregistration.md.

**The tuning period only (2017-2022).** The holdout (2023 on) is not loaded. The gate's second half, a positive holdout estimate, is checked once, after the regime and the defaults are frozen (step 5).

## The pool

Every unit with a rank in a session's capped list, attempted on each following session while listed, until it fills (`backtest/pool.py`). Attempts by outcome:

| Strategy | REJECTED | NOT_REACHED | NO_MINUTES | SKIPPED | MISSED | CLOSED | OPEN |
|---|---|---|---|---|---|---|---|
| `VCP_BREAKOUT` | 7,446 | 3,292 | 0 | 163 | 99 | 691 | 0 |
| `MOMENTUM_BREAKOUT` | 1,430 | 556 | 2 | 23 | 5 | 66 | 0 |
| `EPISODIC_PIVOT` | 686 | 0 | 13 | 551 | 269 | 745 | 1 |
| `LEADER_PULLBACK` | 11,794 | 1,240 | 6 | 372 | 125 | 935 | 4 |
| `EMERGING_LEADER` | 5,553 | 756 | 0 | 238 | 82 | 563 | 0 |

## The baseline gate (tuning period)

Closed trades only (a trade still open on 2022-12-30 is reported apart). R is the P&L over the dollars put at risk; the interval resamples whole entry sessions (2,000 draws, seed 38, 90%, nearest rank). **Pass:** the interval's low end above zero.

| Pool | Trades | Sessions | Expectancy (R) | 90% interval | Tuning half | Win rate | Avg gain (R) | Avg loss (R) |
|---|---|---|---|---|---|---|---|---|
| **All** | 3,000 | 940 | -0.03 | -0.12 to +0.08 | fails | 30% | +2.08 | -0.92 |
| `VCP_BREAKOUT` | 691 | 387 | -0.01 | -0.14 to +0.12 | fails | 34% | +1.65 | -0.87 |
| `MOMENTUM_BREAKOUT` | 66 | 60 | -0.57 | -0.77 to -0.36 | fails | 21% | +1.18 | -1.05 |
| `EPISODIC_PIVOT` | 745 | 473 | +0.39 | +0.07 to +0.75 | **passes** | 28% | +4.07 | -1.07 |
| `LEADER_PULLBACK` | 935 | 501 | -0.24 | -0.33 to -0.14 | fails | 26% | +1.91 | -0.99 |
| `EMERGING_LEADER` | 563 | 146 | -0.20 | -0.27 to -0.14 | fails | 33% | +0.68 | -0.64 |

Open at the end: `VCP_BREAKOUT` 0, `MOMENTUM_BREAKOUT` 0, `EPISODIC_PIVOT` 1, `LEADER_PULLBACK` 4, `EMERGING_LEADER` 0.

## Per year

Expectancy (R) and closed trades by entry year.

| Strategy | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 |
|---|---|---|---|---|---|---|
| **All** | +0.14 (689) | -0.32 (470) | -0.05 (649) | +0.58 (385) | -0.23 (501) | -0.38 (306) |
| `VCP_BREAKOUT` | +0.30 (257) | -0.35 (110) | +0.08 (146) | +0.01 (77) | -0.57 (96) | -0.50 (5) |
| `MOMENTUM_BREAKOUT` | -0.86 (13) | -0.75 (9) | -0.70 (11) | -0.03 (10) | -0.49 (22) | -1.04 (1) |
| `EPISODIC_PIVOT` | +0.19 (119) | +0.15 (70) | +0.12 (99) | +1.35 (164) | +0.18 (216) | -0.20 (77) |
| `LEADER_PULLBACK` | +0.03 (300) | -0.55 (126) | -0.23 (180) | +0.05 (122) | -0.50 (150) | -0.89 (57) |
| `EMERGING_LEADER` | - (0) | -0.28 (155) | -0.04 (213) | -0.27 (12) | -0.74 (17) | -0.28 (166) |

Total R by year (all strategies): 2017 +98.9, 2018 -148.3, 2019 -34.4, 2020 +224.5, 2021 -114.2, 2022 -116.5.

## By regime

Expectancy (R) and closed trades by the regime computed the evening before the entry.

| Strategy | `CONFIRMED_UPTREND` | `UNDER_PRESSURE` | `EARLY_UPTREND` | `CORRECTION` |
|---|---|---|---|---|
| `VCP_BREAKOUT` | -0.03 (677) | +1.14 (14) | - (0) | - (0) |
| `MOMENTUM_BREAKOUT` | -0.57 (66) | - (0) | - (0) | - (0) |
| `EPISODIC_PIVOT` | +0.27 (589) | +0.39 (38) | +1.00 (118) | - (0) |
| `LEADER_PULLBACK` | -0.23 (825) | -0.26 (110) | - (0) | - (0) |
| `EMERGING_LEADER` | - (0) | - (0) | -0.20 (563) | - (0) |

## Diagnostics

### Guardrail rejections

The rule that rejected each attempt (the first rejection ends the check), per strategy.

| Strategy | `BASE_COUNT` | `EARNINGS_BLACKOUT` | `ENTRIES_BLOCKED` | `FUNDABLE` | `PRICE_LEVELS` | `STOP_WIDTH` | `WHOLE_SHARES` |
|---|---|---|---|---|---|---|---|
| `VCP_BREAKOUT` | 408 | 2,510 | 194 | 2,195 | 5 | 2,078 | 56 |
| `MOMENTUM_BREAKOUT` | 355 | 987 | 31 | 0 | 15 | 0 | 42 |
| `EPISODIC_PIVOT` | 114 | 0 | 65 | 364 | 6 | 0 | 137 |
| `LEADER_PULLBACK` | 420 | 4,007 | 506 | 2,853 | 137 | 3,818 | 53 |
| `EMERGING_LEADER` | 128 | 919 | 501 | 0 | 3 | 3,591 | 411 |

### Whole shares at $5,000

| Strategy | Rejected at 0 shares | One tranche instead of two | 09:36 skips |
|---|---|---|---|
| `VCP_BREAKOUT` | 56 | 107 | INVALID_INPUT 122, STOP_WIDTH 27, WHOLE_SHARES 9, MAX_CHASE 4, PRICE_LEVELS 1 |
| `MOMENTUM_BREAKOUT` | 42 | 0 | STOP_WIDTH 15, INVALID_INPUT 4, WHOLE_SHARES 3, MAX_CHASE 1 |
| `EPISODIC_PIVOT` | 137 | 0 | STOP_WIDTH 363, WHOLE_SHARES 116, PRICE_LEVELS 58, INVALID_INPUT 14 |
| `LEADER_PULLBACK` | 53 | 66 | INVALID_INPUT 213, STOP_WIDTH 141, WHOLE_SHARES 10, MAX_CHASE 7, PRICE_LEVELS 1 |
| `EMERGING_LEADER` | 411 | 582 | WHOLE_SHARES 116, INVALID_INPUT 74, STOP_WIDTH 44, MAX_CHASE 3, PRICE_LEVELS 1 |

Placed at 09:36 but never filled (not triggered after the open, or past the limit): `VCP_BREAKOUT` 99 of 790, `MOMENTUM_BREAKOUT` 5 of 71, `EPISODIC_PIVOT` 269 of 1015, `LEADER_PULLBACK` 125 of 1064, `EMERGING_LEADER` 82 of 645.

## Reading

*Written 2026-10-24, after the first pool run. Nothing below changes the method; it records what the run shows and what was checked.*

**The baseline gate fails on the tuning period, overall and for four of the five strategies.** The pool's expectancy is -0.03R a trade (90% interval -0.12 to +0.08) over 3,000 closed trades. Its median trade loses about 1R (-1.02R): it wins 30% of the time, at +2.08R against -0.92R. By the pre-registered rule the work stops here and goes to the user before Phase 4. **The holdout has not been opened.** The gate needs both halves, and the tuning half already fails, so 2023 onward wasn't loaded and stays unused.

Per strategy, on the tuning period:
- `EPISODIC_PIVOT` is the only pass (+0.39R, interval +0.07 to +0.75), but it rests on a few outliers and a known bias:
  - Without its 10 best trades it is -0.09R.
  - Two of its three best trades are exchange-traded notes, TVIX (+93R) and DGAZ (+33R), not stocks (see below).
  - Without securities of assumed type it is +0.11R (interval -0.07 to +0.31): a fail.
  - The EP proxy reads the gap session's own close and volume to select a trade entered on that session, a look-ahead the live morning list doesn't have. This result is an upper bound.
- `VCP_BREAKOUT` is flat: -0.01R (-0.14 to +0.12), positive in 2017 and 2019, negative in 2018, 2021 and 2022.
- `LEADER_PULLBACK` (-0.24R), `EMERGING_LEADER` (-0.20R) and `MOMENTUM_BREAKOUT` (-0.57R, only 66 trades) are clearly negative.
- No year carries the result: 2020 is the only strongly positive year (+0.58R), mostly from EPs.

**Checks made before trusting it:**
- **The worst trades are real gaps through the stop.** UL on 2017-02-17 (-5.0R) is a takeover-news gap that reversed the next session. EXE (Chesapeake under today's symbol) in June 2020 (-4.9R) went from its reverse split into bankruptcy. Only 8 trades lost more than 2R.
- **Prices are in the entry session's traded units.** The best trades' fills and stops are at the real prices of the day: NKLA 19.04 -> 65.76 in 2020, TVIX 61.34 -> 409.59 in February 2020.
- **The 09:36 skips are the live order check.** "no price" (no recent trade by 09:36, mostly illiquid names) and under one share at $5,000.

**Data problems found, all of which flatter the pool:**
- **Assumed-type securities.** Delisted securities that discovery found by enumeration have their type assumed `COMMON` (`type_assumed`, task 1.6). They are 319 of the 2,181 securities in the 2019 universe, and they include dead exchange-traded notes: TVIX, DGAZ and UGAZ. Leveraged notes aren't in the stock universe live (README §5.1).
- **No earnings history.** Securities without EDGAR earnings dates (delisted names have no CIK, #130) are rejected by the earnings blackout, which fails closed: 7,189 attempts on 850 securities. EPs have no blackout, which is how the ETNs reached the EP pool. The pool is close to survivors-only for the other strategies, so its expectancy is an upper bound (README §15).
- Without the assumed-type securities the pool is -0.11R (interval -0.18 to -0.05): a clearer fail.

**Not run, because the gate decides first:** the 19 comparisons, the baseline book and its drawdowns, the holdout. The minute bars are cached (`sim/minute_bars/`, 20,629 symbol-sessions), so any of them can be re-run in minutes.

**This agrees with the early warning.** Spike 0.3h's edge check found the base patterns about breakeven before costs. Here, with slippage, whole shares, the guardrails and the default exit plans, they are slightly negative. The decision is the user's: what to change or test next before Phase 4 starts.
