# Strategy review (2026-10-25): a second reading of the 3.8 decision

> **Superseded 2026-10-25** by the strategy change to a momentum book (#146-#149, [momentum-plan.md](momentum-plan.md)). Its recommendation (the close entry, then fallback F) was tested and failed; kept as the record.

**Status.** Written 2026-10-25 for the user and the next session's agent, after reading [strategy-decision.md](strategy-decision.md), the five reports it lists, README §5.3, §7, §8, §15 and #136–#143, CLAUDE.md, and the code behind the numbers (`backtest/pool.py`, `sim/fills.py`, `sim/trade.py`, `journal/scorecard.py`, `strategies/episodic_pivot.py`, `config/risk.yaml`). **Nothing was re-run and the holdout (2023 on) stays closed.** Nothing was built. This page is a review and a recommendation; the plan is the user's to approve.

The short version:

1. **The decision page's findings hold.** The gate failed for real, the losses are made in the first five sessions, the setups are found as the README defines them, costs alone don't explain it, and option C is data snooping. Drop C.
2. **Four disagreements.** (a) The mechanism claimed for option B (momentum's return accrues overnight) is overstated: it bears only on the entry day; B's real hypotheses are a close-of-day confirmation filter, a structural stop and a higher entry price. (b) The page doesn't mention the one piece of existing evidence on buying at the close: comparison #2 says one tranche beats two, so the tranche-2 buys at 15:40 slightly *hurt*. (c) The EP's 09:36 structure is the weakest part of the design and the strongest candidate for a close entry, which also fixes its live data problem. (d) The bootstrap understates the uncertainty, and an EP-only candidate can't pass the gate as written on about 600 trades unless its true edge is about +0.3R.
3. **Missed mechanisms:** the pool selects for tight stops (the width caps and the ranking's tightness component), 09:36 entries buy the wrong side of the overnight/daytime reversal (Akbas et al.), the emerging leader fails differently (small winners, not early stops), and two years carry every positive result.
4. **Missed options:** starting Phase 4 in `dry_run` now, since the Analyst's verdicts can be re-scored under whichever entry wins and the forward evidence is the project's bottleneck; and the LLM-as-filter test (F) as the alternative to D if the restructure fails.
5. **Recommendation:** one new entry mechanism, **confirmation at the 15:40 decision price**, as two pre-registered candidates: the EP bought on the gap session if the gap held, and the scan strategies bought on the trigger session if the breakout held. It reuses the tranche-2 order path (OTO marketable limit with the stop leg), needs only daily bars to backtest, and keeps every hard rule. Odds of a pass: about one in three. If it fails, the user chooses between D and F; no further trials on the tuning period.

## 1. Where the decision page is right

- **The gate failed, and not narrowly.** The pool is -0.11R (-0.18 to -0.04) over 4,052 trades; four of five strategies are negative with intervals below zero; the fifth (EP) is positive but not distinguishable from zero. Survivors-only is -0.08R. The regime variant, the fill model and the universe are as pre-registered.
- **The loss is in the entries.** Every exit variant moves the pool by hundredths of an R (comparisons #3–#7). D8 puts the money where it goes: trades stopped on the entry session lost about 488R (469 × -1.04R) and sessions 2–5 another 1,335R, against the whole pool's deficit of about 415R before the EP fix. Trades that survived 20 sessions made +2.19R each (670 trades). That last figure is survival-conditioned, not a forecast of what skipping the early failures would earn, since some big winners dip first.
- **The setups are implemented correctly** (D9), and the EP look-ahead was real and is fixed: `EpisodicPivot.morning` reads nothing after the gap session's open, and the pool's features for an EP come from the prior close (`known_at`).
- **Costs are a factor, not the lever.** Zero slippage lifts the pool to +0.03R (-0.04 to +0.11): still no pass. Costs weigh most where stops are under 2%, which is a stop-placement problem, not a cost-model problem.
- **Option C is snooping.** The VCP-with-EPS-growth split (+0.15R, -0.04 to +0.34) was found after the fact among about 30 splits; it has a source but no independent test, and it doesn't pass on its own anyway. Keep EPS growth as a fact for the Analyst.
- **The outside evidence is read fairly:** no independent test of VCP/SEPA or CAN SLIM exists; technical rules don't survive data-snooping and cost adjustments (Sullivan, Timmermann & White 1999; Bajgrowicz & Scaillet 2012, who find even low transaction costs erase in-sample profits); the momentum factor is the most robust anomaly and its profits accrue overnight (Lou, Polk & Skouras 2019).

## 2. Where I disagree

### 2.1 The mechanism behind B is overstated

Lou, Polk & Skouras show that for portfolios sorted on past returns, the abnormal return accrues from close to open and the open-to-close component runs the other way, on *every* day held. A swing trade holds overnight and intraday on every session after the first, under either entry. Moving the entry from 09:36 to 15:40 changes only the first day's exposure. So "hold overnight, where momentum earns its return" is not what B tests. B tests three things at once:

1. **a confirmation filter:** only stocks still above the pivot at 15:40 (and, under the existing gate, in the upper half of the day's range) are bought;
2. **a different stop:** structural (or the trigger day's low) instead of the 09:36 low of the day;
3. **a higher entry price:** the breakout day's move is paid, which lowers the R of the same subsequent move and skips the runaway breakouts that close more than 5% above the pivot (the max chase).

(1) and (2) help; (3) hurts. The page's §5 B lists the first two and not the third. The expected result is therefore uncertain, not "the strongest mechanism found". The closest academic support for (1) is indirect: the daytime reversal of overnight gains (Akbas, Boehmer, Jiang & Koch 2022) means a stock that has held its intraday gain into the close has already absorbed the day's selling.

### 2.2 The existing evidence on buying at the close is mildly negative, and the page doesn't mention it

Comparison #2 (one tranche instead of two) is +0.03R better together (+0.02 to +0.04), +0.02R for VCPs and +0.05R for pullbacks. Tranche 2 is exactly a buy at the 15:40 decision price after the confirmation gate passes, at a higher price than tranche 1 with the *same* stop. Its added dollars at risk earned less than tranche 1's. The two-tranche design also half-sizes the entry-day failures that never confirm, and it was still worse. That is direct, if weak, evidence that the confirmation gate's selection doesn't pay for the higher price **when the stop stays at the morning level**. B differs in two ways the comparison doesn't cover: the day-one intraday failures are skipped entirely rather than half-sized, and the stop moves. The page should say so and lean on those two differences, not on the overnight effect.

### 2.3 The EP's day-one structure is the weakest part of the design, and the close entry fixes its live data problem

Of 1,970 EP listings, 625 became trades: 330 were skipped because the 09:36 low of the day sat more than 1.5 × the pre-gap ADR below the trigger, 277 never filled, and of those that filled 64% were stopped within 5 sessions (pre-fix D8). An EP's gap day has a range several times its pre-gap ADR, so a stop at the 09:36 low capped at 1.5 × ADR is placed inside the day's noise by construction. Buying the opening-range high of a 10% gap also buys the top of the overnight move at the moment the daytime reversal starts (Akbas et al.). The strategy still shows +0.21R with an average winner of +3.61R: the winners are large enough to carry a 27% win rate. Entering at 15:40 if the gap has held, with the stop under the gap session's low, trades the drift the EAR/PEAD literature measures (from the close of the reaction day) instead of the first six minutes.

Live, this is also easier than the 09:36 EP, which the page notes has no design yet: at 15:40 the day's volume is known from delayed SIP (15 minutes old), the gap is confirmed, and the Portfolio Manager has reviewed the candidate in the morning as now. The 09:36 IEX-only volume check goes away.

### 2.4 The intervals are too narrow, and the EP can't pass as written on its sample

`block_bootstrap` resamples single entry sessions. Trades entered on neighbouring sessions share the same weeks of market, so the effective sample is smaller than the session count and every interval on these pages is somewhat too narrow. Two consequences:

- the "beyond noise" labels on hundredth-of-an-R comparisons (one tranche, the time stop, the pullback gate) aren't reliable; nothing was adopted from them, so no harm done;
- the EP interval (-0.09 to +0.55, width 0.64R) implies a standard error of about 0.19R. To put the lower bound above zero at 625 trades the mean would have to be about +0.32R; at 1,000 trades about +0.25R. **An EP-only candidate is a coin flip to pass even if its true edge is +0.25R.** The pre-registration should say what happens in that case (§8).

The gate statistic shouldn't change after the fact (that is itself a form of tuning), but a monthly-block bootstrap (21-session blocks) can be added now as a reported secondary before the next run.

### 2.5 Smaller points

- **The missed-entry diagnostic was pre-registered and not reported.** The 3.8 pre-registration lists "the expectancy the missed entries would have had at the limit"; `baseline.md` gives the miss counts (VCP 143 of 1,181, EP 277 of 903) but not that expectancy. It matters for B: the close entry skips the same fast breakouts through the max chase. The next run should report it.
- **"No source supports filtering on the regime score" is right, but the finding is a mechanism, not a curiosity** (§3.3 below).
- **The page's B asks for a close-based soft stop plus a wider hard stop.** That is a new plan component with its own semantics (two stop levels per position). The simpler form, one hard stop at the broker at the trigger session's low, keeps the plan model as it is (§7).

## 3. What was missed: mechanisms

### 3.1 The pool selects for tight stops

Two parts of the design pull the pool towards the stops that lose:

- **The width caps reject the wide ones.** `STOP_WIDTH` rejections: VCP 2,097, pullbacks 2,666, emerging leaders 3,632, against 1,026, 1,513 and 758 trades. Roughly two of every three listed VCPs and pullbacks are rejected because their structural stop is too far below the entry. What is left is the tight third.
- **The ranking rewards tightness.** Tightness (stop width ÷ ADR, the tightest scoring 1) is one of the eight equal-weight components of the quality score (#138), so the capped list leans towards tight stops as well.

D3 then shows what that costs: stops under 3% are -0.18R (1,279 trades), stops of 7% or more +0.17R (257), the best bucket ahead in 5 of 6 years; for pullbacks the under-3% bucket is -0.36R on 702 trades, nearly half of all pullbacks. VCPs are the exception (under 3% was their best bucket, +0.12R on 231). The sources don't say "the tighter the better"; Kullamägi's ADR rule and Minervini's 8% are *maximums*. A close entry with the trigger day's low as the stop changes this for EPs and pullbacks; the ranking component is a separate question for the user (it was their choice in #138, and changing it now is tuning unless pre-registered as a comparison).

### 3.2 The 09:36 entry buys the wrong side of a known intraday pattern

Besides Lou, Polk & Skouras, Akbas, Boehmer, Jiang & Koch (JFE 2022) document that positive overnight returns are followed by daytime reversals, which they attribute to overnight noise traders and daytime arbitrageurs. The pool's EP and opening-range entries buy a stock that has just made a large overnight or opening move, in the first six minutes, with a stop inside that morning's range. The 469 entry-session stop-outs are what that pattern looks like in a trade log.

### 3.3 The regime score is a late-cycle signal for breakouts

The higher the score the evening before, the worse the breakout (score ≤ +2: -0.03R; ≥ +5: -0.36R; informative for VCPs and pullbacks in 4 of 4 and 5 of 5 years). A +5 reads: VIX under 20, breadth over 60%, at most 3 distribution days, leadership health above 55%, both indexes in full trend. That is an extended market where breakouts are crowded and the next move is distribution. O'Neil's own warning (tops come when everything looks best, and the distribution count is the tell) is the same observation. The page is right not to filter on it without a source. It is still the main reason the pool loses *inside* `CONFIRMED_UPTREND`, and the Analyst and Portfolio Manager prompts should carry the pillar scores as facts so the LLM can act on it, which §5.6 already provides for.

### 3.4 The emerging leader fails in a different way

`EMERGING_LEADER` is -0.21R with only 15% stopped within 5 sessions, 2% of losers ever at +1R, and an average winner of +0.80R. It doesn't fail early; it goes nowhere. Its 758 trades come from 161 entry sessions (all `EARLY_UPTREND`, which is 16% of sessions and whose 21 spells failed into `CORRECTION` 12 times). The close entry doesn't address small winners. Whatever the next run shows for it, its sample is a few spells, not 758 independent trades.

### 3.5 Two years carry every positive result

The pool is positive in 2017 (+0.09R) and 2020 (+0.30R) and negative in the other four; EPs make +0.86R in 2020 (132 trades) and are negative or flat in 2018 and 2022. A candidate that passes the gate on the strength of 2020 alone is a 2020 bet. The pre-registration should add a per-year condition (§8).

### 3.6 Things that look informative and shouldn't be tuned on

Entry price $10–20 (+0.23R, 6 of 6 years), extension ≥ 25% above the 50-day (+0.52R; mostly EPs and momentum, and the EP numbers there predate the fix), ADR 5–8% (+0.36R). Each is one of about 30 splits read after the fact. None has a source as a filter. They belong in the Analyst's facts, not in the pool.

## 4. What was missed: options

### 4.1 Start Phase 4 in `dry_run` without waiting for the gate

The gate sits before Phase 4 to avoid spending Claude tokens on a hopeless pool. The cost at stake is about $20–40 a month (0.3g: about $0.016 an Analyst request; README §12). What the delay costs is forward evidence: the power analysis (§15) says the verdict scorecard needs on the order of 250+ triggered setups per group, a year or more at the expected rate, and that evidence is the only thing that can show whether the LLM adds value. Verdicts are policy-independent (BUY/WATCH/SKIP on the evening scan); the scorecard replays each setup through `sim/` under the default entry policy, so verdicts collected now can be re-scored under whichever entry policy the restructure adopts. Moving the gate from "before Phase 4" to "before capital" (Phase 5's live wiring and the paper trial) keeps its purpose and starts the clock. This changes README §15 and needs the user's approval; it does not touch the hard rules.

### 4.2 The LLM-as-filter test (F): the alternative to D if the restructure fails

README §15 says the LLM "can't be expected to rescue a pool with negative expectancy". That was a design assumption; the diagnostics say something more specific: a crude eight-component score already separates rank 1–10 (+0.03R) from 11–20 (-0.23R) in 6 of 6 years, and the top quarter by score from the rest by 0.14R. The pool has dispersion that selection can reach. F makes the project's thesis the explicit test: run the evening pipeline and the scorecard in `dry_run` on the best available pool, with the bar set in advance that triggered BUYs must beat triggered SKIPs by more than the pool's deficit plus δ (0.1R). It risks no capital, costs about $30 a month, and takes 6–12 months to read. Its weakness is the bar: a -0.1R pool needs a +0.2R BUY–SKIP spread, which is at the edge of what the trial can detect. F is a better fit for the project's purpose than D, whose LLM has few decisions to make (§4.4).

### 4.3 Next-day entry after a confirmed close (G): considered, not recommended

Enter at 09:36 on the session *after* a close above the pivot, with the stop under the breakout day's low. It keeps the 09:36 machinery and needs no 15:40 entry path. But it pays the overnight gap on top of the breakout day's move, holds the next day's intraday session (the wrong side again), and has no source behind it. The 15:40 entry dominates it.

### 4.4 On D (the relative-strength momentum book)

The evidence for D is the strongest of any option (the momentum factor; 52-week-high nearness, George & Hwang 2004; smooth-path momentum, Da, Gurun & Warachka 2014; a stop-loss overlay cutting momentum crashes, Han, Zhou & Zhu, gross of costs). It keeps every hard rule: a wide GTC stop at the broker satisfies the invariant, whole shares at $5,000 work for stocks under about $250, and it needs only daily bars. Two things count against it here. It changes what the system is (README §1, §5, §7 rewritten; the build is large, including the portfolio simulation 3.8 never built). And it gives the LLM a handful of decisions a month, so the question the project exists to answer, whether the LLM adds value, would take years to read. D is the right fallback if the user's goal shifts from "test the LLM trader" to "run a system with a known edge".

## 5. The recommendation

**One new entry mechanism, confirmation at the 15:40 decision price, as two pre-registered candidates**, run once on 2017–2022:

| Candidate | What changes | What stays |
|---|---|---|
| **1. EP at the close** | The gap session is bought at the 15:40 decision price if the gap has held (price at or above the open, in the upper half of the day's range, day volume on pace for the evening rule's 3 ×). Hard stop = the gap session's low, at most 8% below the limit. | The Kullamägi exit set, the earnings flag as a fact, the 08:40 screen and the Portfolio Manager's morning EP review (5.6). The 09:36 proxy, the 1.5 × pre-gap ADR cap and the 25% opening-volume check go away. |
| **2. Scan strategies at the close** | VCP, pullback, emerging leader and momentum are bought at the 15:40 decision price on the trigger session if the strategy's existing confirmation gate passes (price between the level and level × 1.05, upper half of the range, the gate's volume and RS-line conditions). Hard stop = the higher of the structural stop and the trigger session's low, under the strategy's existing cap measured from the limit. One tranche. | The scans, the ranking, the cap, the regime, the plans, the guardrails, the fill model, the slippage tiers. |

Both candidates are judged by the pre-registered gate, overall and per strategy; a strategy that fails stays out of the fundable lists. Whatever passes on tuning is frozen with the 5-pillar regime and the holdout is opened once.

**Why this and not the page's B as written:** it is the same idea with the parts that cost build and add semantics removed. The order path exists (tranche 2 is an OTO marketable limit at the decision price with its own GTC stop leg; the simulator fills it already, `sim/trade.py::_tranche2`). The gate exists (`plans.entry.confirmation_gate`, thresholds in `risk.yaml`). The stop is one broker level, not a soft level plus a hard level. The backtest needs no minute bars (the decision price is the close, as for every later session), so a run takes minutes and candidate 1 doesn't depend on SIP minute coverage.

**Build:** about 2–3 sessions for the backtest (an entry policy with trigger `CLOSE`, the sim's entry at the decision time via the tranche-2 code, `pool.reachable` reading the close instead of the high, the stop-width reference, tests with known answers, the report), plus the README and #144. The live path is Phase 5 work that was due anyway, and it shrinks: no opening range, no 09:51 run, entries placed by the 15:40 job.

**Risks:** the entry pays the day's move and skips breakouts closing more than 5% above the pivot; the stop-width rejections may rise for VCPs (the structural stop is further from a close than from the pivot); live, the IEX spread cap skips small caps at 15:40 (§7.4); PEAD is weak after 2017 outside small caps; EPs are few (about 100 a year in the backtest, fewer live with the earnings-or-news requirement).

**Expected result (my estimate, not a measurement):**

| | Point estimate | Chance of passing the tuning gate |
|---|---|---|
| EP at the close | +0.15 to +0.35R on 900–1,200 trades | about 40% (a sample-size problem as much as an edge problem, §2.4) |
| VCP at the close | -0.05 to +0.10R | about 35% |
| Pullback at the close | -0.10 to +0.05R | about 20% |
| Emerging leader, momentum | no reason to expect a change | under 10% |
| The pool as a whole | around 0 ± 0.1R | about 30% |

A pass needs roughly +0.08R on 2,500 trades (the lower bound sits about 0.06–0.08R under the mean at that size).

**If nothing passes:** no further runs on the tuning period. The user chooses D or F (§4). My order of preference: F, then D, then pause, because F answers the question the project asks and D changes the question.

**In parallel, if the user agrees (§4.1):** start Phase 4 in `dry_run` on the evening scan now; re-score the verdicts under the adopted entry policy when it is known.

## 6. The decision matrix

| Option | Evidence | Build | Keeps the hard rules | LLM decisions to score | Expected |
|---|---|---|---|---|---|
| A: EP at 09:36 (as is) | +0.21R, not distinguishable from zero; live volume check undesigned | none | yes | few (EPs only) | fails as is |
| **A′: EP at the close** | EAR/PEAD from the reaction day's close; the gap-day low as the stop | small | yes | few | positive estimate likely; the gate is a coin flip |
| B (page): close entry, soft structural stop + wider hard stop | indirect (Akbas et al.; the early-failure anatomy) | medium | yes, with a new plan component | many | uncertain, 0 ± 0.1R |
| **B′ (this page): close entry, one hard stop at the trigger day's low, the existing gate** | same as B | small–medium | yes, on the tranche-2 path | many | same as B, cheaper and simpler |
| C: VCP filters | none independent; found after the fact | small | yes | many | snooping; drop |
| D: momentum book | strongest (factor, 52-week high, FIP, stop overlay) | large; a different system | yes, wide stops | few; years to read | positive but modest; crashes |
| E: pause | | none | | none | nothing learned |
| **F: LLM-as-filter in `dry_run`** | the pool's dispersion (D4); the project's thesis | Phase 4 as planned | yes (no capital) | many | readable in 6–12 months; a high bar |
| G: next-day entry | none | small | yes | many | dominated by B′ |

## 7. The design of the close entry, against the hard rules

**7.1 Order path (stops at the broker).** The entry is an **OTO marketable limit buy with a GTC stop leg**, placed by the 15:40 job once the gate passes: the same order the tranche-2 add uses today (#113, #114; `marketable_limit_slippage` 0.5%). The position is protected from its fill. A parent unfilled at the close is cancelled by `post_market` as now; a partial fill is handled by the existing rule (the unfilled rest cancelled, the leg kept or a standalone GTC stop placed). Between 16:00 and `post_market` a stop can't trigger anyway (stops don't work in extended hours), so the exposure window is unchanged.

A **limit-on-close** order is not an option: Alpaca's docs require `time_in_force` of `day` or `gtc` for bracket and OTO orders, and `cls` orders are rejected after 15:50, so a LOC entry would fill at 16:00 without a stop leg and need a stop placed after the close. That adds a code path and breaks the letter of the invariant for the after-hours window. Recommend the marketable limit (the page's §7 question 2).

**7.2 Sizing and price levels (code decides).** Sized on the limit (decision price × 1.005), whole shares floored, 0 shares skipped. The stop is a code level (the trigger session's low, floored by the structural stop); the Analyst may only tighten it, as now. The pivot stays the code pivot; the max chase (level × 1.05) is applied to the decision price.

**7.3 Fail closed.** No decision price at 15:40 → no entry (the existing rule for adds). Half days: no entries (existing). The calendar, data-integrity and LLM-failure rules are untouched. The LLM still decides in the morning (which setups, which plan, the EP review); code confirms at 15:40.

**7.4 Free data tiers.** The 15:40 price is the IEX trade or midpoint under #120; day volume is delayed SIP projected by the pace curve (live) and the full-day volume (sim, #142). The 10-23 finding is the live risk: at 15:40, small and mid caps often have an IEX spread over 2% and so no decision price; the entry is skipped. Three ways to handle it, for the user:

- (a) accept it: the live pool is the names with tight IEX quotes (roughly the top third of the universe by liquidity);
- (b) for **entries only**, fall back to the delayed SIP last trade (15 minutes old plus the 2-minute freshness rule) as the reference, with the marketable limit still bounding the fill at +0.5%: a stale reference can only make the order miss, never fill worse than the limit. Soft exits keep #120 unchanged. Recommended;
- (c) raise `max_spread` for entries. Not recommended: a 10% IEX spread midpoint is not a price.

The backtest can't model IEX quotes (no quote history). Report the ≥ $100M dollar-volume tier separately as the proxy for the names that have a decision price live.

**7.5 Whole shares at $5,000.** An 8% stop at 1% risk is a $625 position; at 0.5% risk, $312. Shares are floored as now; the skip and single-tranche counts are reported. The close entry has one tranche, so the "under 2 shares" rule is moot.

**7.6 What is no longer needed if B′ is adopted:** the opening-range trigger, the 09:51 delayed-SIP run (#109), the `LOW_OF_DAY` stop at 09:36, the EP's 09:36 volume check and its 1.5 × ADR cap, two-tranche entries. The 09:36 job remains only for the invariant check until Phase 5 reorganizes the schedule.

## 8. Pre-registration proposal (for the user to approve before any code runs on real data)

Everything below is fixed before the run. A change after the run is listed in the report with its reason.

### 8.1 Candidates (two, no more)

**Candidate 1: `EPISODIC_PIVOT` at the close.**
- **Listing:** the gap session's after-close view is not used for the entry. A candidate is a security with open ≥ 10% above the prior close (`min_gap`), listed on the gap session's capped list on the evening-before regime (the existing residual stays, and is counted).
- **Gate at the decision time (15:40; the close in the backtest):** decision price ≥ the gap session's open (the gap held); the day's range position ≥ 0.5 (upper half); the day's volume ≥ 3 × the prior 50-session average (`min_volume_ratio`, the evening rule; the full day in the sim, the pace projection live).
- **Entry:** a marketable limit at the decision price × (1 + 0.005), filled at the decision price plus the liquidity-tier slippage (the tranche-2 fill rule). No fill above the limit.
- **Hard stop:** the gap session's low, floored by the structural stop (the prior close). **Cap: 8% below the limit** (`LIMIT_PCT`, Minervini's and O'Neil's maximum loss). A wider stop is rejected, not tightened. The rejection rate is reported.
- **Plan:** the Kullamägi set as it is (⅓ at session 3, breakeven after the partial, the 10-day trail), the safety rules, one tranche.
- **Sizing:** $5,000, the regime's base risk, multiplier 1, whole shares.

**Candidate 2: the scan strategies at the close** (`VCP_BREAKOUT`, `LEADER_PULLBACK`, `EMERGING_LEADER`, `MOMENTUM_BREAKOUT`).
- **Listing:** unchanged (the evening scan, the ranking, the cap, the regime).
- **Gate at the decision time on the trigger session** (the session after the scan): the strategy's existing confirmation gate on the existing level: `BREAKOUT` for VCP and emerging leaders (price between the pivot and pivot × 1.05, range position ≥ 0.5, volume ≥ 1.4 × the 50-day average, RS line near a high); `PULLBACK` for pullbacks (price between the trigger and trigger × 1.05, range position ≥ 0.5, volume ≥ 1.0 ×, above the tested MA, RS line not at a 50-session low). Momentum uses `BREAKOUT` on its consolidation high (it has no gate today; `BREAKOUT` is the source rule for a breakout). A unit is attempted on each session the scan lists it, until it fills, as now.
- **Entry:** as candidate 1.
- **Hard stop:** the higher of the structural stop and the trigger session's low, under the strategy's existing cap **measured from the limit** (VCP and emerging 8%; pullback 1.5 × ATR; momentum 1 × ADR). The base-count, earnings-blackout, 50-day-extension and price-level guardrails apply unchanged.
- **Plan:** each strategy's single-trader set, one tranche.

Both candidates run in one invocation of `backtest.baseline pool` on 2017–2022 under the chosen regime `cu+2_corr-2_c2_dist8`.

### 8.2 The gate (unchanged, with two additions fixed now)

- **Primary (as pre-registered in 3.8):** the session-block 90% interval of expectancy excludes zero from above on the tuning period, overall and per strategy; the holdout point estimate is positive.
- **Addition 1, per-year robustness:** a passing strategy must be positive in at least 4 of the 6 tuning years, and its expectancy without its best year must stay at or above zero. A strategy that passes the interval but fails this is reported as "passes on one year" and is not adopted.
- **Addition 2, a reported secondary statistic:** the same interval with 21-session blocks (about a month), reported beside the primary. It doesn't decide; it tells the reader how much the primary flatters.
- **Minimum sample:** a strategy with fewer than 300 closed trades can't be adopted on this run whatever its interval (momentum will not reach it).

### 8.3 Diagnostics (reported, never tuned)

Per strategy: the gate pass rate at the decision time and which condition fails most; the stop-width rejection rate and the distribution of stop widths; the entry price above the level (the pay-up) and the share skipped by the max chase; the expectancy of those skipped at the limit (the 3.8 diagnostic that wasn't reported); the ≥ $100M dollar-volume tier apart (§7.4); results by year and by regime; the whole-share skips; and the two comparisons below.

### 8.4 Comparisons (two, pre-registered; adopted only by the existing "both periods" rule)

1. **The gate's volume condition off** (price and range only): measures how much the IBD volume rule costs or earns at the close.
2. **The stop at the structural level** instead of the trigger session's low: measures the stop choice. (D3 was mixed between strategies; this settles it per strategy on the pre-registered basis.)

### 8.5 How the result is read, fixed now

- **A candidate passes:** freeze it and the regime in `risk.yaml`; open the holdout once; adopt the fundable lists from what passes on both periods. Phase 4 proceeds (or continues, §4.1).
- **A candidate has a positive point estimate on tuning but fails the interval** (the likely EP case): it is **not** adopted into the fundable lists from the backtest. If the user wants it traded, the pre-registered path is: fund it at half the regime's base risk and at the `UNDER_PRESSURE` conviction bar (top quarter) only, as a *starter* strategy, and judge it on the scorecard after 100 triggered live-scored setups. This is the user's decision, to be taken now, not after the run.
- **Nothing passes:** stop. No further runs on the tuning period. The user chooses D or F or pause.
- **The holdout:** opened only if something passes on tuning, once, for everything at once (the regime's checks, the gate's holdout estimate, the sign of each adopted comparison).

### 8.6 What is not pre-registered and therefore not allowed

Changing the gate thresholds, the slippage tiers, the ranking weights, the universe filters or the plan defaults on the strength of this run. Any of those would be a third candidate.

## 9. Questions for the user

The page's §7 questions, with my answers, plus three new ones:

1. **Which option?** B′ with A′ (the two candidates above). Drop C. D or F as the fallback, chosen now (§8.5).
2. **Limit buy at 15:40 or limit-on-close?** The marketable limit OTO with the stop leg. Alpaca won't attach a stop leg to a `cls` order, so a LOC would fill unprotected.
3. **The small-cap tail at 15:40?** Keep it in the universe; for entries only, fall back to the delayed SIP last trade as the reference price with the marketable limit bounding the fill (§7.4 b). Soft exits keep #120.
4. **How many candidates?** Two, as above; the two comparisons in §8.4 are the only variants.
5. **New: start Phase 4 in `dry_run` now** (about $20–40 a month, no capital), with the gate moved to "before capital" in README §15? Recommended.
6. **New: the EP's "positive but not passing" path** (§8.5): fund as a starter strategy at half risk, or keep it out until the scorecard speaks?
7. **New: the ranking's tightness component** (§3.1): leave as chosen in #138, or pre-register "tightness off" as a comparison on the next run (it would be a third variant, so I recommend leaving it and letting the Analyst see the width as a fact)?

## 10. What happens next (if approved)

1. The next session writes the pre-registration as `docs/backtests/close-entry-preregistration.md` from §8, with every threshold's source, and records the design as #144 in the README (§7.1 entries, §7.3, §5.7 the entry order at 15:40, §15 if the gate moves).
2. Build: the `CLOSE` entry policy (config, `plans/entry.py`, `guardrails` stop-width reference from the limit, tests at 100% branch coverage), the simulator's entry at the decision time through the tranche-2 code, `backtest/pool.py` for both candidates, the report.
3. One run on 2017–2022; the report; the user reads it with the rules of §8.5.
4. In parallel, if approved: Phase 4.1–4.6 in `dry_run`.
5. Either way: the `paper` account's real fills from the 1.8 acceptance against `sim.slippage`, as a measurement.

## 11. Sources (checked 2026-10-25)

- Lou, Polk & Skouras, [A Tug of War: Overnight Versus Intraday Expected Returns](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf), JFE 2019 ([LSE record](https://researchonline.lse.ac.uk/id/eprint/119010)): momentum and reversal profits accrue entirely overnight, with intraday profits of the opposite sign, across 14 strategies including earnings momentum.
- Akbas, Boehmer, Jiang & Koch, [Overnight returns, daytime reversals, and future stock returns](https://academicnewsletter.sufe.edu.cn/info/355173), JFE 2022: positive overnight returns followed by daytime reversals; the intraday session trades against the overnight move.
- Bogousslavsky, [The cross-section of intraday and overnight returns](https://finance.unibocconi.eu/sites/default/files/files/media/attachments/The%2520Cross-Section%2520of%2520Intraday%2520and%2520Overnight%2520Returns_21Dec1620170112151545.pdf), JFE 2021: anomaly returns split between day and night; size and illiquidity premia in the last 30 minutes.
- Earnings announcements in China, [overnight-intraday disparity](https://ideas.repec.org/a/eee/corfin/v82y2023ics0929119923001207.html), JCF 2023: the overnight period reacts to earnings news, the intraday session trades against it, and only overnight abnormal returns predict future returns.
- Welch, [Rest in Peace Post-Earnings Announcement Drift](https://cfr.ivo-welch.info/published/papers/martineau2021rest.pdf) (as cited by the decision page): the drift is gone in large caps, more persistent in microcaps.
- Bajgrowicz & Scaillet, [Technical trading revisited: false discoveries, persistence tests, and transaction costs](https://smallake.kr/wp-content/uploads/2017/12/SSRN-id1095202.pdf), JFE 2012: even low transaction costs offset the in-sample performance of technical rules; no ex-ante selection of future winners.
- George & Hwang, [The 52-week high and momentum investing](https://academicnewsletter.sufe.edu.cn/info/361244), JF 2004; [Alpha Architect summary](https://alphaarchitect.com/the-secret-to-momentum-is-the-52-week-high/).
- Da, Gurun & Warachka, Frog in the Pan: Continuous Information and Momentum, RFS 2014; [Alpha Architect summary](https://alphaarchitect.com/2015/11/frog-in-the-pan-identifying-the-highest-quality-momentum-stocks/): momentum of 5.94% for continuous-information stocks against -2.07% for discrete ones.
- Han, Zhou & Zhu, Taming Momentum Crashes: A Simple Stop-Loss Strategy; [Alpha Architect](https://alphaarchitect.com/2016/08/taming-the-momentum-roller-coaster-fact-or-fiction/), [CXO](https://www.cxoadvisory.com/?p=25071): a 10–15% stop overlay cuts the worst month from about -50% to about -11% (equal-weighted), gross of costs; costs could erase much of it.
- Alpaca, [Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca): bracket and OTO orders need `time_in_force` `day` or `gtc`; `cls` orders are rejected after 15:50 ET and execute only in the closing auction; stop orders are not eligible for extended hours.
- The decision page's own sources (§9 there) stand; nothing found contradicts them.
