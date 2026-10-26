# The strategy change: from a momentum book to a portfolio manager (#153)

**Status 2026-10-26: direction decided by the user, plan not yet written.** This page records the decision, why it was made, the strategy we start from, the decisions still open, and how the documents and the code will change. Nothing below is a rule unless it says "decided". It plays the role [strategy-change-momentum.md](strategy-change-momentum.md) played for #146: the input to a plan and a pre-registration, both approved by the user before any code is built.

## 1. The decision

AuraTrade stops being a stock trader and becomes a **portfolio manager**: it manages a whole account by following a written investment strategy, mostly in broad ETFs, changing course rarely. The LLM has bounded flexibility inside the strategy. **The goal is to beat the S&P 500 over 10 years.**

**Decided (the user, 2026-10-26):**
- **Not a swing trader.** No weekly entries and exits; the strategy is followed month to month and changes position a few times a year.
- **Goal:** beat the S&P 500 over 10 years. Single years may lag.
- **Universe:** ETFs and anything the broker offers. Holding mostly the S&P 500 is allowed and expected.
- **Leverage is allowed, "as long as it's careful":** bounded, used only when the conditions favour it, and enforced by code.
- **Capital:** $100,000 as the reference account. The Alpaca **paper** account is reset to it (👤); no live money is planned.
- **Target account:** a **regular (taxable) Israeli brokerage account with an API**, "just in case". So the strategy is judged **after Israeli tax**: 25% on real gains when sold, losses offset within the year and carried forward. The user is in Israel.
- **Purpose:** the user's **flagship project**, shown at interviews. Credibility (honest validation, a visible track record, explained decisions) matters more than a headline return.

**Kept from the current design:**
- **The LLM decides; code enforces the limits** (CLAUDE.md §3.4). Deterministic-first stays rejected. Code computes the rule allocation and the bands; the LLM chooses inside them and can't leave them.
- Paper only until the user says otherwise (CLAUDE.md §3.1); no secrets in git; no paid calls in tests; fail closed; the single-writer lock on every order change.
- Validation before belief: a pre-registered backtest, a gate set before the results, and a holdout opened once.

**To be decided (§4):** everything else, including whether the "protective stops always rest at the broker" rule (CLAUDE.md §3.5) still applies to an ETF portfolio.

## 2. Why

Three strategies tested, all on 2017-2022, all failing their gates:

