# The strategy change: from chart-pattern setups to a momentum book (#146)

**Status 2026-10-25: decided by the user, not yet planned or built.** The plan (the rules, the pre-registration, the build order) is written in a separate chat; this page is its input. It records what was decided, why, what carries over, and the questions the plan must answer. Nothing below is a rule yet unless it says "decided".

## 1. The decision

The system stops trading chart-pattern setups (VCP, leader pullbacks, emerging leaders, momentum breakouts, episodic pivots) and becomes a **relative-strength momentum book**: it holds a small number of the strongest US stocks by intermediate-term return, rebalanced on a fixed schedule, each position with a protective stop at the broker. The LLM decides within that list; code enforces the limits. This is option 1 of the options the user was shown on 2026-10-25 (option D of `docs/strategy-decision.md`).

**Decided:**
- the strategy family: a cross-sectional momentum book on US stocks;
- the split stays: **the LLM decides, code enforces limits** (CLAUDE.md §3.4). Code builds the candidate list and every price level; the LLM chooses within it and may only tighten;
- every hard rule in CLAUDE.md §3 stays: paper only, stops always at the broker covering the full quantity, fail closed, no paid calls without the user's OK;
- the account: $5,000 paper, whole shares (#122); free data tiers; budget under $150 a month;
- it is validated the same way as before: a pre-registered backtest on 2017-2022, then the 2023+ holdout once (§6).

## 2. Why

Three independent tests of the chart-pattern setups, all on the tuning period, all negative:

| Test | Result | Source |
|---|---|---|
| 0.3h early edge check (spike-grade) | about 0R before costs, -0.08R after | `docs/spikes/edge-check.md`, #112 |
| 3.8 baseline gate, entries at 09:36 | the pool -0.11R (-0.18 to -0.04) over 4,052 trades; EP +0.21R, not distinguishable from zero | `docs/backtests/baseline.md`, #143 |
| The close-entry candidates (#144) | the pool -0.30R (-0.39 to -0.20) over 1,003 trades; every strategy below its 09:36 result | `docs/backtests/close-entry.md` |

The losses come from the entries (breakouts fail within days: 37-56% of trades stopped within 5 sessions), small winners (about +2R against the ~2.7R a 29% win rate needs) and costs (0.13-0.19R a trade). The setups were checked by hand against the README's definitions: the rules are implemented correctly, they just have no edge when run mechanically. No independent study supports VCP / SEPA or CAN SLIM rules run mechanically; technical rules generally don't survive data-snooping and cost adjustments (Sullivan, Timmermann & White 1999; Bajgrowicz & Scaillet 2012).

Momentum is the best-documented anomaly in the literature: Jegadeesh & Titman (1993); across markets and asset classes, Asness, Moskowitz & Pedersen (2013); it survived publication (McLean & Pontiff 2016), though its premium is smaller than it was. Variants with supporting evidence: nearness to the 52-week high (George & Hwang 2004), smooth "frog in the pan" momentum (Da, Gurun & Warachka 2014), and a stop-loss overlay that cuts momentum crashes (Han, Zhou & Zhu, gross of costs). It turns over slowly, so costs matter far less than in the setup pool, and it needs only daily bars.

**Account size is not the reason.** Expectancy is measured per unit of risk, so a bigger account would have lost the same share. $5,000 matters for the economics (running costs of roughly $500-800 a year are 10-15% of it) and for whole-share sizing, not for the edge.

## 3. What the evidence says to expect

Honestly, and before any test: a long-only momentum book has historically earned a few percent a year above the market, with deep drawdowns and occasional sharp crashes, mostly when a beaten-down market rebounds (Daniel & Moskowitz 2016). On $5,000 that is a few hundred dollars a year in a good year, about the size of the running costs. The benchmark every version must beat is the market itself: SPY, and a momentum ETF (for example MTUM), both of which cost almost nothing to hold. The LLM's job is to add value on top of the rule list; whether it can is measured, not assumed (§6).

## 4. What carries over and what changes

**Carries over (built and tested):**
- the data layer: Alpaca bars, the Parquet history since 2016 with the point-in-time universe, EDGAR earnings and fundamentals, news, VIX (1.3-1.6);
- the market calendar, the job runner, the database, the AWS skeleton and the `paper` account (1.4-1.8; the 1.8 acceptance continues unaffected);
- the regime classifier (2.3) as an exposure overlay, if the plan keeps it;
- indicators (2.1), including RS ratings and the trend template, as facts and filters;
- the order layer and its invariants: OTO entries with GTC stop legs, the single-writer lock, `stop_guard`, reconciliation (1.7);
- the guardrails framework, sizing in whole shares and the kill switch (3.5b); the simulator and fill model (3.6), which must be extended to a portfolio (§6);
- the charts (3.4) and the ranking machinery (3.3), as facts for the LLM.

**Changes:**
- **The candidate list:** a momentum ranking of the universe replaces the five setup scans as the pool. The scans can stay as facts for the LLM, not as the pool.
- **Entries:** on the rebalance schedule, not on intraday breakouts. The 09:36 opening-range machinery and two-tranche entries are no longer needed.
- **Exits:** dropping out of the ranking at a rebalance, a protective stop, and whatever rules the plan adopts, replacing the per-trader exit menus of §7.3.
- **The baseline and the scorecard:** the baseline becomes the rule-only momentum book; the verdict scorecard is redefined around the LLM's choices within the list.
- **README** §1 (principles 1 and 5), §5.3 (the scanner), §5.6-§5.9 (the morning, execution and EOD jobs), §7 (entries, plans and exits), §8 (stop widths) and §15 (validation) are rewritten by the plan. Until then they describe the system as built.
- **The execution plan:** Phases 4-7 are re-planned. **No task after 3.8 starts until the plan is approved** (the tracker carries this notice).
- **Fallback F** (the LLM-as-filter test on the old pool, #144) is superseded: the old pool isn't traded.

## 5. Open questions for the plan

Each needs an answer, a source, and (where the sources disagree) a pre-registered comparison. Listed with the usual options, not decided:

1. **Formation period:** 12-1 months (the academic standard: the past year, skipping the last month) or 6 months; nearness to the 52-week high as an alternative or a tie-break.
2. **Universe:** the existing point-in-time universe and its liquidity floor; a price ceiling so whole shares work at $5,000 (a position of $600-1,000 rules out stocks above about $500); whether to exclude the smallest and least liquid names, where costs are highest.
3. **Holdings:** how many (5-10 at $5,000); equal weight or risk-weighted; a sector or cluster cap (the existing cluster guardrail).
4. **Rebalance:** monthly or every 4 weeks, on which session; a buffer so a holding isn't sold until it falls well out of the ranking (turnover control).
5. **Stops:** a wide GTC stop at the broker on every position (CLAUDE.md §3.5), for example 10-15% below the entry, an ATR multiple, or a trailing stop; how it interacts with the rebalance. The stop-loss overlay evidence is gross of costs.
6. **Regime overlay:** whether the regime classifier scales exposure (for example less in `CORRECTION`), and how it handles the rebound crashes momentum is known for.
7. **Earnings:** hold through reports, or let the LLM step aside before them; the existing earnings blackout and cushion rules.
8. **The LLM's role, precisely:** for example choosing N of the top 2N, vetoing names with bad news or accounting red flags, stepping aside before events, sizing within limits. It must stay inside code's list and price levels. What does it see (charts, news, filings, the setup facts)?
9. **Cost and cadence:** monthly decisions use far fewer Claude calls than nightly setup reviews; the LLM budget is re-estimated (README §9, §12).
10. **Validation:** the portfolio simulation (§6), the benchmarks, the gate statistic and how many variants may be tried before the holdout.
11. **What stays of #145:** Phase 4 (the LLM layer) is still needed, but its prompts and schemas change; whether it starts in `dry_run` before the backtest passes.

## 6. How it will be validated

- **Pre-registered before any code runs on real data,** as the 3.8 and close-entry runs were: every rule and threshold from a source or the README, the gate, the comparisons, and what happens on a fail.
- **Tuning period 2017-2022** (2016 is warm-up for a 12-month formation window).
- **The 2023+ holdout has never been opened.** Neither the 3.8 pool nor the close-entry candidates passed, so it is still a clean, single-use test for this strategy. Open it once, for the frozen configuration.
- **A portfolio simulation is needed.** The 3.8 pool scored independent trades; a momentum book is a portfolio (fixed slots, rebalances, cash). 3.8's book-measured comparisons were never built. It must go through the one simulator (`sim/`), whole shares at $5,000, the fill model and the slippage tiers (#141).
- **Measured against the benchmarks** (SPY, a momentum ETF), not only against zero: return, drawdown, and return per unit of drawdown, per year.
- **The LLM's value** is measured forward (the scorecard of its choices against the rule book), because it can't be backtested honestly: the model has read what happened to these stocks in 2017-2022.

## 7. A prompt for the planning chat

> We're on AuraTrade. The user changed the strategy on 2026-10-25 (decision #146): from the chart-pattern setups, which failed three tests, to a relative-strength momentum book. Read `docs/strategy-change-momentum.md` first, then CLAUDE.md, README §1, §2, §5, §7, §8, §15 and the decision log from #135, and the reports `docs/backtests/baseline.md` and `docs/backtests/close-entry.md`. Don't run anything and don't open the 2023+ holdout.
>
> Then write the plan for the momentum book:
> 1. Propose the strategy's rules, answering every open question in its §5 with a source, and flag where the sources disagree.
> 2. Propose the pre-registration for its backtest (§6): the rules, the portfolio simulation, the gate against the benchmarks, the comparisons allowed, and what happens on a fail.
> 3. Propose the README changes and a re-planned execution plan from 3.8 on (what is reused, what is rebuilt, in what order).
>
> Ask me the questions only I can answer. Build nothing until I approve the plan.
