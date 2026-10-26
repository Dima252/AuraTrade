# The momentum book: pre-registration (tasks 3.10-3.16)

**Written 2026-10-25, before any momentum-book code exists or has read real data.** **Status: approved 2026-10-25** under the user's delegation ("decide for me; as profitable as you can", `docs/momentum-plan.md` §10). The commit that holds this text is quoted at the top of `docs/backtests/momentum.md`. From that commit:
- a change after the blind mechanics check (§2 step 3) needs the user's approval and is listed in the report with its reason;
- a change after the scored run (step 4) makes it a new trial, which this page doesn't allow (§9);
- a change after the holdout is opened voids the holdout as a test.

Sources: `docs/momentum-plan.md` (the rules, §2-§5, and the decisions in §10); `docs/strategy-change-momentum.md`; README §5.1, §5.2, §5.7, §15 and decisions #122, #130, #135, #140, #141, #144, #146-#149. The values below are in `config/risk.yaml` `book:`, which is committed with this page.

## 1. What is being tested

**The hypothesis:** a concentrated, long-only, 12-1 momentum book over the 1,000 most liquid US stocks earns more than SPY and MTUM after costs, on $5,000 in whole shares, with a wide broker stop on every position and no new buys in `CORRECTION`.

What is backtested is the **rule book**: the same list and limits the LLM gets live, taken in rank order. The LLM's choices can't be backtested honestly (the model has read what happened), so they are measured forward (§8).

## 2. Order of work

1. This page approved and committed, with the README rewrite and decisions #147-#149 (task 3.9).
2. **Build and test on hand-built data only** (3.10-3.13). No code reads 2016-2022 outcomes before step 3. Loading bars to check that a loader works is allowed if nothing about returns is printed.
3. **The blind mechanics check** (3.14): one run over the tuning period with the report's blind flag set. It writes counts and invariants only (§7): no equity, return, P&L, drawdown or comparison of any kind.
   - A bug found here may be fixed and step 3 re-run.
   - **If a rule can't work as written** (the thresholds in §7), work stops and it comes to the user before step 4. Nothing is adjusted silently.
4. **The scored run** (3.15): one invocation over the tuning period, for the primary, C1-C7 and the diagnostics. The report is committed as produced, before it is read for meaning.
5. **The reading** by §5 and §6. If a comparison is adopted, one more run of the primary with every adopted change (the **adopted configuration**), on the tuning period only.
6. **Only on a tuning pass** (3.16): freeze the configuration in `risk.yaml` (commit and hash recorded), then open the holdout **once**, for the frozen configuration only.

Nothing after 2022-12-30 is loaded before step 6, except to rank the first holdout rebalance in step 6 itself.

## 3. Definitions

### 3.1 Sessions and periods

- **Ranking session R:** the last session of each calendar month (from `market_sessions`). **Execution session E:** the next session, normally the first of the next month. If E is a half day, the next full session is E.
- **Tuning period:** every E from 2017-02-01 to 2022-12-01. The book starts with $5,000 cash at the close of 2017-01-31 and is marked to 2022-12-30. The first ranking needs 253 sessions; 2016 is the warm-up.
- **Holdout:** every E from 2023-01-03 to the last E before the latest backfilled session. The book starts again with $5,000 at the close of 2022-12-30 and is marked to the latest backfilled session.
- **Timezone and windows:** sessions are counted with the calendar helpers, never calendar days.

### 3.2 The ranking (on R, using data through R's close)

1. **Universe:** the point-in-time universe membership for R (§5.1: common stocks and ADRs, raw close ≥ $10, 50-session average raw dollar volume ≥ $10M, ≥ 252 sessions of history).
2. **History:** the security has ≥ 253 bars up to R (its own traded sessions).
3. **Liquidity cut:** the `book.universe_size` (1,000) securities with the highest 50-session average raw dollar volume (raw close × volume). All of them if fewer qualify. These are the **ranked set**.
4. **Signal (12-1):** close[−22] ÷ close[−253] − 1 on the security's split-adjusted closes, where close[−1] is R's close. That is the return from 252 bars before R to 21 bars before R.
5. **Percentile:** `indicators.relative_strength.percentile_ranks` over the ranked set (0 to 100, ties averaged). **Rank order:** by the signal, highest first; ties are broken by the symbol, alphabetically.

### 3.3 The book on E

