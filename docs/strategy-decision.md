# The strategy decision after the failed baseline gate (task 3.8)

**Superseded 2026-10-25.** The decision is made: see [strategy-review-2026-10-25.md](strategy-review-2026-10-25.md) (the second reading), [strategy-execution-2026-10-25.md](strategy-execution-2026-10-25.md) (the plan of work), [backtests/close-entry-preregistration.md](backtests/close-entry-preregistration.md) and decisions #144 and #145. This page is kept as the record of what was known on 2026-10-24.

**Status 2026-10-24.** Task 3.8's baseline gate failed. The diagnostics are done, one look-ahead bug is fixed (#143), and the EP proxy has been re-run: EPs are +0.21R but not distinguishable from zero, so every strategy fails. **Phase 4 is blocked until the user decides how the strategy changes.** This page is the handoff for that decision. It is written for the next session's agent and for the user. It says what was found, what the outside evidence says, which options exist, what each would cost and risk, and the plan of action we recommend.

Branch `phase-3/baseline-backtest` (merged to `main` at the end of the 2026-10-24 session). The holdout (2023 on) is **still closed**.

## 1. What to read first

| File | What it holds |
|---|---|
| This page | The decision, the options, the plan |
| [docs/backtests/baseline.md](backtests/baseline.md) | The gate, per strategy, per year and per regime, the 19 pre-registered comparisons, with the EP proxy fixed (#143) |
| [docs/backtests/baseline-diagnostics.md](backtests/baseline-diagnostics.md) | Why the pool loses: costs, the splits, the anatomy of the losses, 12 charts read by eye. **Its EP numbers predate the fix (#143) and look ahead** |
| [docs/backtests/baseline-diagnostics-preregistration.md](backtests/baseline-diagnostics-preregistration.md) | What the diagnostics and the EP fix were committed to before they ran |
| [docs/backtests/baseline-preregistration.md](backtests/baseline-preregistration.md) | 3.8's own pre-registration: the gate, the comparisons, the holdout rule |
| [docs/backtests/regime-5-pillar.md](backtests/regime-5-pillar.md) | The 5-pillar regime run (variant `cu+2_corr-2_c2_dist8`, not yet frozen in `risk.yaml`) |
| [docs/spikes/edge-check.md](spikes/edge-check.md) | Spike 0.3h: the early warning that these setups were about breakeven before costs |
| README §5.3, §7, §8, §15, §16 (#136-#143) | The strategies, plans, guardrails, backtest validity rules and the decisions behind them |
| CLAUDE.md §3 | The hard rules every option must keep |

## 2. What was found

**The gate.** On 2017-2022, every ranked setup played under its default plan after slippage (README §15):

| Pool | Trades | Expectancy (R) | 90% interval | Gate |
|---|---|---|---|---|
| All (EP look-ahead, before #143) | 4,154 | -0.10 | -0.16 to -0.03 | fails |
| The four non-EP strategies (second run) | 3,407 | -0.17 | -0.23 to -0.12 | fail |
| `VCP_BREAKOUT` | 1,023 | -0.03 | -0.14 to +0.07 | fails |
| `MOMENTUM_BREAKOUT` | 130 | -0.51 | -0.68 to -0.30 | fails |
| `LEADER_PULLBACK` | 1,500 | -0.22 | -0.31 to -0.14 | fails |
| `EMERGING_LEADER` | 754 | -0.21 | -0.28 to -0.14 | fails |
| `EPISODIC_PIVOT`, **fixed (#143)** | 625 | **+0.21** | -0.09 to +0.55 | fails |
| All, **fixed (#143)** | 4,052 | -0.11 | -0.18 to -0.04 | fails |

**Why it loses** (baseline-diagnostics.md):
1. **Costs** take +0.13R a trade (0.12-0.14). At zero slippage the pool is +0.03R (-0.04 to +0.11): still no pass. Costs weigh most on tight stops: a quarter of momentum, pullback and EP stops are under 2% wide (the 09:36 low of the day sits next to the trigger), and those trades average -0.5 to -0.9R. For pullbacks, stops under 3% are 0.50R worse than 5-7%, in 6 of 6 years.
2. **Early failures.** 39% of trades are stopped out at a loss within 5 sessions, 469 on the entry day itself. Only 12% of losers had closed at +1R first, so the exits aren't giving gains back; every exit variant moved expectancy by hundredths of an R.
3. **The win rate is normal, the winners are small.** 29% win at +1.98R against -0.94R. Kullamägi reports a 25-30% win rate too; his method needs an average winner of about 2.7R or more.
4. **The setups are found correctly.** 9 non-EP charts drawn at random match the README's definitions. This is not an implementation error.
5. **The regime works backwards for breakouts:** the higher the score the evening before, the worse the trade (<= +2: -0.03R; >= +5: -0.36R), informative for VCPs and pullbacks. No source supports filtering on it.
6. **What leaned positive:** VCPs with growing earnings (+0.15R, -0.04 to +0.34, 397 trades), the top 10 of the ranking (+0.26R over ranks 11-20, though partly EPs).
7. **The EP bug (#143).** The old EP proxy admitted a gap on its full-day volume and ranked it on the gap session's close, while entering at 09:36. Fixed: the 09:36 view (the gap at the open, >= 25% of the average daily volume in the first 6 minutes, features from the prior close). **Result:** +0.21R (-0.09 to +0.55) over 625 trades, a fail by the pre-registered rule. The point estimate barely moved from the look-ahead version (+0.23R); the interval widened. It is positive in 4 of 6 years, but 2020 (+0.86R) carries much of it, and it is flat in `CONFIRMED_UPTREND` (+0.01R, 525 trades) against +1.46R in `EARLY_UPTREND` (91 trades). Most listed EPs never trade: of 1,970 listings, 330 were skipped because the low of the day sat more than 1.5 x the pre-gap ADR below the trigger, and 277 never filled.

## 3. What the outside evidence says

Searched 2026-10-24 (sources in §9). The short version: **intraday breakout entries are where the evidence is weakest; the overnight effect and the short post-earnings window are where it is strongest.**

- **Overnight vs intraday (Lou, Polk & Skouras, JFE 2019).** Momentum profits accrue **overnight**; the intraday component runs the other way (institutions trade against momentum during the day). Robust across subsamples, stronger in large and high-priced stocks. Our pool buys at 09:36 and so takes the intraday side.
- **Post-earnings drift.** Real but much weaker: near zero overall after 2017 in one study, gone in large caps since about 2006 in another (a preprint). The drift in the **first few days** after the announcement has grown stronger. The price reaction around the announcement (what an EP measures) predicts the drift (Brandt et al.). Strongest in small caps, where costs are highest.
- **Momentum (the factor).** The most robust anomaly; it didn't decay after publication (McLean & Pontiff), but its premium roughly halved after 1994 (10.9% to 5.5% a year), it crashes, and turnover costs bite.
- **Chart patterns and CAN SLIM.** No independent, peer-reviewed test of Minervini's VCP or SEPA was found. CAN SLIM's evidence is in-sample (a 2013 study over 1999-2013) plus an unverified paper-trading test. Sullivan, Timmermann & White (1999): no technical rule is significantly profitable once the number of rules tried is accounted for.
- **The traders.** Their methods are discretionary, concentrated and sized up in hot markets. Among active traders, fewer than 1% are persistently profitable after costs (Barber, Lee, Liu & Odean, Taiwan). The famous ones are the visible survivors; their rules run mechanically are not evidence of an edge, and our backtest agrees.
- **LLMs picking stocks.** Thin evidence: a 10-month live study found plain prompts insignificant, structured prompts with human oversight better. Nothing says an LLM can turn a losing pool into a winning one.

## 4. The constraints every option keeps

From CLAUDE.md §3 and the decision log; don't trade these away without the user:
- **The LLM decides; code enforces limits** (#2; "deterministic-first" was rejected). Every option below changes the *pool and the mechanics* the Analyst and Portfolio Manager work with, not that split. Price levels come from code.
- **Protective stops always rest at the broker**, covering the full quantity; a partly filled buy is unprotected. No halt cancels a stop.
- **Fail closed** on missing data (no decision price, no calendar, LLM error: no new entries).
- **$5,000 paper capital, whole shares** (#122): position sizes of $500-1,500; one or two shares of expensive names.
- **Free data tiers:** IEX real-time only; SIP 15 minutes delayed (#109, #110); intraday volume is delayed; the 15:40 decision price is the last IEX trade or the IEX midpoint, skipped when the IEX spread is over 2% (#120, #131). On 2026-10-23, 5 of 10 small-cap signals had no decision price for that reason (docs/next-steps.md "Finding (10-23)").
- **Budget** under $150/month; no paid calls without the user's OK.
- **Backtest validity (README §15):** point-in-time universe, the one simulator (`sim/`), tune on 2016-2022, **open 2023+ once**.
- **The design is frozen (#105):** changes come from backtest findings or the user. This is both.

## 5. The options

### A. Earnings gaps over the short post-announcement window (EP / PEAD)

- **Idea:** trade only gaps on earnings reaction sessions (the README's live EP requires earnings or news; the proxy keeps the earnings flag as a fact), hold days to a few weeks, sell a third into the first 3-5 days of strength (Kullamägi's own exit, already the default `KULLAMAGI` set).
- **Evidence:** moderate (the EAR/drift literature), concentrated in the first days, decaying.
- **The fixed result** (§2): +0.21R, not distinguishable from zero. Most listed EPs never trade at 09:36 (the morning low too far below the trigger, or no fill). A natural variant joins A to B: **enter the gap session at its 15:40 decision price if the gap has held**. The announcement-window reaction the research uses is then known legitimately (by 15:40, not the full-day look-ahead #143 removed), the stop sits under the gap session's low, and the position holds overnight.
- **Build:** small. The proxy, plans and simulator exist; the earnings flag exists. Live EPs are task 5.6 (pre-market data 15 minutes old, IEX volume only at 09:36: the live 09:36 volume check needs its own design).
- **Risks:** small-cap costs; few trades (about 1 a day before the fix); live detection is harder than the backtest's.

### B. The same scans, entered at the close (recommended main candidate)

- **Idea:** keep the evening scan and the ranking. On the trigger session, enter only if the stock is still above its pivot at the **15:40 decision price**, rather than buying the 09:36 break. The stop is structural (under the base or the pullback low) on a closing basis plus a wider hard stop at the broker, not the morning's low. Hold overnight, where momentum earns its return.
- **Evidence:** the strongest mechanism found (overnight vs intraday). It targets what we measured: entry-day failures, tight intraday stops, costs per unit of risk.
- **Build:** medium.
  - A new entry policy (`plans.entry` / `execution.triggers`): a 15:40 trigger (a limit buy at the decision price, or a limit-on-close order), with the broker stop placed once filled. The order layer must keep the invariant that a filled position is protected before the session ends (CLAUDE.md §3.5): design the OTO/after-fill stop path carefully.
  - The fill model (`sim/fills.py`) already fills soft exits at the decision price; entries at the decision price plus slippage follow the same rule.
  - The guardrails' stop widths per strategy (README §7.1) change for this entry.
  - README §7.1 and the decision log change.
- **Risks:** the IEX spread cap skips many small caps at 15:40 (the 10-23 finding), so live may trade a narrower universe than the backtest; it may only shrink the loss.

### C. Stricter VCP filters (earnings growth, top ranks, wider stops): not recommended

- **Evidence:** none independent; the filters would be chosen after seeing 2017-2022 (data snooping).
- Keep the *facts* (EPS growth, rank) for the Analyst; don't make them pool filters on this evidence.

### D. A relative-strength momentum book (the fallback)

- **Idea:** hold the strongest 5-10 stocks over 6-12 months, rebalance monthly; the Analyst picks within the top list and steps aside before earnings or bad news; the regime layer sets exposure.
- **Evidence:** the most robust of all, but a smaller premium than before and crashes.
- **Build:** large. It needs the baseline book's portfolio simulation (not built: 3.8's book-measured comparisons are still open), a rebalancing job, and a stop policy that keeps the broker-stop invariant with wide stops. It changes what kind of system this is (README §1, §5, §7 rewritten).

### E. Pause

Keep the `paper` account running the 1.8 acceptance; build nothing further until a decision.

## 6. The plan of action we recommend

1. **The fixed EP result is in** (§2): +0.21R, a positive point estimate that fails the gate. By the rule we set before seeing it, A stays a candidate (a positive estimate) but has to be restructured, not re-run as is.
2. **The user chooses** among: B alone; B + A; D; or pause. Our recommendation: **B, plus A in its close-entry form** (§5 A: the gap session bought at 15:40 if the gap held), so both candidates share one new entry mechanism. Drop C.
3. **Pre-register before any code runs on real data** (as `docs/backtests/baseline-preregistration.md` did):
   - at most two candidates, each fully specified (entry, stop, plan, fundable strategies);
   - the gate unchanged (90% interval above zero on 2017-2022, positive on the holdout);
   - what counts as success and what happens on failure;
   - every new threshold fixed in advance, from a source or the README, not from the tuning data.
4. **Design and record the change** (README §7.1 entries and stops, #144), including the order-layer path that keeps stops at the broker for close entries. Get the user's approval for the design before building.
5. **Build and test** (pure functions, 100% branch coverage on guardrails, tests with known answers), then run the pool once on 2017-2022.
6. **If a candidate passes on tuning:** freeze it and the 5-pillar regime in `risk.yaml`, then **open the holdout once** (3.8 step 5) for the gate and the regime's holdout checks.
7. **If nothing passes:** report it plainly and bring the decision back to the user (D or pause). Don't keep searching the tuning period; each extra trial makes a lucky pass more likely.
8. **Either way, check the cost assumption against reality:** compare the `paper` account's real fills (1.8 acceptance) with `risk.yaml` `sim.slippage`. That is a measurement, never a lever to make the gate pass.

**Running alongside, unaffected:** the 1.8 acceptance on AWS (`paper`, 10 unattended sessions, the stop hand check at the first fill). Before the next deploy, `paper` needs `alembic upgrade head` (0007).

## 7. Questions only the user can answer

1. Which option(s): B, B + A, D, or pause?
2. For B: a limit buy at 15:40 at the decision price, or a limit-on-close order? (Fill certainty vs the price we decided on.)
3. Should the small-cap tail stay in the universe, given that the IEX spread cap skips it live?
4. How many candidates may we try before opening the holdout (we suggest two at most)?

## 8. A prompt for the next session

Paste this into the new chat:

> We're on AuraTrade task 3.8. The baseline gate failed and Phase 4 is blocked until we decide how the strategy changes. Read `docs/strategy-decision.md` first, then the files its §1 lists, then CLAUDE.md. Don't re-run anything yet and don't open the 2023+ holdout.
>
> Then:
> 1. Check the reasoning in `docs/strategy-decision.md` against the reports and the code. Tell me where you disagree, and anything we missed (other mechanisms behind the losses, other options that keep "the LLM decides, code enforces limits", stops at the broker, $5k whole shares and the free data tiers).
> 2. Give me your recommendation among the options in §5 (or a better one), with the evidence, the build effort, the risks and what you'd expect the result to be.
> 3. Propose the pre-registration for the candidates you recommend (§6 step 3): every rule and threshold fixed in advance, and what we do if they fail.
>
> Ask me the questions in §7 when you need the answers. Build nothing until I approve the plan.

## 9. Sources (searched 2026-10-24)

- Lou, Polk & Skouras, [A Tug of War: Overnight Versus Intraday Expected Returns](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf) (JFE 2019); summary at [Alpha Architect](https://alphaarchitect.com/2015/02/overnight-momentum-vs-intraday-momentum/)
- [Why Has PEAD Declined Over Time?](https://business.columbia.edu/sites/default/files-efs/imce-uploads/CEASA/Events%20Page/PEAD_Declined_over_time.pdf) (Columbia working paper)
- [Rest in Peace Post-Earnings Announcement Drift](https://ideas.repec.org/p/osf/socarx/z7k3p.html) (preprint)
- Fink, [A Review of the Post-Earnings-Announcement Drift](https://ideas.repec.org/p/grz/wpsses/2020-04.html)
- [Earnings announcement strategies](https://quantpedia.com/strategy-tags/earnings-announcement/) (Quantpedia, on Brandt, Kishore, Santa-Clara & Venkatachalam's EAR)
- McLean & Pontiff, [Does Academic Research Destroy Stock Return Predictability?](https://Www.Gwern.net/doc/economics/2016-mclean.pdf)
- [Swedroe: Is Momentum Really Dead?](https://www.etf.com/sections/index-investor-corner/swedroe-momentum-really-dead)
- [Accounting for the Anomaly Zoo](https://jacobslevycenter.wharton.upenn.edu/wp-content/uploads/2019/09/Accounting-for-the-Anomaly-Zoo.pdf) (costs)
- Sullivan, Timmermann & White, [Data-Snooping, Technical Trading Rule Performance, and the Bootstrap](https://www.fmg.ac.uk/sites/default/files/2020-11/dp303.pdf)
- [Rethinking CAN SLIM](https://www.greatworklife.com/canslim-investing/) (secondary summary of the 2013 study)
- [A VCP "study"](https://tradingqna.com/t/a-25-year-empirical-study-on-minervinis-volatility-contraction-pattern-vcp-what-2-35m-data-points-reveal-about-market-crashes/198732) (an unverified forum post)
- [Kullamägi research sheet](https://completetradersedge.com/wp-content/uploads/2026/09/CTE-Research-Sheet-Kristjan-Kullamagi-market-wizards-next-generation.pdf); [Qullamaggie setups](https://tikamalma.substack.com/p/qullamaggie-swing-trading-setups)
- Barber, Lee, Liu & Odean, [Taiwan day traders](https://faculty.haas.berkeley.edu/odean/papers/day%20traders/Day%20Trading%20Skill%20110523.pdf); [CXO summary](https://www.cxoadvisory.com/individual-investing/do-day-traders-make-money/)
- [Large Language Models and Stock Investing: Is the Human Factor Required?](https://arxiv.org/pdf/2603.19944) (2026); [LiveTradeBench](https://arxiv.org/pdf/2511.03628); Lopez-Lira, [Can ChatGPT Forecast Stock Price Movements?](https://www.anderson.ucla.edu/sites/default/files/document/2024-04/4.19.24%20Alejandro%20Lopez%20Lira%20ChatGPT_V3.pdf)
