# The close entry (task 3.8, #144): blind mechanics check

**Run:** 2026-10-25 19:48 UTC with `uv run python -m backtest.baseline close --blind`, the `CLOSE` candidate's risk.yaml `137ee5c9360b`, regime `cu+2_corr-2_c2_dist8`, 328 s. **Pre-registration:** `docs/backtests/close-entry-preregistration.md`, approved by the user 2026-10-25 in commit `5b8a22a`. Generated; only the Reading at the end is written by hand.

**The tuning period only (2017-2022).** The holdout (2023 on) is not loaded.

**Blind (step 3).** Counts and invariants only: no R, P&L, expectancy, win rate or exit is computed into this page and no trades are saved. It checks the code on real data before the one scored run (step 4).

## The attempts

Every unit with a rank in a session's capped list, attempted on each following session while listed until it fills (an EP on its gap session). `REJECTED`: a setup-level guardrail at proposal. `NOT_CONFIRMED`: the gate failed at the decision time. `SKIPPED`: the decision-time order check rejected it. `CLOSED` and `OPEN` are fills (blind: shown together as filled).

| Strategy | Attempts | REJECTED | NOT_CONFIRMED | SKIPPED | MISSED | Filled |
|---|---|---|---|---|---|---|
| `VCP_BREAKOUT` | 11,631 | 3,719 | 7,823 | 4 | 0 | 85 |
| `MOMENTUM_BREAKOUT` | 2,084 | 560 | 1,504 | 15 | 0 | 5 |
| `EPISODIC_PIVOT` | 3,146 | 1,013 | 1,473 | 383 | 0 | 277 |
| `LEADER_PULLBACK` | 18,141 | 5,871 | 11,607 | 162 | 0 | 501 |
| `EMERGING_LEADER` | 7,095 | 2,914 | 3,997 | 47 | 0 | 137 |

## Invariants

Every fill is one tranche of at least one share, between its stop and its limit, with its gate passed and dollars at risk, and nothing fills after its exit.

**All hold.**

## Rejections at proposal

The guardrail that rejected each attempt before the decision time (the first rejection ends the check), per strategy.

| Strategy | `BASE_COUNT` | `EARNINGS_BLACKOUT` | `ENTRIES_BLOCKED` | `FUNDABLE` | `PRICE_LEVELS` | `WHOLE_SHARES` |
|---|---|---|---|---|---|---|
| `VCP_BREAKOUT` | 597 | 706 | 193 | 2,123 | 5 | 95 |
| `MOMENTUM_BREAKOUT` | 357 | 91 | 31 | 0 | 15 | 66 |
| `EPISODIC_PIVOT` | 102 | 0 | 177 | 547 | 28 | 159 |
| `LEADER_PULLBACK` | 785 | 906 | 600 | 3,253 | 150 | 177 |
| `EMERGING_LEADER` | 421 | 448 | 484 | 0 | 3 | 1,558 |

## The gate at the decision time

Attempts that reached the gate (past the proposal guardrails), how many passed, and how often each condition failed (an attempt can fail several). A unit is attempted on every session it stays listed, so most attempts are quiet sessions below the level; the second table keeps the attempts that closed at or above it.

**Every attempt at the gate:**

| Strategy | Reached | Passed | below the level | max chase | range | volume | RS line | RS low | MA |
|---|---|---|---|---|---|---|---|---|---|
| `VCP_BREAKOUT` | 7,912 | 89 of 7,912 (1%) | 7,016 | 32 | 3,753 | 7,278 | 5,471 | 0 | 0 |
| `MOMENTUM_BREAKOUT` | 1,523 | 19 of 1,523 (1%) | 1,427 | 19 | 724 | 1,360 | 849 | 0 | 0 |
| `EPISODIC_PIVOT` | 2,133 | 660 of 2,133 (31%) | 1,101 | 0 | 1,165 | 585 | 0 | 0 | 0 |
| `LEADER_PULLBACK` | 12,269 | 662 of 12,269 (5%) | 8,840 | 152 | 5,938 | 9,621 | 0 | 51 | 5,559 |
| `EMERGING_LEADER` | 4,176 | 179 of 4,176 (4%) | 3,202 | 18 | 2,123 | 3,511 | 214 | 0 | 0 |

**Closed at or above the level:**

| Strategy | Reached | Passed | below the level | max chase | range | volume | RS line | RS low | MA |
|---|---|---|---|---|---|---|---|---|---|
| `VCP_BREAKOUT` | 896 | 89 of 896 (10%) | 0 | 32 | 66 | 716 | 386 | 0 | 0 |
| `MOMENTUM_BREAKOUT` | 96 | 19 of 96 (20%) | 0 | 19 | 6 | 60 | 7 | 0 | 0 |
| `EPISODIC_PIVOT` | 1,032 | 660 of 1,032 (64%) | 0 | 0 | 217 | 201 | 0 | 0 | 0 |
| `LEADER_PULLBACK` | 3,429 | 662 of 3,429 (19%) | 0 | 152 | 450 | 2,517 | 0 | 1 | 109 |
| `EMERGING_LEADER` | 974 | 179 of 974 (18%) | 0 | 18 | 116 | 757 | 16 | 0 | 0 |