| Strategy | Result | Source |
|---|---|---|
| Chart-pattern setups, entered at 09:36 | the pool -0.11R a trade over 4,052 trades | `docs/backtests/baseline.md` |
| The same setups, entered at the close (#144) | the pool -0.30R over 1,003 trades | `docs/backtests/close-entry.md` |
| The momentum book (#146-#149) | -1.9% a year (adopted) and -14.0% (primary) against SPY +11.0%; max drawdown 52-76% | `docs/backtests/momentum.md`, #152 |

**The lesson:** both strategies tried to beat the market by **choosing stocks**, and the selection signal had no measurable edge here. The 12-1 ranking's monthly rank correlation with the next month's return was 0.014 (t = 0.65). Concentration and tight stops then turned a weak signal into a large loss.

**What's left:** a book that mostly owns the market can beat it only through **how much** it owns and **when**, not **which** stocks. There are three routes (§3); the plan combines two.

**What this is not:** a fourth variant on 2017-2022. That period is spent (#152). This is a different question (market exposure, not stock selection), tested on a different, much longer history (§5).

## 3. The strategy we start from

A starting point for the discussion, agreed in the brainstorm of 2026-10-26. Every number is a placeholder until the plan and the pre-registration fix it.

### 3.1 The three ways to beat the S&P

1. **Pick better:** hold different stocks or sectors and be right. Most professional managers fail at this over 10-15 years (the S&P SPIVA scorecards), and our three tests did too. **Here it gets only the satellite (about 20%), where the LLM's value is measured, not assumed.**
2. **Avoid the worst bear markets:** step out during long declines such as 2000-02 and 2008, and compound from a higher base. The cost is whipsaws: selling in a fast V-shaped crash (1987, 2020) and buying back higher.
3. **More than 100% exposure:** modest leverage beats the index on raw return over long periods, but on its own it makes crashes deeper.

**2 and 3 together are the core:** leveraged S&P exposure while the trend is up, T-bills while it's down. The leverage provides the return; the trend rule keeps the leverage from compounding the crashes. Sources: Faber (2007), the 10-month moving average; Gayed & Bilello (2016), "Leverage for the Long Run", the 200-day average with leveraged S&P and T-bills, 1928-2015; Hurst, Ooi & Pedersen (2017), a century of trend-following; Moreira & Muir (2017) on volatility-managed portfolios, with Cederburg et al. (2020) as the counter-evidence.

### 3.2 The shape

| Part | Share | What it does | Who decides |
|---|---|---|---|
| **Core** | ~80% | S&P 500 exposure on a dial from about 0.5x to 1.5x. A **buffered monthly trend rule** sets the level (for example, leave only when the S&P is clearly below its 10-month average); a **volatility brake** lowers it when the market is turbulent. The defensive asset is T-bills (SGOV/BIL) or Treasuries. | Code computes the rule level; the LLM may shift it by about ±0.2x with a written reason |
| **Satellite** | ~20% | Monthly allocation among an approved ETF list: sectors, Nasdaq-100, international, small-cap value, gold, bonds, managed futures | The LLM, within code's caps |
| **Rules-only twin** | (shadow) | The same portfolio with the core at its rule level and a fixed satellite, run alongside on paper | Code; the benchmark that tells us whether the LLM adds anything |

**Leverage, "carefully":**
- a hard exposure ceiling (about 1.5x), and leverage only while the trend is up;
- the volatility brake (high realized volatility → 1.0x or less, whatever the trend);
- through **leveraged ETFs (for example SSO, 2x daily) rather than margin**: no margin calls, and a position can't lose more than its value. Margin is the pre-registered alternative (§4, A3);
- code enforces all of it; the LLM can't raise the ceiling.

**Cadence:** a monthly decision, traded the next session, plus code's daily checks (the stop guard if stops are kept, the volatility brake if it is daily, reconciliation). The trend rule flips a few times a year at most.

### 3.3 What to expect, honestly

- **Before tax:** the published long-history results show trend + leverage beating buy-and-hold with smaller drawdowns, but they are in-sample for their authors, and the period after publication (2016 on) includes the 2020 whipsaw.
- **After Israeli tax:** each exit from stocks realizes gains taxed at 25%, while SPY held for 10 years pays only at the end. **The tax drag can be as large as the edge.** The strategy is built to sell rarely (a buffered rule, partial de-risking to 0.5x rather than 0x, using losses), and is judged after tax.
- **Fast crashes hurt:** a 1987-style day (-20%) costs a 1.5x book about 30% before a monthly rule can act.
- **Some years will lag the S&P badly.** The edge, if any, shows over full cycles, which is why the goal is 10 years.
- **It may fail the gate.** If so, §5.5 decides beforehand what the project shows instead.

## 4. The decisions to make

Grouped by the stage that needs them. Each lists the options and my recommendation. The user decides; the plan records the answers as decisions #154 onwards.

### A. The core strategy (before the pre-registration)

| # | Decision | Options | Recommendation |
|---|---|---|---|
| A1 | The trend rule | 10-month SMA on monthly closes (Faber); 200-day SMA checked monthly (Gayed & Bilello); 12-month time-series momentum (price above its level 12 months ago, Moskowitz, Ooi & Pedersen 2012) | **10-month SMA, monthly**: the fewest flips, the oldest published rule, and the 200-day is the pre-registered comparison |
| A2 | The buffer | none; a band (exit below SMA − x%, re-enter above SMA + x%); two months' confirmation | **A band of about 2-3%**, fixed from the literature before testing; it cuts whipsaws and taxed sales |
| A3 | How leverage is held | leveraged ETFs (SSO 2x) mixed with SPY; margin on SPY; index futures (out of reach for most retail APIs) | **SPY + SSO**: no margin calls; margin as a comparison |
| A4 | Exposure levels | up-trend 1.3x or 1.5x; down-trend 0x or 0.5x | **Up 1.3x, down 0.5x** as the primary; 1.5x / 0x as comparisons. 0.5x in a downtrend sells less (tax) and misses less of a V-shaped recovery |
| A5 | Volatility brake | none; scale to a volatility target (e.g. 15-18% annualized); a simple cap (1.0x when 1-month realized vol > x) | **The simple cap**, checked monthly; a daily brake trades more and pays more tax |
| A6 | The defensive asset | T-bills (SGOV/BIL); intermediate Treasuries (IEF); a mix | **T-bills**: Treasuries fell with stocks in 2022 |
| A7 | Core and satellite split | 80/20; 90/10; 70/30 | **80/20** |
| A8 | The rebalance band | rebalance every month; only when weights drift past a band | **A drift band** (e.g. ±5 points on the core), to avoid small taxed trades |

### B. The LLM's role (before the LLM layer is built; doesn't affect the core backtest)

| # | Decision | Options | Recommendation |
|---|---|---|---|
| B1 | The LLM's latitude | satellite only; satellite + a core shift of ±0.2x; more | **Satellite + ±0.2x on the core**, never above the ceiling, never more risk during a code downtrend |
| B2 | The satellite's ETF list | narrow (8-10 ETFs) or broad; leveraged and inverse ETFs allowed or not | **About 12 liquid ETFs, no leveraged or inverse products** in the satellite; leverage lives only in the core |
| B3 | What the LLM reads | regime and trend state, valuations, macro (FRED, VIX), news, ETF holdings, its own past decisions and their outcome | All of it, as facts; prompts cached; monthly batch where possible |
| B4 | How its share changes | fixed 20%; earns more (or less) by its record against the twin after N months | **Fixed for the first 12 months**, then a pre-registered rule (for example ±5 points a year by its excess over the twin) |
| B5 | Scoring the LLM | excess return of the real book over the twin; hit rate of its tilts; its written forecasts against outcomes | The excess over the twin, after tax, reported monthly with its uncertainty |

### C. Risk and the hard rules (before the plan; C1 changes CLAUDE.md)

| # | Decision | Options | Recommendation |
|---|---|---|---|
| C1 | Broker stops (CLAUDE.md §3.5) | keep "every position has a GTC stop"; stops only on the leveraged sleeve; no stops (the trend rule is the exit) | **Catastrophe stops on the leveraged sleeve only** (e.g. SSO 25-30% below its month's reference), tested as a comparison. A stop on every ETF would sell broad funds on noise and realize taxed gains. **This changes a hard rule, so it needs the user's explicit approval** |
| C2 | A drawdown halt | none; at -25% or -30% from the peak, drop to 1.0x or less until the trend rule re-confirms | **At -25%: no leverage until the next up-trend signal** |
| C3 | Fail closed, redefined | today: open no new positions | **On uncertainty: never increase exposure; hold or reduce.** Exits are never blocked (as today) |
| C4 | Single-ETF and issuer caps | none; a cap per ETF; a cap on all leveraged products | The core's SSO is capped by the exposure ceiling; each satellite ETF ≤ 10% of equity |

### D. Validation (before any code reads data)

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Long history source | Ken French data library (US market total return and T-bill, daily from 1926, free); S&P 500 index levels + Shiller's dividends; SPY total return for recent years | **Ken French daily for 1926 on**, cross-checked against SPY total return where both exist |
| D2 | Leveraged ETF before 2006 | synthesize 2x daily from the index: 2 × daily return − the fund fee − borrowing on 1x at the T-bill rate + a spread | **Synthesize**, then check the synthesis against real SSO 2006 on (it must track within a pre-set error) |
| D3 | Periods | tune on 1928-1989, holdout 1990-2026 opened once; or no tuning (rules from the literature only) with all of 1928-2026 scored once | **No tuning; rules from the sources; one scored run**, with the post-publication period (2016 on) reported on its own. Fewer choices, less overfitting, and the strongest story for an interview |
| D4 | The gate | after Israeli tax, against SPY bought, held and sold at the end: higher CAGR, max drawdown ≤ SPY's, positive in ≥ x% of rolling 10-year windows | **All three, x = 60%**; pre-tax shown beside it |
| D5 | The tax model | 25% on nominal USD gains (approximation); full Israeli real-gain rules in shekels | **25% on nominal USD gains, FIFO lots, losses offset in-year and carried forward, dividends taxed when paid**: conservative and the same for the benchmark |
| D6 | Comparisons allowed | the A-table alternatives (200-day, 1.5x/0x, margin, stops on/off, the vol brake on/off) | **At most 5**, judged by a pre-registered rule, as in #148 |
| D7 | What happens on a fail | stop; run the project with a plain 1.0x S&P core and the LLM satellite as the showcase; something else | **Decide before the run.** My recommendation: the showcase continues with a 1.0x core, and the report says plainly that leverage + trend failed after tax |

### E. The system and operations (before the build)

| # | Decision | Options | Recommendation |
|---|---|---|---|
| E1 | The running `AuraTradePaper` stack (the setup skeleton, 1.8 acceptance) | keep running; kill switch on and keep; tear down | **Kill switch on, keep the stack**: its plumbing (watchdog, stop guard, reconciliation) is reused; the jobs are replaced in the build |
| E2 | The paper account | reset `paper` to $100,000; open a third paper account | **Reset `paper` to $100,000** (👤 in the Alpaca dashboard) when the paper trial starts |
| E3 | Ingest | keep the nightly 2,761-ticker ingest; slim it to the ETF list, SPY, macro and news | **Slim it**: faster, cheaper, and frees most of Neon's 0.5 GB |
| E4 | The broker | Alpaca only; a broker interface with an Interactive Brokers adapter later | **Keep `execution/broker.py` as the interface** (it already is one); build an IBKR adapter only if real money is ever planned |
| E5 | The public site | the current landing page; a dashboard of the book vs the twin vs SPY, before and after tax, plus the decision journal | **The dashboard**: it is the showcase |
| E6 | Budget | Claude monthly calls (a few dollars a month); AWS (a few dollars a month) | Within the existing $150 cap; re-estimated in the plan |

## 5. How it will be validated

1. **Pre-registered before any code runs on real data** (`docs/backtests/portfolio-preregistration.md`): every rule and threshold with its source, the data and the synthesis, the tax model, the gate, the comparisons, the fail path (D7).
2. **The core is tested on about a century of daily data** (D1-D3): 1929, 1937, 1973-74, 1987, 2000-02, 2008, 2020 and 2022. 2017-2022 is not used for any choice (#152).
3. **The same two-step run as before:** a blind mechanics check (counts and invariants only), then one scored run. The report is committed as produced, then read.
4. **The LLM can't be backtested honestly** (it has read what happened). Its value is measured forward on paper against the rules-only twin (B5).
5. **On a fail,** the pre-decided path applies (D7); no new variants on the same history.

## 6. Documentation changes

| Document | Change | When |
|---|---|---|
| `docs/strategy-change-portfolio.md` | This page; becomes "decided and planned" after §4 is answered | Now |
| `docs/portfolio-plan.md` (new) | The plan: the answers to §4 with sources, the re-planned phases and tasks | After §4 |
| `docs/backtests/portfolio-preregistration.md` (new) | The pre-registration (§5) | With the plan, approved before any build |
| README | Rewritten for the portfolio manager: §1 (principles), §2 (architecture), §3 (schedule: monthly + daily checks), §5 (components), §6 (overlay → the trend dial), §7 (rebalance, exposure, exits), §8 (risk and guardrails), §9 (LLM design), §10 (data model), §15 (validation), §17 (limitations), glossary. Decision #153 and the plan's decisions in §16 | With the plan's first task |
| `docs/archive/README-momentum-book.md` (new) | The momentum book's README sections, kept as the record (as `README-setup-system.md` was) | Same task |
| `docs/execution-plan.md` | Tracker notice for #153; the momentum book's Phases 4-7 archived to `docs/archive/execution-plan-momentum-book.md`; the new phases | Same task |
| CLAUDE.md | §1 (what the project is), §2 (the source-of-truth table and decision count), §3.5 if C1 changes, §6 (the backtest rule points at the new pre-registration; "one simulator" names `sim/portfolio.py`), §7 (commands), §8 (glossary: core, satellite, twin, dial, after-tax gate) | Same task |
| `docs/next-steps.md` | The new "now" | Every stage |
| `config/risk.yaml` | A `portfolio:` section (typed and hashed); `book:` stays for the archived code | The first build task |

## 7. Codebase changes

The rule from #146 holds: code from a dropped strategy stays, unwired, citing its archived README; nothing new depends on it.

### 7.1 Kept as is (the foundation)

| Module | Why it still fits |
|---|---|
| `calendar/`, `db/` (engine, flags, job runs), `notify/`, `storage/`, `log.py`, `parameters.py`, `config.py` | Infrastructure, strategy-free |
| `jobs/base.py`, `jobs/runs.py`, `jobs/invoke.py`, `jobs/watchdog.py`, `jobs/schedule.py` (the slot list changes) | The shared runner, heartbeat, alerts and schedule |
| `execution/broker.py`, `alpaca_broker.py`, `lock.py`, `reconcile.py`, `ids.py`, `store.py`, `prices.py` | The broker interface and the order layer's plumbing; already an adapter boundary (E4) |
| `sim/fills.py`, `sim/fake_broker.py` | The one fill model and the chaos-tested fake broker |
| `data/alpaca_data.py`, `data/macro.py`, `data/news.py`, `data/http.py` | ETF bars, FRED/VIX, news for the LLM |
| `infra/` (both stacks), `Dockerfile`, `scripts/put_parameters.py`, `check_keys.py`, `reset_trading_state.py` | The AWS skeleton and its tools |
| `cli.py` (kill switch, mismatches) | Same operations |

### 7.2 Adapted

| Module | Change |
|---|---|
| `execution/invariant.py`, `jobs/stop_guard.py` | Follow C1: stops required only where the plan says (the leveraged sleeve), at the plan's level |
| `execution/orders.py`, `sizing.py` | Rebalance orders: target weights → whole-share orders, sells before buys, limits from the decision price, the drift band |
| `strategies/momentum_book.py` → a shared calendar module | The rebalance calendar (ranking session R, execution session E, half days, offsets) moves out so the portfolio reuses it; the book module imports it |
| `history/benchmarks.py`, `scripts/backfill.py` | A long-history loader (D1) and the ETF set; SPY total return as the benchmark |
| `ingest/` | Slimmed to the ETF list, macro and news (E3); the stock-universe steps are left unwired |
| `regime/` | Kept as **facts for the LLM** (and possibly a comparison), not a rule input |
| `journal/` | A monthly journal: the allocation, the LLM's reasoning, the limits it hit, the twin, after-tax results |
| `web/` + `AuraTradePublicSite` | The dashboard (E5) |

### 7.3 New

| Module | What it is |
|---|---|
| `strategies/core_allocation.py` | Pure: the trend rule with its buffer, the volatility brake, the drawdown halt → the core's rule exposure |
| `guardrails/portfolio.py` | Pure, 100% branch coverage: the exposure ceiling, the approved list, per-ETF caps, the LLM band, the turnover cap, "never more risk on uncertainty"; exits never blocked |
| `tax/israel.py` | Pure: FIFO tax lots, 25% on realized gains, in-year offsets and carry-forward, dividend tax; used by the simulator, the journal and the dashboard |
| `sim/portfolio.py` | The portfolio simulator on `sim/fills.py`: daily marks, monthly decisions, synthesized leveraged ETF (D2), T-bill accrual, taxes |
| `llm/portfolio_manager.py` (+ schemas, prompts) | The monthly LLM decision: structured output inside the bands, refusal handling, prompt caching (load the `claude-api` skill first) |
| `jobs/rebalance.py` (and its submit/collect pair if batched) | The monthly job: rule allocation → LLM → guardrails → orders; the twin computed in the same run |
| `backtest/portfolio.py` (+ report) | The harness: `--blind`, the scored run, the gate, the comparisons |
| Alembic migration | Tax lots, monthly decisions (the LLM's and the twin's), target weights |

### 7.4 Left unwired (archived code)

`strategies/` (the setup scans, ranking, verdict cache, watchlist, `momentum_book.py`'s ranking), `plans/`, `guardrails/` modules of the setup system and `book.py`, `sim/trade.py`, `sim/book.py`, `sim/minutes.py`, `backtest/` (baseline, close entry, momentum), `jobs/scan.py`, `analyst_submit.py`, `execute_entries.py`, `execute_open.py`, `eod_review.py`, `simulate.py`, `journal/baseline.py`, `scorecard.py`, `breakout_tracker.py`, `charts.py`, the stock-universe parts of `ingest/` and `indicators/`. Their tests keep running, so they stay correct as the record; they are removed from the schedule when the new jobs replace them.

## 8. The sequence

| Stage | What | Gate |
|---|---|---|
| 0 | This page (#153) | 👤 The user agrees with the direction and the decision list |
| 1 | The discussion of §4: A, C and D first (they shape the backtest), then E; B can wait until stage 5 | 👤 The user's answers, recorded as decisions |
| 2 | `docs/portfolio-plan.md` + the pre-registration | 👤 Approved and committed before any code reads data |
| 3 | The documentation rewrite (§6) and `risk.yaml` `portfolio:`; kill switch on `paper` (E1) | Reviewed in the PR |
| 4 | The backtest build: long-history data and the leveraged synthesis, `core_allocation`, `tax/israel`, `guardrails/portfolio`, `sim/portfolio`, the harness; known-answer tests; the blind check; the one scored run | 👤 The scored run with the user; gate pass or the D7 path |
| 5 | The LLM layer (B), the monthly job, the twin, the slimmed ingest, orders | `dry_run` first, then `paper` |
| 6 | The paper trial on $100,000; the dashboard on the public site | 👤 Deploys are the user's |
| 7 | (Optional) an IBKR adapter, only if real money is ever planned | 👤 |

## 9. A prompt for the next session

> We're on AuraTrade. On 2026-10-26 the user decided to turn it from a stock trader into a portfolio manager that aims to beat the S&P 500 over 10 years, after Israeli tax, on a $100k paper account (decision #153). Read `docs/strategy-change-portfolio.md` first, then CLAUDE.md, the decision log from #146, and `docs/backtests/momentum.md`. Don't run anything on real data.
>
> Then walk me through the decisions in its §4, groups A, C and D first: for each, the options, the sources and your recommendation, and record my answers. When they are settled, write `docs/portfolio-plan.md` and the pre-registration for my approval. Build nothing until I approve them.
