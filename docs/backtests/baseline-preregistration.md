# Baseline backtest (task 3.8): pre-registration

**Written 2026-10-24, before any 3.8 code has run on real data.** Everything below is fixed now. A change after the first run must be listed in `docs/backtests/baseline.md` with its reason, and a change after the holdout is opened voids the holdout as a test (README §15).

Sources: execution plan task 3.8; README §15 "Backtest validity"; decisions #105, #122, #135, #140–#142.

## Order of work

1. **Historical full scan**, every session 2016-01-04 to 2022-12-30, through the live `strategies.watchlist` code on the point-in-time universe (the 1.6 membership table). Each session gets its features as they stood then:
   - **base count:** `indicators.bases` run over each security's whole Parquet history from 2016 (decided with the user 2026-10-24: computed inside 3.8; keeping the live `bases` table current is a separate follow-up task)
   - **group RS rank:** from each security's SIC group in the history's securities table. That's today's SIC applied to the past (no point-in-time group map exists), a small look-ahead the report states.
   - **fundamentals** by EDGAR `filed` date; **earnings dates** from the EDGAR 8-K history
2. **Breakout tracker** from that scan (the live `journal.breakout_tracker` rules, #142), giving the leadership-health pillar its history.
3. **Regime re-run on 5 pillars** over the tuning period only. The thresholds are chosen from the search space below against 2.4's pre-registered targets.
4. **Baseline pool and book** over the tuning period, under the regime chosen in step 3: the gate first, then the comparisons.
5. **Freeze** the chosen regime thresholds and defaults in `risk.yaml` (committed), then **open 2023 to the present once**: the regime's holdout checks, the gate's holdout estimate and the sign of every adopted comparison.
6. Write `docs/backtests/baseline.md`, and re-write `docs/backtests/regime.md` for the 5-pillar run.

Nothing after 2022-12-30 is loaded before step 5.

## Definitions

- **Pool trade:** a setup with a rank in its session's capped list (the top 40 of the regime's fundable strategies, #138), played from the next session under its strategy's default entry policy, exit plan and risk through `sim.trade.simulate_trade`. Pool trades are independent: no heat, exposure, position, cluster or multiplier limits.
  - The setup-level guardrails apply, because a Portfolio Manager couldn't fund a setup that fails them: price levels, stop width, the 50-day extension, the base count, the earnings blackout. The default stop is the structural stop. A stop that's too wide is rejected, not tightened.
  - **One trade per unit** (ticker, strategy, structure: the scorecard's unit, #142). A unit is attempted on each session after a scan that lists it, until it fills; a filled unit isn't entered again.
  - **Sizing:** $5,000 equity, the regime's base risk, the late-stage factor, multiplier 1, whole shares (#122). A trade sized at 0 shares is counted as skipped, and the skips are reported.
  - **EPs:** the EP proxy (the gap session) stands in for the morning candidate list. It can't require an earnings beat historically; the report says how that differs from the live rule.
- **Fills:** the 3.6 fill model (#141). On the entry session the 09:36 levels come from IEX minute bars from 2020-07-27 and from SIP before that; fills come from SIP. Later sessions run on daily bars, with the close as the decision price (README §7.3). Minute bars are fetched only for the trigger sessions of ranked setups and cached in Parquet.
- **R:** the simulator's R (the P&L over the dollars put at risk). A trade still open at the end of the data is marked at the last close and reported apart; it is excluded from the gate.
- **Expectancy:** the mean R per pool trade. The report also gives the win rate, the average gain vs the average loss (in R and %), and the average R per year.
- **Session-block bootstrap:** whole entry sessions are resampled with replacement, 2,000 draws, seed 38, giving a 90% percentile interval (nearest rank). This is the `journal.scorecard` implementation.
- **Baseline book:** `journal.baseline` over the whole period. It starts at $5,000 with every portfolio rule: heat, exposure, positions, clusters, the multiplier and halts. Its **max drawdown** is measured on its daily equity at the close.
- **"More than noise"** (plan 3.8): the 90% interval for the difference excludes zero on the tuning period, and the holdout difference has the same sign.
- **Per year:** every table shows each calendar year. No conclusion may rest on a single year.
- **Cells:** a strategy × regime × option cell with fewer than 30 trades keeps the source default.

## The baseline gate (decided first)

- **Pass:** the pool's tuning-period expectancy interval excludes zero (lower bound > 0), and the holdout point estimate is positive. This is checked overall and for each fundable strategy.
- **A strategy that fails** comes out of the fundable lists in `risk.yaml`, with the reason.
- **If the pool fails as a whole:** stop. Write the finding to `baseline.md`, note that the holdout has been used, and bring it to the user before Phase 4.

## Comparisons (all of them, fixed now)

Each changes one dimension against the reference: the source default, or for exits each strategy's single-trader exit set (README §7.3). Plan and entry options are measured on **pool expectancy** (R per trade). Portfolio and risk controls are measured on the **book**: max drawdown, and total return over the period. Only the adopted combination is checked for interactions.

| # | Comparison | Reference | Measured on |
|---|---|---|---|
| 1 | Pivot trigger vs opening-range-high trigger on gap opens | the strategy's default trigger | pool |
| 2 | One tranche vs two | the strategy's default | pool |
| 3 | Exit sets: single-trader vs the full combination vs the floor (hard stop + trail only), per strategy | single-trader set | pool |
| 4 | Rules added one at a time to the single-trader set: round trip, breakeven, time stops 10 / 20, the heavy-volume-reversal exit, climax sales, the 8-week hold (each where it isn't already in the set) | single-trader set | pool |
| 5 | Safety rules on vs off: the earnings cushion, regime-downgrade tightening (kept unless they cut expectancy by more than noise *without* reducing max drawdown, on both periods) | on | pool and book |
| 6 | Partial styles: Kullamägi ⅓ at session 3, O'Neil ¼ at +20%, Minervini ⅓ at +2R, none (#128) | the strategy's trader | pool |
| 7 | Trails: 10-day vs 21 EMA vs 50 SMA | the strategy's trader | pool |
| 8 | Base count: 3rd stage at half risk and 4th excluded vs no filter | the filter | pool |
| 9 | The progressive-exposure multiplier on vs off | on | book |
| 10 | Leadership-health pillar on vs off | on | book |
| 11 | The 6% heat cap on vs off | on | book |
| 12 | Correlation clusters on vs off; threshold 0.7 vs 0.6 | on, 0.7 | book |
| 13 | `UNDER_PRESSURE` VCPs and pullbacks: top quarter by rank vs all | top quarter | pool and book |
| 14 | `EARLY_UPTREND`: the score-conditional 10-session step vs a flat 0.25% | the step | book |
| 15 | `MomentumBreakout` with vs without the 25%-above-the-50-day rule | with | pool |
| 16 | EP stop cap: 1 × vs 1.5 × the pre-gap ADR, with each one's rejection rate | 1.5 × | pool |
| 17 | `LeaderPullback` confirmation gate: the pullback gate vs the breakout gate (tranche-2 pass rate and expectancy) | pullback gate | pool |
| 18 | The climax `fast_gain_min_move_sessions`: 10 / 15 / 20 | 15 (provisional) | pool |
| 19 | Momentum consolidation depth: results by depth bucket (≤ 15%, 15–25%, > 25%); a cap is adopted only by the rule above (#136) | no cap | pool |

**Diagnostics** (reported, never tuned):
- how often each guardrail rejects, per strategy (stop width above all, for momentum and EPs), and how often the cluster rule fires
- how often an entry triggers but misses its limit, how often the opening-range rule replaces the pivot, and how often the 09:36 limit buy is used
- the expectancy the missed entries would have had at the limit
- how many trades are skipped at 0 shares or entered as a single tranche, per strategy and risk level
- on 200 randomly sampled held position-days (seed 38), how often a soft-rule decision on the 15:40 price differs from one on the close
- the delisted-coverage gap, and the slippage tiers used (#141)

## Regime re-run (finishes 2.4)

**Search space** (tuning period, 5 pillars, scores −5 to +5): `CONFIRMED_UPTREND` from +2 or +3; `CORRECTION` at ≤ −1 or ≤ −2; `correction_2` (2 sessions at the score before `CORRECTION`, index trend −1 still immediate) on or off; the distribution pillar at −1 from 6 or 8 days. That's 16 variants. Everything else stays at its `risk.yaml` value.

**Targets on the tuning period:** 2.4's pre-registered five:
1. declines: `CORRECTION` is reached while SPY's drawdown is under 50% of the decline's full drawdown
2. the 2020 recovery: `EARLY_UPTREND` within 15 sessions of the 2020-03-23 low, `CONFIRMED_UPTREND` within 60
3. at most 12 switches in any year, failed-FTD round trips counted apart (#135)
4. at most 1 false `CORRECTION` spell a year on average
5. `CONFIRMED_UPTREND` covers at least 60% of 2017, 2019 and H2 2020–2021

**Choice:** the variant that passes the most targets. Ties go to the fewest switches a year, then to the one closest to IBD's published values (+3, ≤ −1, 6 days).

**Holdout checks (2023 on, opened once in step 5):**
- the 2023 recovery: `EARLY_UPTREND` within 15 sessions of SPY's 2022-10-12 low, `CONFIRMED_UPTREND` within 60
- at most 12 switches in each full year (failed FTDs apart)
- at most 1 false `CORRECTION` spell a year

## Known biases the report must state

- **Survivorship:** delisted securities the vendor doesn't cover are missing (0.3a). They were more often losers, so expectancy is an upper bound, and options that favour long holds get extra scrutiny.
- **Industry groups:** today's SIC is applied to the past.
- **EP proxy:** it has no earnings-beat condition.
- **Slippage tiers** are provisional (#141).
- **Decision price:** later sessions decide on the close, not the 15:40 price (measured by the diagnostic above).
- **Confirmation period:** "beats on both periods" filters about 20 comparisons, so the adopted configuration's 2023-on numbers are somewhat flattering.
