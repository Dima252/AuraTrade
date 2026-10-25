# The close entry (task 3.8, after the failed gate): pre-registration

**Written 2026-10-25, before any close-entry code exists or has run on real data.** **Status: approved by the user 2026-10-25.** The commit that holds the approved text is quoted at the top of `docs/backtests/close-entry.md`. Everything below is fixed from that commit:
- A change after the blind mechanics check (step 3) needs the user's approval and is listed in the report with its reason.
- A change after the scored run (step 4) makes that change a new trial, which this page doesn't allow (§9).
- A change after the holdout is opened voids the holdout as a test (README §15).

Sources:
- the failed 3.8 gate: `docs/backtests/baseline.md`, `baseline-diagnostics.md`, #143;
- the review `docs/strategy-review-2026-10-25.md` §7–§8;
- the user's answers of 2026-10-25 (`docs/strategy-execution-2026-10-25.md` §2);
- README §7.1 and §15;
- decisions #120, #122, #135–#144;
- 3.8's own pre-registration `docs/backtests/baseline-preregistration.md`, whose definitions apply wherever this page doesn't replace them.

## 1. What is being tested

**One new entry mechanism, confirmation at the decision time.** On the trigger session, code checks the setup at the 15:40 decision price (#120). If it passes, it buys with a marketable limit. The hard stop sits at the trigger session's low, floored by the structural stop.

The hypothesis is the review's (§2.1). It skips the entry-day failures that the 09:36 entries took (469 stopped on the entry session), and it moves the stop out of the morning's noise. In exchange it pays the day's move. Overnight-return evidence bears only on the entry day and is not the claim.

Two candidates, and no more (the user's answer 4):

| | Candidate 1: `EPISODIC_PIVOT` at the close | Candidate 2: the scan strategies at the close |
|---|---|---|
| Strategies | `EPISODIC_PIVOT` | `VCP_BREAKOUT`, `LEADER_PULLBACK`, `EMERGING_LEADER`, `MOMENTUM_BREAKOUT` |
| Replaces | the 09:36 opening-range entry, the 09:36 low-of-day stop, the 25% opening-volume check (#143), the 1.5 × pre-gap ADR cap | the 09:36 pivot / opening-range entry, two tranches, the momentum low-of-day stop |

## 2. Order of work

1. **This page approved by the user and committed.** Decisions #144 and #145 recorded in the README in the same PR.
2. **Build and test on hand-built data only** (execution plan 3.8c): the pure layer, the simulator entry, the pool, the report. No code reads 2016–2022 outcomes before step 3.
3. **The blind mechanics check.** One run over 2017–2022 with the report's blind flag set. It writes counts and invariants only: no R, no P&L, no expectancy, no win rate, no average gain or loss. The decision-price sample (§6) runs here too.
   - A bug found here may be fixed and step 3 re-run; it is blind.
   - **If a rule below can't work as written** (for example a strategy with almost no gate passes, or almost every stop rejected), the agent stops and brings it to the user before step 4. Nothing is adjusted silently.
4. **The scored run.** One invocation of `backtest.baseline` over 2017-01-03 to 2022-12-30 under the regime variant `cu+2_corr-2_c2_dist8` (`docs/backtests/regime-5-pillar.md`): both candidates and both comparisons (§7). The report is committed as produced, before it is read for meaning.
5. **Reading it** by §8, with the user.
6. **Only if something passes:**
   - freeze the passing entry policies, the fundable lists and the 5-pillar regime in `risk.yaml` (committed, hash recorded);
   - then open 2023-01-03 to the latest backfilled session **once**, for everything at once (§8.4).

Nothing after 2022-12-30 is loaded before step 6.

## 3. Shared definitions

Everything in 3.8's definitions applies unless replaced here: the pool trade, one trade per unit, the setup-level guardrails, R, expectancy, per-year tables, the cells.

- **Decision time:** 15:40 (12:40 on half days). **No entries on half days**: the existing tranche-2 rule (#142) is kept.
- **Decision price in the backtest:** the session's close. This flatters the backtest in two ways: the gate reads the last 20 minutes, and the fill is at the close rather than at 15:40. §6 measures by how much.
- **Day volume in the backtest:** the full session's volume, as for tranche 2 (#142). Live, it is the delayed SIP volume projected by the pace curve.
- **The limit:** the decision price × (1 + 0.005) (`execution.marketable_limit_slippage`, #131), rounded up to the tick.
- **The fill:** the decision price plus the liquidity-tier slippage (`sim.slippage`, #141), rounded to the tick against the buyer. It fills only at or under the limit. With the current tiers (at most 0.30%) every gate pass fills; the rule is kept so a later tier change can't fill above the limit. **This is the tranche-2 fill, through the tranche-2 code** (`sim/trade.py::_tranche2`), so there is still one fill model.
- **One tranche.** The two-tranche rules, the tranche-2 gate on later days and `min_two_tranche_shares` don't apply.
- **Sizing:** $5,000 equity, the regime's base risk (`regimes.*.base_risk`), the late-stage factor, multiplier 1. Sized on the limit and the hard stop; whole shares, floored (#122). 0 shares is a skip, counted per strategy and risk level.
- **The stop is one hard stop at the broker** (a GTC stop leg on the entry, as for tranche 2). There is no separate soft structural stop. The plan's later rules (breakeven, trails) raise it as now; it is never lowered.
- **Stop width is measured from the limit for every strategy** (the worst fill), with each strategy's existing unit and maximum. The ATR and ADR caps are measured from the limit, not the trigger: at the close the trigger *is* the decision price, and the limit sits 0.5% above it. A stop wider than its cap is **rejected, never tightened**. The noise floors (`stop_widths.*.noise_floor_atr`) and "keep losses small" apply unchanged, the latter only after 20 closed trades of a strategy (#140).
- **Fundable strategies and conviction bars:** as in `risk.yaml` `regimes:` today, on the evening-before regime. That includes `UNDER_PRESSURE` VCPs, pullbacks and EPs only in the top quarter by score (conviction ≥ 4, #142's proxy).
- **Retries:** a unit is attempted on each session after a scan that lists it, until it fills; a filled unit isn't entered again (3.8's rule). A gate failure is an attempt, not a fill.
- **Session counting:** the entry session is session 0 of the plan, as now. A trade entered at the decision time takes no further action that session in the backtest, since the decision price is the close.
- **The bootstrap (primary):** whole entry sessions resampled with replacement, 2,000 draws, seed 38, 90% percentile interval, nearest rank (`journal.scorecard.block_bootstrap`, unchanged).
- **The bootstrap (secondary, reported only):**
  - the same, but resampling **blocks of 21 consecutive trading sessions**;
  - non-overlapping, starting at the first session of the tuning period, with a final shorter block kept;
  - a block carries every entry in its sessions;
  - 2,000 draws, seed 38.

  It decides nothing. It shows how much the single-session blocks flatter the interval (review §2.4).

## 4. Candidate 1: `EPISODIC_PIVOT` at the close

- **Listing (the morning view, #143):**
  - the gap session's open is ≥ 10% above the prior close (`strategies.episodic_pivot.min_gap`);
  - the setup is ranked on its prior-close features (`known_at`, unchanged);
  - it takes a slot in the gap session's capped list as the 09:36 view does today, on the evening-before regime.

  The 25% opening-volume check (`min_opening_volume_ratio`) is **not** applied: volume is judged at the decision time instead. No earnings or news condition (the proxy can't require one historically; §10).
- **The gate at the decision time** (all three):
  1. the decision price ≥ the gap session's open (the gap held);
  2. the range position ≥ 0.5, where range position = (price − the session's low) ÷ (the session's high − the session's low), the confirmation gates' definition;
  3. the day's volume ≥ 3.0 × the prior 50-session average (`min_volume_ratio`, the after-close EP rule).
- **No max chase.** An EP has no prior pivot to chase from (README §7.1: EPs are exempt from the 50-day rule and the gap invalidation). How far above the day's low it can be bought is bounded by the 8% stop cap below.
- **Hard stop:** the gap session's low, floored by the structural stop (the prior close, #136): stop = max(gap-session low, prior close).
- **Stop cap:** at most 8% below the limit (unit `LIMIT_PCT`, max 0.08, noise floor 0.5 × ATR). This is Minervini's and O'Neil's maximum loss, the cap VCPs and emerging leaders already use. It replaces the 1.5 × pre-gap ADR cap, which was sized for a stop at the 09:36 low.
- **Plan:** the Kullamägi set as it is (`exit_sets.KULLAMAGI`: ⅓ after 3 sessions, breakeven after the partial, the 10-day trail) and the safety rules.
- **Guardrails:** the setup-level ones as today. EPs stay exempt from the 50-day extension rule and the gap invalidation.

## 5. Candidate 2: the scan strategies at the close

- **Listing:** unchanged: the evening scan, the ranking (#138, tightness component included, the user's answer 7), the cap, the regime.
- **Trigger session:** any session after a scan that lists the unit (3.8's retry rule).
- **The gate at the decision time:** the strategy's existing confirmation gate (`plans.confirmation_gates`, README §7.1), on the existing level:

  | Strategy | Gate | Level | Conditions (as in `risk.yaml`) |
  |---|---|---|---|
  | `VCP_BREAKOUT` | `BREAKOUT` | the pivot | level ≤ price ≤ level × 1.05; range position ≥ 0.5; volume ≥ 1.4 × the 50-day average; RS line at or near a high |
  | `EMERGING_LEADER` | `BREAKOUT` | the pivot (the 52-week high) | as above |
  | `MOMENTUM_BREAKOUT` | `BREAKOUT` | the consolidation high | as above. **New:** momentum has no gate today, and `BREAKOUT` is the README's gate for a breakout |
  | `LEADER_PULLBACK` | `PULLBACK` | the trigger (the prior session's high) | level ≤ price ≤ level × 1.05; range position ≥ 0.5; volume ≥ 1.0 × the 50-day average; above the tested EMA; RS line not at a 50-session low |

  The upper bound (level × 1.05) is the max chase, applied to the decision price.
- **Hard stop:** stop = max(the structural stop, the trigger session's low). The structural stop is the scan's own: VCP below the final contraction's low; emerging leader the 5-session low (#137); pullback the lower of the pullback low and the tested EMA (#137); momentum the low of the consolidation's second half, its last higher low (`strategies/momentum_breakout.py`).
- **Stop caps, measured from the limit:** VCP 8%, emerging leader 8%, pullback 1.5 × ATR(14), momentum 1 × ADR (`plans.stop_widths`, units unchanged).
- **Plan:** each strategy's single-trader set (`default_plans`) and the safety rules, unchanged.
- **Guardrails:** the setup-level ones as today: price levels, stop width, the 50-day extension, the base count, the earnings blackout.

## 6. The decision-price check (run in step 3, blind)

The backtest decides and fills at the close; live decides and fills at 15:40. This measures the difference before the scored run.

- **Sample:** 200 attempts (unit × session) drawn at random, seed 38, from both candidates' attempts in 2017–2022 (passes and failures alike). The SIP 1-minute bars come from the 3.8 cache where present, otherwise from Alpaca's free historical endpoint.
- **What is compared:** the gate's decision on the close against its decision on the last SIP trade at or before 15:40, with the range so far at 15:40. The volume condition uses the full day in both, since there is no pace history. Reported: the disagreement rate, and the mean and the 90th percentile of |close − 15:40 price| ÷ price on the agreeing passes.
- **Threshold, fixed now:** if the decisions disagree on more than **15%** of the sample, the scored run may still be made and reported. **Nothing is adopted from it** until the user has decided how to handle the gap.
- **Also reported here, owed from 3.8:** 3.8 pre-registered a check on 200 held position-days (seed 38): how often a soft-rule decision on the 15:40 price differs from one on the close. It was never reported. It runs in this step on the same cache, because the close entry's exits depend on it just as much.

## 7. Comparisons (two, both run in step 4)

Each changes one dimension against its candidate. They are adopted only by the existing rule: they beat the candidate on **both** periods by more than noise (README §15).

1. **The volume condition off:** each candidate's gate without its volume condition (price and range only). This measures what the IBD and EP volume rules earn or cost at the close.
2. **The stop at the structural level** instead of max(structural, trigger-session low), **candidate 2 only.** For an EP the structural stop is the prior close, which is at least 10% below the open and so rejected by the 8% cap almost always; the comparison would only measure rejections. The rejection rate is reported for each arm.

## 8. How the result is read (fixed now)

### 8.1 The gate (README §15, with the additions of #144)

A strategy **passes** on the tuning period only if all four hold:
1. **The interval:** the primary 90% interval of its expectancy has a lower bound > 0.
2. **The per-year rule:** its expectancy is positive in at least 4 of the 6 tuning years (2017–2022), **and** its expectancy with its best year left out is ≥ 0. The best year is the year with the highest expectancy for that strategy.
3. **The sample:** at least 300 closed trades. Trades still open at the end of the data are reported apart and excluded, as in 3.8.
4. **The decision-price check** (§6) is at or under 15%.

A strategy that passes 1 but fails 2 is reported as "**passes on one year**" and is not adopted. The pool of all passing strategies must also have an interval above zero. The pool of everything is reported alongside.

### 8.2 The outcomes

| Outcome on 2017–2022 | What happens |
|---|---|
| One or more strategies pass | Step 6: freeze, open the holdout once. Strategies that pass on both periods form the fundable lists; the others leave them, with the reason. The candidate policy becomes the default in README §7.1 and `risk.yaml` `entry:` |
| `EPISODIC_PIVOT` has a positive point estimate but fails (the review's expected case) | **Not fundable from the backtest** (the user's answer 6). It stays in the scan and the capped list, so the Analyst reviews it in `dry_run` and the scorecard scores it. The user reconsiders it when the scorecard has 100 triggered EP setups. No entry with capital before that. How a "scored, not funded" strategy is marked in `risk.yaml` is designed in Phase 4/5 (no capital moves in `dry_run`, so nothing depends on it before then) |
| Another strategy has a positive point estimate but fails | The same as above |
| Nothing passes | **Stop.** No further runs on 2017–2022, whatever the report suggests. **The fallback is F** (the user's answer 2): Phase 4 continues in `dry_run`, and before 4.6 deploys, a pre-registered bar is written that triggered BUYs must beat triggered SKIPs by more than the pool's deficit plus δ (0.1R) on the scorecard (review §4.2). No capital until that bar is met |
| The decision-price check fails (§6) | Reported. Nothing adopted until the user decides |

### 8.3 Comparisons

Adopted only if they beat their candidate on both periods by more than noise. A comparison is never read as a reason to re-run with a different setting.

### 8.4 The holdout (step 6 only)

Opened once, for everything at once:
- the gate's holdout point estimate (> 0) for every strategy that passed on tuning;
- the 5-pillar regime's holdout checks from 3.8's pre-registration (the 2023 recovery; ≤ 12 switches a year with failed FTDs apart; ≤ 1 false `CORRECTION` spell a year);
- the sign of each adopted comparison.

It is labelled a confirmation period (README §15). If it is ever re-run after a change, the report says so.

## 9. Not allowed (each would be another trial)

On the strength of the blind check or the scored run, none of these may change:
- the gate conditions and their thresholds;
- the stop rules and caps;
- the slippage tiers;
- the ranking weights or components;
- the universe filters;
- the plan defaults;
- the regime variant;
- the statistic, its seed or its draws.

Also not allowed:
- a third candidate;
- dropping a year;
- restricting the universe after the fact (the ≥ $100M tier is reported, not adopted);
- adding a split as a filter.

The paper account's real fills (1.8) are compared with `sim.slippage` as a measurement, reported beside the run, and never used to re-run it.

## 10. Diagnostics (reported, never tuned)

Per strategy, per year and per regime:
- **the gate:** its pass rate at the decision time, and which condition fails most (price below the level, the max chase, the range, volume, the RS line, the MA);
- **stops:** the distribution of stop widths, the stop-width rejection rate, and how often the trigger-session low (rather than the structural stop) sets the stop;
- **the pay-up:** the entry price above the level, in % and in R;
- **the max chase:** how many attempts it skipped, and the expectancy those would have had entered at the close with the same stop rule (the missed-entry diagnostic 3.8 pre-registered and didn't report);
- whole-share skips (0 shares), per risk level;
- **costs:** the expectancy at zero slippage beside the real one, as in the 3.8 diagnostics;
- **the ≥ $100M dollar-volume tier apart** (50-session average), as the proxy for the names with a usable IEX decision price at 15:40 (#120, the 2026-10-23 finding). Reported, not adopted;
- **the 09:36 reference:** each strategy's 3.8 result (`baseline.md`, EP after #143), quoted beside the candidate. It is not re-run;
- the early-failure anatomy (stopped within 1 and within 5 sessions), as in D8.

## 11. Known biases the report must state

- **Survivorship:** delisted securities the vendor doesn't cover are missing. Expectancy is an upper bound.
- **Industry groups:** today's SIC is applied to the past.
- **EP proxy:** no earnings-beat or news condition. Live EPs require one (README §5.6), so live EPs are fewer.
- **The decision price is the close,** and the volume is the full day: measured in §6.
- **The live small-cap gap:** at 15:40 the IEX spread cap (#120) leaves many small caps without a decision price. The user chose a delayed-SIP reference for entries (#144) so they can still be entered live. The backtest can't model IEX quotes; the ≥ $100M tier stands in.
- **Slippage tiers** are provisional (#141).
- **Single-session blocks** understate the interval's width (§3, the secondary statistic).
- **This is a second look at 2017–2022.** The 3.8 run and its diagnostics have been read. The candidates were chosen with that knowledge, and the gate's interval doesn't account for it. The per-year rule and the holdout are the guards.
