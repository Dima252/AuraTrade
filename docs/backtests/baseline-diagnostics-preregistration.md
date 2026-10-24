# Pre-registration: why the baseline pool loses (task 3.8, after the gate failed)

**Written 2026-10-24, before any of these splits was computed.** The pool failed its gate on the tuning period (-0.10R, 90% -0.16 to -0.03; docs/backtests/baseline.md). This note fixes, in advance, what is looked at to find out why, and how each result is read. It looks at the **tuning period only** (2017-2022); the holdout stays closed.

The question: is the loss **fixable** (costs, the regime, an implementation error, a filter the sources would apply and the code doesn't) or **built in** (these setups, traded mechanically in this universe, don't pay)?

## What is computed

Everything runs on the reference pool of docs/backtests/baseline.md (same regime `cu+2_corr-2_c2_dist8`, same `risk.yaml`, closed trades only), overall and per strategy where a bucket holds 30 trades or more. Every table gives trades, expectancy, win rate and the per-year expectancy. The script is `backtest/diagnostics.py`, the report `docs/backtests/baseline-diagnostics.md`.

| # | Question | Split (buckets fixed here) |
|---|---|---|
| D1 | **Costs:** how much of the loss is slippage? | The pool re-played through `sim/` with every slippage tier at 0, against the reference (the difference and its interval, as the comparisons compute it), and the gate on the zero-cost pool |
| D2 | **Chase:** do fills far above the pivot lose? | Entry fill / code pivot - 1: <= 0.5%, 0.5-1.5%, 1.5-3%, > 3% |
| D3 | **Stop width:** do tight or wide stops lose? | (entry fill - initial stop) / entry fill: < 3%, 3-5%, 5-7%, >= 7% |
| D4 | **Ranking:** does the score pick better trades? | Rank in the capped list 1-10, 11-20, 21-40; conviction >= 4 (top quarter) vs the rest |
| D5 | **Regime:** does the market filter keep us out of bad tapes? | The regime score the evening before entry: <= +2, +3, +4, >= +5; the leadership pillar <= 0 vs > 0; the distribution pillar < 0 vs 0 |
| D6 | **Liquidity and price:** | 50-session dollar volume < $25M, $25-100M, $100-500M, >= $500M (the slippage tiers); entry price < $10, $10-20, $20-50, >= $50 |
| D7 | **Leadership features** the sources lean on: | RS rating < 80, 80-90, >= 90; group RS < 50, 50-80, >= 80; extension above the 50-day < 5%, 5-15%, 15-25%, >= 25%; ADR < 3%, 3-5%, 5-8%, >= 8%; EPS growth none or <= 0, 0-25%, > 25%; base stage 1, 2, 3+ |
| D8 | **Anatomy of the losses:** | The exit rule of each trade, with its mean R; the exit session 1, 2-5, 6-20, > 20; the share of trades stopped out at the hard stop within 5 sessions (failed breakouts); the share of losers that closed a session at +1R or more first (the exits gave it back) |
| D9 | **Implementation:** does the code find what the sources describe? | 12 closed trades drawn at random (seed 38), 3 each of `VCP_BREAKOUT`, `MOMENTUM_BREAKOUT`, `LEADER_PULLBACK`, `EMERGING_LEADER`, drawn as daily charts with the pivot, stop and entry marked and read by eye against the README's definition of the setup |

## How the results are read (fixed now)

1. **Costs (D1).** If the zero-cost pool's interval lies above zero, costs are the main cause, and the lever is liquidity (which names are traded), not the setups. If removing costs moves the pool by less than +0.05R, costs are ruled out as the main cause.
2. **An informative split** (D2-D7): the expectancy difference between the best and the worst bucket, with the session-block interval of `backtest.comparisons.difference`, excludes zero, **and** the best bucket beats the worst in at least 4 of the 6 tuning years (years with at least 10 trades in both). About 30 splits are looked at, so at a 90% interval two or three will look informative by chance; the year rule is there to catch those.
3. **A candidate fix** must come from an informative split, have a source behind it (Minervini, O'Neil or Kullamägi apply that filter, or README §15's liquidity reasoning), and on its own make the pool's tuning interval exclude zero from above. At most two are proposed. None is adopted here: each goes to the user, and if approved it is pre-registered as a variant, the pool re-run, and it is checked on the holdout when the holdout is opened once.
4. **A bug** (D9, or anything in D2-D8 that the code shouldn't be able to produce) is fixed first, with a test, then the pool and this diagnostic are re-run before anything else is read.
5. **Built in:** if costs are ruled out, no split is informative and D9 finds the setups faithful, the report says so plainly: the setups as specified don't pay in this universe after costs, and the decision goes back to the user.

## Addendum (2026-10-24): the EP proxy fix, fixed before it runs

The diagnostics found the EP proxy looks ahead (docs/backtests/baseline-diagnostics.md, Reading 2). The user approved option A the same day. Written before the fixed pool runs:

- **What changes, for the pool only.** An EP candidate is what is known at 09:36 on the gap session:
  - the gap at the open (open >= 10% above the prior close, as before);
  - the opening volume: the SIP minute bars from the open to the entry (09:30-09:35, `sim.entry_minutes`) must reach **25%** of the prior 50 sessions' average daily volume (`strategies.EPISODIC_PIVOT.min_opening_volume_ratio: 0.25`). The full-day 3x volume rule is no longer applied to the pool's candidates;
  - its ranking features, price and ATR from the **prior close**, not the gap session's.
- **What doesn't change.** The evening scan (the gap session on 3x full-day volume) stays for the breakout tracker and the evening watchlist, which read it after the close; so the regime run and its chosen variant don't change. Every other strategy's candidates are unchanged.
- **Known residual.** EPs are still listed on the gap session's capped list, chosen under that session's computed regime; the guardrails use the regime of the evening before, as for every entry. The report counts the EP listings where the two differ.
- **The run.** `uv run python -m backtest.baseline pool`, once, on the tuning period, with the comparisons. The gate is read as before, overall and per strategy. **The holdout stays closed.**
- **How it is read.**
  - If `EPISODIC_PIVOT`'s interval stays above zero... it can't have been above zero before (it was -0.03 to +0.55). So: **EPs pass** if the fixed proxy's interval excludes zero from above. Otherwise EPs fail like the other four.
  - The fixed EP numbers replace the old ones everywhere. The old ones are kept in the report's history, labelled as looking ahead.
  - Nothing else is re-chosen from this run. The strategy discussion with the user comes after it.