## Stops

Stop-width rejections at the decision time (of the attempts whose gate passed; the width is measured from the limit) and at proposal (only a structural stop is checked there); how often the trigger session's low, rather than the structural stop, set the stop; and the width of the stops placed.

| Strategy | Width rejections at the decision time | At proposal | Set by the day's low | < 3% | 3-5% | 5-8% | >= 8% | Median width |
|---|---|---|---|---|---|---|---|---|
| `VCP_BREAKOUT` | 2 of 89 (2%) | 0 | 83 of 85 (98%) | 32 | 35 | 18 | 0 | 3.6% |
| `MOMENTUM_BREAKOUT` | 13 of 19 (68%) | 0 | 5 of 5 (100%) | 1 | 2 | 1 | 1 | 4.5% |
| `EPISODIC_PIVOT` | 355 of 660 (54%) | 0 | 277 of 277 (100%) | 52 | 71 | 154 | 0 | 5.3% |
| `LEADER_PULLBACK` | 153 of 662 (23%) | 0 | 490 of 501 (98%) | 323 | 142 | 32 | 4 | 2.5% |
| `EMERGING_LEADER` | 0 of 179 (0%) | 0 | 137 of 137 (100%) | 74 | 46 | 17 | 0 | 2.7% |

## Sizes and the pay-up

Under one share at $5,000 per risk level (rejected at proposal or at the decision time), and the entry above the level: in % of the level and in R (the fill minus the level over the fill minus the stop), medians over the fills.

| Strategy | Under one share (risk: count) | Pay-up (%) | Pay-up (R) |
|---|---|---|---|
| `VCP_BREAKOUT` | 0.50%: 2, proposal: 95 | 1.7% | +0.54 |
| `MOMENTUM_BREAKOUT` | 0.50%: 1, proposal: 66 | 2.0% | +0.61 |
| `EPISODIC_PIVOT` | 0.12%: 2, 0.25%: 6, 0.50%: 8, 1.00%: 12, proposal: 159 | 2.5% | +0.63 |
| `LEADER_PULLBACK` | 0.25%: 2, 0.50%: 3, 1.00%: 3, proposal: 177 | 1.4% | +0.61 |
| `EMERGING_LEADER` | 0.12%: 8, 0.25%: 33, 0.50%: 1, proposal: 1558 | 1.4% | +0.55 |

## The decision-price checks (section 6)

**The gate at 15:40 vs the close.** 200 attempts that reached the gate, drawn at random (seed 38); 200 compared (0 without minute bars or a 15:40 price). The 15:40 gate reads the last SIP trade before 15:40 and the range so far; volume is the full day in both. **Disagreement: 3 of 200 (2%)** against a threshold of 15%: **within the threshold**. Price difference on the attempts both passed: mean 0.46%, 90th percentile 0.25%.

**Held sessions (owed from 3.8).** 200 held position-days drawn at random (seed 38); 200 compared (0 without minute bars). A session disagrees when its plan events or its sales and exits (other than the hard stop) differ between the 15:40 price and the close: **14 of 200 (7%)**. Reported, decides nothing.

## The 09:36 reference (3.8, not re-run)

From `docs/backtests/baseline.md`, the EP after the look-ahead fix (#143).

| Strategy | Trades | Expectancy (R) | 90% interval |
|---|---|---|---|
| `VCP_BREAKOUT` | 1,023 | -0.03 | -0.14 to +0.07 |
| `MOMENTUM_BREAKOUT` | 130 | -0.51 | -0.68 to -0.30 |
| `LEADER_PULLBACK` | 1,500 | -0.22 | -0.31 to -0.14 |
| `EMERGING_LEADER` | 754 | -0.21 | -0.28 to -0.14 |
| `EPISODIC_PIVOT` (after #143) | 625 | +0.21 | -0.09 to +0.55 |
| All (after #143) | 4,052 | -0.11 | -0.18 to -0.04 |

## Known biases

- **Survivorship:** delisted securities the vendor doesn't cover are missing; expectancy is an upper bound.
- **Industry groups:** today's SIC is applied to the past.
- **EP proxy:** no earnings-beat or news condition; live EPs require one, so they are fewer.
- **The decision price is the close** and the volume the full day (measured above).
- **The live small-cap gap:** at 15:40 the IEX spread cap (#120) leaves many small caps without a decision price; entries fall back to the delayed SIP last trade (#144). The $100M tier stands in for the names with a usable IEX price.
- **Slippage tiers** are provisional (#141).
- **Single-session blocks** understate the interval's width (the 21-session column).
- **A second look at 2017-2022:** the 3.8 run and its diagnostics were read before these candidates were chosen; the per-year rule and the holdout are the guards.
- **Candidates:** 1 = `EPISODIC_PIVOT`; 2 = `VCP_BREAKOUT`, `LEADER_PULLBACK`, `EMERGING_LEADER`, `MOMENTUM_BREAKOUT`.

## Reading

(Not written yet.)