In this order, on each E:
1. **Stops** are checked for the session first (§3.5). A holding stopped on E is gone before the rebalance.
2. **Rule sells:** every holding whose percentile on R is below `book.hold_percentile` (90), or which isn't in the ranked set, is sold at E's close minus slippage.
3. **Stop raises** for the holdings kept (§3.5).
4. **Buys,** only if allowed (§3.6):
   - **Candidates:** the first `book.candidates` (16) securities in rank order that pass all of these: not held; a bar on E; at least 1 share fits in a slot at the limit; not linked by the cluster rule (§3.7) to 2 holdings.
   - **The rule book** takes candidates in order, re-checking the cluster rule against holdings plus the names already chosen, until it holds `book.holdings` (8) names or the candidates run out. Empty slots stay in cash.
   - **Slot value** = equity at E's close (after step 2) × `book.max_exposure` (1.0) ÷ 8. **Limit** = E's raw close × (1 + 0.005), rounded up to the tick. **Shares** = ⌊min(slot value, cash left) ÷ limit⌋ on raw prices. 0 shares: skipped, and the next candidate is tried.
   - **Fill:** E's close plus the liquidity-tier slippage (`sim.slippage`, #141), rounded to the tick against the buyer, if at or under the limit (with the current tiers it always is).
5. **Retry:** if buys were blocked on E by a loss halt (§3.6) or had no bar, they are tried once more at E+1's close, with the same R ranking and fresh sizes. After that, slots wait for the next E. Overlay blocks are not retried.

Between rebalances only the stops act. There is no resizing, no trimming, and no refilling after a stop-out.

### 3.4 Prices and units

