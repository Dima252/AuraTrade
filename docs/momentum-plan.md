# The momentum book: the plan (#146)

**Status 2026-10-25: approved.** The user delegated the decisions in §10 ("decide for me; as profitable as you can") and Claude took them; they are recorded under §10. Recorded as decisions #147-#149 in task 3.9. This plan answers `docs/strategy-change-momentum.md` (its §7 prompt). It proposes the strategy's rules, the pre-registration for its backtest, the README changes and a re-planned execution plan from 3.8 on. Nothing was run to write it, and the 2023+ holdout is still closed. Once approved, the decisions go into the README log (proposed #147-#149), the rules into `config/risk.yaml`, and the pre-registration into its own file before any code reads real data.

Where a source disagrees with another, the plan picks one as the **primary** and makes the other a **pre-registered comparison** (C1-C7, §5.5). The literature citations are from the published papers and books as known when this was written. Their exact numbers are checked when the pre-registration is written (task 3.9).

## 1. The strategy in one paragraph

Once a month, code ranks the 1,000 most liquid US stocks in the point-in-time universe by their **12-1 month return**: the past year, skipping the last month. The book holds **8 names** in equal slots. A holding is kept while it stays in the **top decile**; it is sold at the rebalance once it drops out. Empty slots are filled from the top of the ranking, at most 2 per industry cluster. Every position carries a **GTC stop at the broker, 20% below the entry**, raised at each rebalance and never lowered. No new buys go in while the regime is `CORRECTION` or the crisis override is on. Trades go in at the **15:40 decision price** on the first session of each month. **Live, the LLM chooses** which of the top 16 candidates fill the empty slots. It may exit early, buy smaller or hold cash, and it does nothing looser than the rule book would. Code builds the list, every price level and every limit.

## 2. The rules (primary configuration)

All values go to a new `risk.yaml` section, `book:`. Windows count sessions.