- Sizing, limits, ticks and stop levels use **raw prices as traded** on E (#122). Quantities and marks are then carried in split-adjusted units, so a later split changes nothing in the book.
- Cash earns nothing. No dividends are credited (the book is price only; §4).
- **Fills** go through `sim/fills.py`: `buy_fill` and `sell_fill` with the tier's slippage, `stop_on_bar` for stops.

### 3.5 The stop

- **Initial:** E's raw close × (1 − `book.stop_fraction`) (0.20), rounded down to the tick. It is active from the session after the fill.
- **Raised** on each later E for every holding kept: to E's raw close × 0.80 (rounded down to the tick) if that is higher. Never lowered.
- **Triggered** on a session whose low is at or below the stop: filled at min(stop, open) minus slippage (`stop_on_bar`: a gap goes through).
- **After a stop-out** the slot is cash until the next E.

### 3.6 What blocks buys (never sells or stops)

- **Overlay (primary):** the computed regime on R's evening is `CORRECTION`, or the crisis override is active for E (README §5.2). The regime is the 5-pillar variant `cu+2_corr-2_c2_dist8` (`docs/backtests/regime-5-pillar.md`), replayed from 2016.
- **Daily loss halt:** the book's equity at E's close is ≥ 2% below the previous close.
- **Weekly loss halt:** at E's close the book is ≥ 5% below its equity at the last close of the previous week.
- **Drawdown halt:** equity ≥ `book.drawdown_halt` (30%) below its running peak at E's close. Live it waits for a manual re-enable. **In the backtest it blocks that E's buys and its retry only.** It counts as re-enabled at the next E, and the report counts how often it fired.
- **Half days:** no trades (E moves, §3.1).

### 3.7 The cluster rule

The existing guardrail (`guardrails/clusters.py`, #140): two names are linked if they're in the same industry group or their 60-session daily-return correlation is ≥ 0.7. A correlation that can't be computed counts as linked. Industry groups are the ones the 3.8 backtest used: the history's SIC-based groups, which are current, not point in time. A name with no group is linked only by correlation. At most `guardrails.max_per_cluster` (2) linked holdings.

### 3.8 Securities that end

The history splits a ticker into a new security after a gap of more than 30 sessions or a one-bar move of 4× or ¼× (#130). A held security:
- **Ends with a successor** (the next piece of the same ticker): exit at min(its last close, the successor's first open), minus slippage. This takes a real collapse at its post-collapse price, and doesn't credit an unadjusted reverse split.
- **Ends without a successor before the period's end** (a delisting): exit at its last close, minus slippage.
- The blind check counts both cases.

## 4. Benchmarks and data

- **SPY and MTUM:** daily closes with `adjustment=all` (total return), fetched once from Alpaca into the Parquet history (`history` keeps them apart from the split-only bars). MTUM has traded since 2013-04.
- **The equal-weighted ranked set:** a notional reference. Each E, equal weights in the ranked set, held to the next E, price only, no costs. It shows what selection adds; it doesn't gate.
- **Known bias:** the book is price only and the benchmarks are total return. That understates the book by about its dividend yield (momentum leaders pay little). The report states it.

## 5. Metrics and the gate

**Daily equity** = cash + Σ quantity × close. Reported per configuration, for the whole period and per calendar year (2017 from 2017-01-31):
- CAGR = (E_end ÷ E_start)^(252 ÷ sessions) − 1; volatility; max drawdown on daily closes; CAGR ÷ max drawdown;
- excess CAGR over SPY and over MTUM (book CAGR − benchmark CAGR);
- turnover (names bought a year), slippage paid (as % of the starting equity a year), average holding sessions, mean cash share, stop-outs per year, the largest single weight.

**Intervals (reported, gate nothing):** `journal.scorecard.block_bootstrap` on the daily excess return over SPY, with non-overlapping **63-session blocks** from the first tuning session (the last, shorter block kept), `book.backtest.bootstrap_samples` (2,000) draws, seed `book.backtest.seed` (39), 90%, statistic = mean daily excess × 252. The report also gives the minimum detectable annual excess (1.645 × annualized tracking error ÷ √years).

**The gate, on the tuning period** (for the adopted configuration, or the primary if nothing is adopted; all required):

| | Condition |
|---|---|
| **G1** | Excess CAGR over SPY > 0 |
| **G2** | Excess CAGR over MTUM ≥ 0 |
| **G3** | Excess return over SPY positive in ≥ `book.backtest.min_positive_years` (3) of the 6 calendar years 2017 (from 31 January) to 2022, **and** the excess still > 0 when the year with the largest excess is left out (compounding the other years' returns) |
| **G4** | CAGR ÷ max drawdown ≥ SPY's, and max drawdown ≤ `book.backtest.max_drawdown` (45%) |

**If the adopted configuration fails while the primary passes,** the primary is frozen instead, and the report says so.

**The holdout** (once, for the frozen configuration only): **H1** excess CAGR over SPY > 0, **H2** excess CAGR over MTUM ≥ 0. Max drawdown and per-year figures are reported. 2023-25 was kind to momentum, so a pass is weak evidence and a fail is strong.

## 6. The comparisons and the adoption rule

Each comparison changes one thing from the primary, on the tuning period:

| # | Change | Exact rule |
|---|---|---|
| C1 | Signal: nearness to the 52-week high | close[−1] ÷ the highest close of the last 252 bars; rank ties broken by the 12-1 signal, then the symbol |
| C2 | Stop at 10% | `stop_fraction` 0.10 (initial and raises) |
| C3 | Overlay off | Neither the regime nor the crisis override blocks buys |
| C4 | Overlay = SPY trend | Buys blocked when SPY's split-adjusted close on R is at or below its 200-session SMA; the crisis override kept |
| C5 | Earnings rules on | No buy of a name whose next known report (EDGAR history, timing per README §5.1) falls in the 5 sessions after E; a name with no report history isn't blocked (EDGAR lacks delisted names, #130). A holding is sold at the close of the last session before a known report (README §7.4's timing) if its gain since entry is under `book.earnings_cushion_min_gain` (+10%) |
| C6 | Whole universe | No liquidity cut: the ranked set is every security passing §3.2 steps 1-2 |
| C7 | Cluster cap off | The cluster rule isn't applied |

**Adoption** (tuning period only; the holdout is never used to choose):
- **C1, C2, C4, C6:** adopted if the 90% block-bootstrap interval (as in §5) of the daily return difference (variant − primary) lies wholly above zero, and its max drawdown is no deeper than the primary's.
- **C3, C7 (removing a safety rule):** adopted on the same test. Otherwise the safety rule stays.
- **C5 (adding the README's earnings rules):** adopted if its max drawdown is shallower than the primary's and the difference interval doesn't lie wholly below zero. Otherwise hold-through stays.
- C3 and C4 can't both be adopted. If both pass, the one with the larger mean difference is adopted.
- The report states that 8 configurations were run (plus the adopted one), as a warning about multiple trials.

## 7. The blind check: what it reports and when it stops

**Reported (counts only):** per year and overall: E sessions; ranked-set size (min, median); candidates and buys per E; holdings at each E after buys (mean, min); skip reasons (no price fit, cluster, no bar); buys blocked by the overlay, each halt and half days; retries; stop-outs; securities ending with or without a successor; average holding sessions; turnover. The decision-price question was settled in 3.8c (2% disagreement); it isn't re-run, since every rule here acts on the close.

**Invariants (any failure is a bug):** never more than 8 holdings; cash never negative; at entry, no two-cluster violation (unless C7); every holding has a stop below its entry; no stop ever lowered; no buy on a blocked session; no fill above its limit.

**Stop and bring it to the user if:** mean holdings after buys < 6; > 25% of the names the rule book reached were skipped for price; the cluster rule removed > 50% of candidates; or any ranked set had < 800 names.

## 8. Forward evidence (not part of the gate)

From task 4.7, in `dry_run`: the Book Analyst's conviction against each reviewed name's return to the next E, relative to the top-40 average. The headline is the monthly rank correlation (information coefficient), with its mean and t-statistic. Alongside it: the LLM book against the rule book, both through `sim/book.py`. The bar for go-live is pre-registered in task 7.0, from this data.

## 9. On a fail, and what isn't allowed

- **Tuning gate fails:** the holdout stays closed; no capital; **no further variants on 2017-2022.** It comes back to the user, with the options of a passive SPY/MTUM holding or a pause.
- **The holdout fails:** no capital; the holdout is spent; it comes back to the user.
- **Not allowed:** a comparison not in §6; a re-run of the scored run with changed rules; reading 2023+ before step 6; tuning any value in `book:` on the results.