| Rule | Primary | Source |
|---|---|---|
| **Universe** | The point-in-time universe of §5.1 (common stocks and ADRs, raw price ≥ $10, 50-session average dollar volume ≥ $10M), narrowed to the **1,000 with the highest 50-session average dollar volume** on the ranking session, with ≥ 253 sessions of closes | Gray & Vogel (2016) rank about the 1,000 largest liquid US stocks. Israel & Moskowitz (2013) find momentum doesn't weaken with size. Lesmond, Schill & Zhou (2004) and Korajczyk & Sadka (2004) find small, illiquid names eat most momentum profits in costs. Dollar volume stands in for market cap: there's no free point-in-time cap history, and delisted names have no EDGAR data (#130) |
| **Signal** | 12-1 return = close(t−21) ÷ close(t−252) − 1, on split-adjusted closes (price return, as §5.1 already uses for RS) | Jegadeesh & Titman (1993); Asness, Moskowitz & Pedersen (2013); the Fama-French UMD factor (months 2-12) |
| **Holdings** | **N = 8.** Slot value = equity × max exposure ÷ 8, fixed at the rebalance. Shares = ⌊slot ÷ limit price⌋. A name whose price doesn't fit 1 share is skipped and the next one is taken (#122) | The README's 4-8 positions (§6, Minervini). The doc's 5-10 at $5,000. 8 slots of about $625 leave 4-12 shares for a typical $50-150 leader |
| **Weighting** | Equal slots. No resizing between rebalances: winners can grow | Jegadeesh & Titman; Gray & Vogel (equal weight). Clenow (2015) re-sizes by ATR; not adopted, to avoid partial sales and their turnover |
| **Rebalance** | **Monthly.** Ranking on the close of the last session of each month (evening job). Trades on the **first session of the next month** at 15:40. A half day moves the trades to the next full session | Monthly is the academic standard (Jegadeesh & Titman). The 12-1 signal skips the last month, so the list is known the evening before and needs no intraday data |
| **Hold zone (turnover buffer)** | A holding is kept while its percentile in the ranked 1,000 is ≥ 90 (the top decile). Below that it is sold at the rebalance | Novy-Marx & Velikov (2016): a buy/hold spread cuts momentum's trading costs most. Clenow sells below the top 20%. The decile is the academic winner portfolio itself |
| **Buys** | Empty slots are filled in rank order (the rule book), or by the LLM from the **top 16** (2N) eligible names not held (live, §3) | — |
| **Cluster cap** | At most 2 holdings per cluster (same industry group, or a 60-session return correlation ≥ 0.7): the existing guardrail (#140) | Moskowitz & Grinblatt (1999): much of stock momentum is industry momentum, so the cap gives up some return for diversification. It's a safety rule (comparison C7) |
| **Stop** | One GTC stop at the broker covering the full quantity, at **80% of the decision price** at entry (rounded down to the tick). At each later rebalance it is raised to 80% of that session's decision price if that is higher. Never lowered. After a stop-out the slot stays in cash until the next rebalance | CLAUDE.md §3.5 requires a broker stop. Gray & Vogel, Clenow and Antonacci use no stop, so the primary is a wide catastrophe stop that rarely changes the strategy. Han, Zhou & Zhu (2016) find a 10% stop cuts momentum crashes (gross of costs): comparison C2. The cash-until-rebalance rule is theirs |
| **Market overlay** | No new buys while the effective regime is `CORRECTION` or the crisis override is active (§5.2). Rule sells and stops carry on | Clenow buys only with the index above its 200-day (comparison C4). Daniel & Moskowitz (2016): momentum crashes come in panic states. This reuses the built, live regime layer (#135) |
| **Earnings** | **Hold through and buy through** reports (no blackout, no cushion). Live, the LLM may step aside (that only tightens) | Jegadeesh & Titman (1993), Chan, Jegadeesh & Lakonishok (1996), Novy-Marx (2015): a large share of momentum's return comes around earnings announcements, so stepping aside gives it up. **Disagrees with** the README's earnings safety rules (§7.4, §8): comparison C5, and a question for you (§10 Q3) |
| **Execution** | On the rebalance session at 15:40, under the lock: sells first, through the sell sequence (§5.7). Then buys: an **OTO marketable limit** at the decision price × 1.005 with its GTC stop leg (the #144 / tranche-2 order path). Without an IEX decision price, a buy falls back to the delayed-SIP last trade (#144: the limit bounds the fill). A buy that doesn't go in is retried at 15:40 on the next session only | The close has the day's best liquidity. The order path exists and keeps the stop invariant (#113-#115). The next morning's open (09:31) is the alternative: wider spreads, and it misses no information the 12-1 signal uses |
| **Cash** | Earns nothing in the backtest | Conservative |

**Live-only rules** (they can't be backtested, and the report lists them as differences): a name with a pending definitive cash acquisition or an unresolved corporate-action mismatch isn't bought (§5.7); the LLM's vetoes and step-asides.

## 3. The LLM's role (open question 8)

The split stays: **the LLM decides within code's list; code enforces the limits** (CLAUDE.md §3.4).

| Role | When | Sees | Decides | Bounded by |
|---|---|---|---|---|
| **Book Analyst** (Batch API, no tools) | Rebalance evenings | Each of the top 40 eligible names and every holding: the 12-1 return and rank, distance from the 52-week high, smoothness (share of up days), volatility, industry group and its rank, RS rating and line, Trend Template facts, any current setup from the old scans (as a fact), EPS/revenue growth, the next earnings date, ≤ 5 headlines, daily and weekly charts | For each name: `TAKE / PASS` (`KEEP / EXIT` for a holding), conviction 1-5, ≤ 3 key risks, reasoning ≤ ~80 words | Its verdicts are inputs. Only the Portfolio Manager acts |
| **Portfolio Manager** (real time, tools) | Rebalance sessions, 08:45 (hard deadline 12:00); and event days | The Analyst's verdicts, account state, the regime and pillars, holdings with events, clusters | Which names from the **top 16** fill the empty slots; early exits; each buy at 100% or 50% of a slot; leaving a slot in cash; its regime view (only more conservative); raising a stop | Guardrails (§4): the list, the slot, the cluster cap, tighten-only. A late or invalid answer means **no buys today**. The rule sells still run (CLAUDE.md §3.5) |
| **Position events** (inside the Portfolio Manager call) | Any session with an event | Only the holdings with events: a report in the next 5 sessions, a one-session fall of ≥ 10%, a corporate action | `KEEP / EXIT / RAISE_STOP` | Tighten-only |
| **Weekly review** | Saturday | The LLM book vs the rule book vs SPY and MTUM; the scorecard | Proposed lessons (your approval, §5.11) | As now |

**Why the top 16 and not the top 40:** every name in the top 16 of 1,000 is deep in the top decile the backtest validates. So whatever the LLM picks, the live book stays inside the tested strategy. The Analyst reviews 40 so that the scorecard has enough data (§5.7).

**How its value is measured** (it can't be backtested: the model has read what happened to these stocks):
- **The rule book** (the shadow baseline, §5.12) takes the same list in rank order. Both books are simulated through the same portfolio simulator (§5.3), so the comparison is like for like.
- **The scorecard:** every reviewed name is scored by its return over the next rebalance period, relative to the average of the top 40. The headline statistic is the **monthly rank correlation between conviction and that relative return** (the information coefficient), averaged over months, with its t-statistic. About 40 names a month gives far more data than about 3 trades a month would.

## 4. Risk limits (README §8 changes)

The setup-era limits assume small R-based risks with tight stops. A momentum book with a 20% catastrophe stop doesn't fit them: at 8 slots, each position risks about 2.5% of equity to its stop, and the book about 20%. **This is the largest change in risk posture, and it needs your explicit approval.**

| Limit | Now (setups) | Proposed (book) |
|---|---|---|
| Risk per trade | ≤ 1.5% of equity, R-sized | Replaced by the slot: ≤ 1/8 of equity at entry, about 2.5% of equity to the stop |
| Heat | ≤ 6% | Reported, not capped: about 20% when fully invested |
| Max positions / exposure | By regime (§6) | 8 / 100% (no margin). `CORRECTION` and the crisis override block buys |
| Position size | ≤ 25% | ≤ 1/8 at entry; drift allowed (the report shows the largest weight) |
| Cluster cap | 2 | 2 (unchanged) |
| Stop | Per-strategy widths, structural | GTC, full quantity, at the plan level, raised only (§2). The invariant, sell sequence and lock are unchanged |
| Progressive-exposure multiplier | R-based tiers | Retired: R means little with a 20% catastrophe stop. "Prove it first" survives as **half slots** at the start of the paper trial and of live trading |
| Daily / weekly loss halts | −2% / −5%, block entries | Kept: they block buys only, so they matter only on rebalance days (the buys retry the next session) |
| **Drawdown halt** | −15%, manual re-enable | **−30%**, manual re-enable, buys only (§10, decision 1). A momentum book often draws down 25-40%. At −15% the halt would stop the strategy in most bad years |
| Price levels from code | Pivot ±3%, tighter stops | The LLM never supplies a price. It may only raise a stop |
| Removed for the book | Pivot tolerance, max chase, 50-day extension, base count, tranche 2, stop-width caps, "keep losses small" | — |

## 5. The pre-registration (proposal)

Written out as `docs/backtests/momentum-preregistration.md` in task 3.9, approved and committed **before any book code reads real data**, the way the close entry was.

### 5.1 Periods and order of work

- **Warm-up 2016.** The first ranking needs 253 sessions, so the first rebalance is 2017-02-01.
- **Tuning period:** rebalances from 2017-02-01 to 2022-12-01. Positions are marked through 2022-12-30.
- **Holdout:** 2023-01-03 to the latest backfilled session, opened **once**, for the frozen configuration only.
- **Order:** approve and commit → build and test on hand-built data → **blind mechanics check** (counts and invariants only: no returns) → **one scored run** of the primary and C1-C7 → read with you → freeze → open the holdout once.
- A bug found in the blind check may be fixed and the check re-run. A rule that can't work as written stops the work for you (the close-entry rule). Nothing changes after the scored run without making it a new trial.

### 5.2 Data

- Daily bars only. The minute-bar cache isn't needed: every rule acts on closes, and every fill is at the close or a stop.
- **Benchmarks:** SPY and MTUM daily bars with `adjustment=all` (total return, dividends reinvested), fetched once from Alpaca into the Parquet history (free calls; MTUM has traded since 2013). The book's returns are **price only**: there's no dividend history for the whole universe. That **biases the comparison against the book** by about the universe's dividend yield less the benchmark's. The report states it.
- A third reference: the **equal-weighted ranked universe** (the 1,000), notional, price-only, monthly. It shows what the momentum selection adds over just holding the universe.

### 5.3 The portfolio simulation (`sim/book.py`)

- Goes through the one simulator: `sim/fills.py` for every fill, the liquidity-tier slippage (#141), ticks against the trader, and the pure ranking, rule-book and guardrail functions that live trading calls.
- $5,000, whole shares, cash tracked; no margin; cash earns nothing.
- **Rebalance fills:** the decision price is the session's close (the backtest proxy, measured at 2% disagreement against 15:40 in 3.8c). Sells first, then buys from the cash freed. A buy fills at close + slippage if that is at or under the limit.
- **Stops:** each session, a low at or below the stop fills at min(stop, open) − slippage (gaps go through).
- **Delistings:** a held security that stops trading exits at its last close. **A held security ended by the 4× jump rule (#130)** exits at its successor's first open. Without a successor, it exits at the last close minus the jump. This is the conservative side: a real collapse shouldn't exit at the price before it. The blind check counts these cases.
- **Regime:** the computed regime from the 5-pillar variant `cu+2_corr-2_c2_dist8` (`docs/backtests/regime-5-pillar.md`), on the evening before, as the close-entry run used. It is frozen with the book.

### 5.4 Metrics, the gate and the fail rule

Reported for every configuration, for the whole period and per calendar year: CAGR, volatility, max drawdown, CAGR ÷ max drawdown, excess CAGR over SPY and MTUM, turnover, costs paid, average holding period, cash share, and the stop-out rate.

**The power problem, stated in advance.** The book's tracking error against SPY is likely 15-25% a year. Over 6 years, a 90% interval on the excess return is then about ±10-17% a year wide. A realistic 2-5% edge can't be told from zero in that time, whatever the strategy. So **the gate rests on point estimates and robustness, not on a significance test.** The intervals are still reported (block bootstrap of daily excess returns, 63-session blocks, 2,000 draws), and so is the minimum detectable effect.

**Gate on 2017-2022** (the primary, or the configuration adopted from C1-C7; all four required):
- **G1:** excess CAGR over SPY total return > 0, after costs.
- **G2:** excess CAGR over MTUM total return ≥ 0 (§10 Q2).
- **G3:** excess over SPY positive in ≥ 3 of the 6 years, and still > 0 with the best year left out.
- **G4:** CAGR ÷ max drawdown ≥ SPY's over the same period, and max drawdown ≤ 45% (§10, decision 1).

**Holdout, opened once:** excess CAGR over SPY > 0 and over MTUM ≥ 0. 2023-25 was kind to momentum (README §15), so a pass there is weak evidence and a fail is strong.

**On a fail:**
- **Tuning fails:** the holdout stays closed; no capital; no further variants on 2017-2022. It comes back to you, with the options of a passive SPY/MTUM holding or a pause.
- **The holdout fails:** no capital; the holdout is spent; it comes back to you.
- **Both pass:** freeze in `risk.yaml` (hash recorded), then build on to Phase 5 and the paper trial.

### 5.5 The comparisons (C1-C7) and the rule for adopting one

Each comparison changes one dimension against the primary, on 2017-2022 only.

| # | Comparison | Why it's allowed |
|---|---|---|
| C1 | Signal: **nearness to the 52-week high** (close ÷ 252-session high) instead of 12-1 | George & Hwang (2004) find it dominates; the README already cites them |
| C2 | Stop at **10%** instead of 20% (Han, Zhou & Zhu), raised the same way | The only momentum-specific stop evidence (gross of costs) |
| C3 | Market overlay **off** | Safety rule, tested on vs off (§15) |
| C4 | Market overlay = **SPY above its 200-day SMA** (Clenow) instead of the regime | A simpler, sourced filter |
| C5 | **Earnings rules on:** the existing blackout for buys (§8) and the cushion for holdings (§7.4, measured in % since R doesn't apply: exit if under +10%, otherwise hold) | The README's safety rule, against the evidence in §2 |
| C6 | **The whole universe** instead of the top 1,000 by dollar volume | Hong, Lim & Stein (2000): momentum is stronger in small, less-covered names; costs say otherwise |
| C7 | Cluster cap **off** | Safety rule, tested on vs off |

**Adoption:** a comparison replaces the primary only if, on 2017-2022, the 90% block-bootstrap interval of its daily return difference against the primary excludes zero in its favour, with max drawdown no worse. A safety rule (C3, C5, C7) stays unless removing it improves return by more than noise without a deeper drawdown (README §15). **The holdout is not used to choose between them.** README §15 asks for "beats on both periods", but that spends the last clean test on selection, so this plan proposes judging the comparisons on tuning only and keeping the holdout for the gate (§10 Q6). The report states that 8 configurations were tried.

### 5.6 Diagnostics (reported, never adopted)

- **Rebalance timing luck** (Hoffstein, Faber & Braun 2020): the primary rerun with its rebalance on the 1st, 6th, 11th and 16th session of the month. The spread shows how much of the result is the calendar.
- **No price skips:** the primary at a notional $100,000. The difference is the cost of $5,000 and whole shares.
- **The signal check:** each month, the rank correlation between 12-1 and the next month's return across the 1,000, with its mean and t-statistic. It separates "momentum didn't work in 2017-2022" from "our implementation lost it".
- Zero-cost run; returns by regime; holding periods; the largest weight; the count of buys moved by a half day or by no price.

### 5.7 Forward evidence (the LLM, from task 4.7 on)

Pre-registered in task 7.0, from the dry-run data: the information coefficient of conviction (§3), and the LLM book against the rule book through `sim/book.py`. With about 40 names a month, a meaningful information coefficient takes a year or more to show. That is expected, and it's why the dry run starts as soon as the backtest passes.

## 6. Answers to the open questions (`strategy-change-momentum.md` §5)

| # | Question | Answer | Where the sources disagree |
|---|---|---|---|
| 1 | Formation period | 12-1 | George & Hwang (52-week high, C1); Novy-Marx (2012) puts the effect in months 7-12. 6-1 isn't tested, to keep the comparison count down |
| 2 | Universe | Point-in-time universe, top 1,000 by dollar volume. No fixed price ceiling: a name that doesn't fit 1 share in a slot is skipped (the $100k diagnostic measures the cost) | Small-cap momentum is stronger before costs (C6) |
| 3 | Holdings | 8, equal slots, cluster cap 2 | Industry momentum vs diversification (C7); risk weighting (Clenow) not tested |
| 4 | Rebalance | Monthly, trades on the first session at 15:40, top-decile hold zone | Clenow: weekly, top 20%. Timing luck reported (§5.6) |
| 5 | Stops | 20% GTC catastrophe stop, raised at each rebalance, cash until the next rebalance after a stop-out | Han, Zhou & Zhu 10% (C2); practitioners use none |
| 6 | Regime overlay | `CORRECTION` / crisis blocks buys | None (C3); SPY 200-day (C4) |
| 7 | Earnings | Hold through, buy through; the LLM may step aside | The README's blackout and cushion (C5), your call (§10 Q3) |
| 8 | The LLM's role | §3 | — |
| 9 | Cost and cadence | §7 | — |
| 10 | Validation | §5 | §15's "both periods" vs a clean holdout (§10 Q6) |
| 11 | What stays of #145 | Phase 4 is still needed, with new schemas and prompts. Recommended: it starts **after the tuning gate passes**, not alongside: decisions are monthly, so waiting costs about one month of forward data, and it avoids building prompts for a strategy that fails | Your call (§10 Q5) |

## 7. What to expect (honestly)

- **Return:** long-only momentum has historically earned a few percent a year over the market, with deep drawdowns. Long-only books suffer less in the crashes than the long-short factor: Daniel & Moskowitz find the losers' leg drives them. The backtest may well show about zero excess in 2017-2022. That would be a fail by G1, and the plan takes it as one.
- **Turnover and costs:** probably 2-3 name changes a month, about 1% a year in slippage at the current tiers.
- **Running costs fall:**

  | Item | Monthly |
  |---|---|
  | Claude: one batch of ~40 reviews a month ($0.016 each, spike 0.3g), plus Portfolio Manager calls on rebalance and event days | ~$3-8 |
  | AWS | ~$1-5 |
  | Domain (optional) | ~$1.50 |
  | **Total** | **~$6-15** (was ~$20-30) |

  That is still 1.5-3.5% of $5,000 a year, about the size of the expected edge. At this account size the project's value is a validated system, not income.
- **Limits** that go into README §17: the delisted-coverage gap (survivorship flatters); price-only book returns (they understate the book); 6 years can't prove a small edge; 2023-25 favoured momentum; the LLM's value is only measurable forward.

## 8. README changes (written in task 3.9, in the same PR as the decisions)

| Section | Change |
|---|---|
| Intro, §1 | The method becomes a documented anomaly (momentum) run with practitioners' discipline. Principle 1: the rules come from the momentum literature and practitioners. Principle 5 becomes "hold the strongest; sell on the rebalance or the catastrophe stop". Principle 6: half slots to start. Principles 2-4 and 7-9 stay |
| §2, §3 | The architecture and schedule: evening `post_market` → `ingest` → `simulate` (rule book, scorecard) → `regime` → `rank` → `book_review` (batch, rebalance evenings only). Day: `premarket_check`, `portfolio_manager` (rebalance and event days), `execute_open` (09:20 splits, 09:31 check run), `execute_close` (15:40: rebalance trades, event exits), `stop_guard`. Retired: `execute_entries` 09:36/09:51, the `intraday_monitor` early rules, the `eod_review` soft rules |
| §5.3 | The scanner → the **momentum ranking** (§2). The five scans stay as facts for the Analyst, not as the pool |
| §5.4-§5.9 | The Book Analyst, the Portfolio Manager, events and execution as in §2-§3. The live EP path (5.6) is dropped |
| §5.12 | The shadow baseline → the **rule book**; the breakout tracker stays only as the regime's leadership-health input; the scorecard → §3 |
| §6 | Regimes → the overlay (buys blocked in `CORRECTION` / crisis). The strategy table and conviction bars go |
| §7 | Rewritten: entries, the stop, the hold zone, exits. Tranches, the menus, early-behaviour rules, the 8-week hold, climax sales and the cushion are retired for the book (C5 tests the cushion) |
| §8 | §4 of this plan |
| §9 | The roles and schemas of §3; the budget of §7 |
| §12, §13 | Costs; new modules (`strategies/momentum_book.py`, `guardrails/book.py`, `sim/book.py`, `journal/rule_book.py`, `backtest/momentum.py`) |
| §15 | The book's backtest validity (§5), the gate, the go-live criteria rewritten for a monthly book (§9, 7.x) |
| §16 | #147 the momentum-book rules; #148 the pre-registration and the gate; #149 the re-plan |
| §17, §18 | The limits in §7; the momentum sources in §11 |

## 9. The execution plan from 3.8 on

3.8 stays closed. The old Phases 4-7 are replaced as below. **Kept running alongside:** the 1.8 acceptance on `paper`. `paper` needs `alembic upgrade head` (0007) before its next deploy.

### Phase 3 (continued): the momentum book and its backtest

| Task | Size | What | Accept |
|---|---|---|---|
| **3.9** 👤 | S | README rewrite (§8 of this plan), decisions #147-#149, `risk.yaml` `book:` values, and `docs/backtests/momentum-preregistration.md`, approved and committed before any book code reads real data | You approve the PR |
| **3.10** | M | `strategies/momentum_book.py` (pure): eligibility, 12-1 and 52-week-high signals, percentiles, the top-16 list, the hold zone, the rebalance calendar (calendar helpers, sessions), slot sizing in whole shares, stop levels and raises, the rule book's choice (rank order under the cluster cap) | Known-answer tests on hand-built data; 100% branch coverage |
| **3.11** | M | `guardrails/book.py` (pure): the §4 limits for LLM actions (list membership, slot size, positions, exposure, cluster cap, tighten-only stops and exits, buys blocked by regime, crisis, halts and the kill switch). The setup rules stay for the old code but don't apply to book actions | 100% branch coverage (CI), every rejection path tested |
| **3.12** | M | `sim/book.py`: the portfolio simulation of §5.3 on `sim/fills.py`; the benchmark fetch (SPY and MTUM `adjustment=all` into the Parquet history, free calls) | Known-answer books: a hand-built month of fills, a gap through a stop, a delisting, a jump-rule ending, a half-day rebalance, a 0-share skip |
| **3.13** | M | `backtest/momentum.py` + report: the primary, C1-C7, the diagnostics, the blind flag, the per-year tables, the bootstrap | Runs end to end on synthetic history; the blind mode prints no returns |
| **3.14** | S | The **blind mechanics check** over 2017-2022 → `docs/backtests/momentum-blind.md` | Invariants hold (no unprotected position, exposure ≤ 100%, ≤ 8 names, ≤ 2 per cluster); counts look workable, or it comes to you |
| **3.15** 👤 | S | The **one scored run** → `docs/backtests/momentum.md`, committed as produced, read with you by §5.4 | The reading is written |
| **3.16** | S | (On a pass only) freeze in `risk.yaml`, open the holdout once → the holdout section of the report | Gate result recorded; the regime's 5-pillar variant frozen with it |

### Phase 4: the LLM layer (after a 3.15 pass, unless you choose otherwise in §10 Q5)

| Task | Size | Change from the old plan |
|---|---|---|
| 4.1 LLM client and batch | M | Unchanged (load the `claude-api` skill first) |
| 4.2 Schemas and prompts | M | `BookVerdict` (TAKE/PASS, KEEP/EXIT, conviction, risks, reasoning), `RebalanceDecision` (slot picks from the top 16, sizes 100/50%, early exits, regime view, stop raises), `PositionEventDecision`, `WeeklyLessons`; untrusted text as data |
| 4.3 Context builders and tools | M | The §3 fact table per name; charts; `get_news`, `get_chart`, `get_fundamentals`, `get_position_history` |
| 4.4 Jobs and skip logic | M | `rank` (replaces `scan` in the chain; writes the ranking every evening and the list on rebalance evenings), `book_review_submit` / `collect`, `portfolio_manager` (rebalance and event days only), deadlines, events |
| 4.5 Rule book and scorecard (live) | M | `journal/rule_book.py` replaces the setup baseline in `simulate`: the rule book from the same list, through `sim/book.py`; the information-coefficient scorecard |
| 4.6 👤 Prompt sanity set and effort | S | 20-30 historical rebalance lists, hand-checked for format and reasoning. Paid: under $2, your OK first |
| 4.7 👤 Deploy the evening pipeline in `dry_run` | S | As old 4.6. Paid: about $3-8 a month, your OK first. It keeps running through the trial |

### Phase 5: execution (smaller than before)

| Task | Change |
|---|---|
| 5.1 Guardrail wiring | Book actions → `guardrails/book.py` → `signals`; `guardrail_events`; alerts |
| 5.2 Account constraints | Unchanged, minus the opening-range triggers |
| 5.3 Orders | The rebalance at 15:40: sells via the sell sequence, then OTO marketable-limit buys with GTC legs; stop raises at rebalances (atomic replace); partial fills; corporate actions (unchanged design). No tranches, no 09:36 entries |
| 5.4 Reconciliation | Unchanged |
| 5.5 Jobs | `execute_close` (15:40), `execute_open` (09:20 / 09:31), `stop_guard`, `post_market` (journal by position return, book equity vs SPY, MTUM and the rule book), `weekly_review`; operator CLI; runbook. Chaos tests as in the old plan, minus the tranche and early-rule cases, plus a rebalance that dies between the sells and the buys |
| ~~5.6 Live EP path~~ | Dropped |
| 5.6 Public site data | As old 5.7: equity against SPY, MTUM and the rule book; closed positions only |

### Phases 6-8

- **Phase 6 (AWS):** as planned. The schedule is smaller (no 09:36, 09:51 or intraday-monitor slots).
- **Phase 7 (paper trial):** 7.0 pre-registers the forward statistics (§5.7) and the non-inferiority margin against the rule book. **The trial length must change:** "30 trades" takes about a year at 2-3 changes a month. The proposal is the later of **6 rebalances** or 3 months, judged on operations (every rebalance and stop correct, no unprotected position, no duplicate orders) and on the LLM book not being worse than the rule book. The performance evidence stays weak and keeps building at half slots live (§10 Q7).
- **Phase 8:** live at half slots; full slots after one clean quarter.

**Retired code** stays in the repo, unwired: the five scans (now facts for the Analyst), the quality ranking and cap, the verdict cache, the plan menus, the 09:36 triggers, tranches and the early rules. Deleting any of it waits for your OK. The breakout tracker keeps feeding the regime's leadership-health pillar.

### What is reused

| Reused as built | Rebuilt or new |
|---|---|
| Data layer, Parquet history, point-in-time universe, calendar, job runner, DB, AWS skeleton, `paper` | Momentum ranking and rule book (3.10) |
| Order desk: sell sequence, OTO legs, the lock, the stop invariant, `stop_guard`, reconciliation, kill switch | Book guardrails (3.11) |
| Regime classifier and job; crisis override | Portfolio simulator (3.12) and the backtest (3.13) |
| Indicators (RS, ATR, Trend Template) and charts, as facts | LLM schemas, prompts and jobs (Phase 4) |
| `sim/fills.py`, slippage tiers; `block_bootstrap`; the cluster rule | The scorecard (information coefficient) and the live rule book (4.5) |

## 10. Questions only you can answer

**Decided 2026-10-25** (the user delegated them; Claude chose for expected return, without loosening the validation):

| # | Decision | Why |
|---|---|---|
| 1 | Drawdown halt at **−30%** (manual re-enable; it blocks buys only, never sells or stops). G4's drawdown cap: **45%** | A −15% halt would sit out most recoveries, which is where momentum earns. 45% is about 1.3× SPY's 2020 fall |
| 2 | G2 (beat MTUM) **required** | If the book can't beat MTUM, holding MTUM makes more money |
| 3 | **Hold through earnings** (primary); C5 tests the cushion | Much of momentum's return comes around reports |
| 4 | The LLM picks from the **top 16** | Keeps the live book inside the tested strategy |
| 5 | Phase 4 **after the tuning gate passes** | No prompts paid for a strategy that fails |
| 6 | Comparisons chosen on **2017-2022 only**; the holdout is one clean test of the frozen book | The holdout is the last clean evidence |
| 7 | Paper trial: **the later of 6 rebalances or 3 months**, judged mainly on operations | "30 trades" would take about a year |
| 8 | **8 names, monthly, trades at 15:40 on the first session** | As proposed |

The original questions, for the record:

1. **Drawdown tolerance.** A momentum book often falls 25-40% from its peak. What is the most you'd accept in the backtest (G4)? And should the −15% drawdown halt stay (it would stop the strategy in most bad years), move to −25%, or become an alert only? *Recommendation:* the halt at −25% with manual re-enable, and G4's cap at 40%.
2. **MTUM.** If the book beats SPY but not MTUM, should it still go ahead, or would you rather hold MTUM? *Recommendation:* keep G2 (MTUM ≥ 0) required. A book that can't beat a cheap ETF isn't worth its running costs.
3. **Earnings.** Hold through reports (the evidence) or step aside (the README's safety rule)? With 8 names, one −30% earnings gap costs about 3.75% of equity, and the stop can't prevent it. *Recommendation:* hold through as the primary, the cushion as C5, and the LLM free to step aside per name.
4. **The LLM's latitude.** Pick from the top 16 (recommended; it keeps the live book inside the tested strategy), or a wider top 40?
5. **Phase 4 timing.** After the tuning gate passes (recommended), or built alongside the backtest as under #145?
6. **The holdout.** Choose the comparisons on 2017-2022 alone and keep the holdout as one clean test of the frozen book (recommended), or keep README §15's "beats on both periods"?
7. **The paper trial.** The later of 6 rebalances or 3 months, judged mainly on operations (recommended)? Or longer?
8. **Holdings and rebalance.** 8 names, monthly, trades at 15:40 on the first session: confirm, or change any of them before it's pre-registered.

## 11. Sources

- Jegadeesh & Titman (1993), "Returns to Buying Winners and Selling Losers", *Journal of Finance*
- Chan, Jegadeesh & Lakonishok (1996), "Momentum Strategies", *Journal of Finance*
- Moskowitz & Grinblatt (1999), "Do Industries Explain Momentum?", *Journal of Finance*
- Hong, Lim & Stein (2000), "Bad News Travels Slowly", *Journal of Finance*
- George & Hwang (2004), "The 52-Week High and Momentum Investing", *Journal of Finance*
- Lesmond, Schill & Zhou (2004), "The Illusory Nature of Momentum Profits", *Journal of Financial Economics*
- Korajczyk & Sadka (2004), "Are Momentum Profits Robust to Trading Costs?", *Journal of Finance*
- Novy-Marx (2012), "Is Momentum Really Momentum?", *Journal of Financial Economics*
- Asness, Moskowitz & Pedersen (2013), "Value and Momentum Everywhere", *Journal of Finance*
- Israel & Moskowitz (2013), "The Role of Shorting, Firm Size, and Time on Market Anomalies", *Journal of Financial Economics*
- Da, Gurun & Warachka (2014), "Frog in the Pan", *Review of Financial Studies*
- Barroso & Santa-Clara (2015), "Momentum Has Its Moments", *Journal of Financial Economics*
- Novy-Marx (2015), "Fundamentally, Momentum Is Fundamental Momentum", NBER working paper
- Daniel & Moskowitz (2016), "Momentum Crashes", *Journal of Financial Economics*
- Han, Zhou & Zhu (2016), "Taming Momentum Crashes: A Simple Stop-Loss Strategy", working paper (SSRN)
- McLean & Pontiff (2016), "Does Academic Research Destroy Stock Return Predictability?", *Journal of Finance*
- Novy-Marx & Velikov (2016), "A Taxonomy of Anomalies and Their Trading Costs", *Review of Financial Studies*
- Hoffstein, Faber & Braun (2020), "Rebalance Timing Luck: The (Dumb) Luck of Smart Beta", Newfound Research / SSRN
- Practitioners: Gray & Vogel, *Quantitative Momentum* (2016); Clenow, *Stocks on the Move* (2015); Antonacci, *Dual Momentum Investing* (2014); Faber (2007), "A Quantitative Approach to Tactical Asset Allocation"
