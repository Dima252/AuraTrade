# The portfolio manager: pre-registration (tasks 3.18-3.23)

**Written 2026-10-26, before any portfolio code exists or has read data.** **Status: approved by the user 2026-10-27** (decision #156). The commit that holds the approved text is quoted at the top of `docs/backtests/portfolio.md`. From that commit:
- a change after the blind mechanics check (§2 step 3) needs the user's approval and is listed in the report with its reason;
- a change after the scored run (step 4) makes it a new trial, which this page doesn't allow (§9).

Sources: `docs/strategy-change-portfolio.md` (the decisions of §4, all accepted by the user 2026-10-26) and `docs/portfolio-plan.md` (the readings that fix what §4 left open, §3 there). The values below go into `config/risk.yaml` `portfolio:` in task 3.17, committed with the approved text.

## 1. What is being tested

**The hypothesis:** a portfolio that holds the US stock market at 1.3x while its trend is up and at 0.5x while it is down (the rest in T-bills), switching on a buffered 10-month moving average checked monthly, with a volatility cap, a drawdown halt and a catastrophe stop on its leveraged part, **ends richer after Israeli tax than buying the market and holding it**, with no deeper drawdown, over 1928-2026 and over most 10-year windows within it.

What is backtested is the **rules-only portfolio** (the twin): the core rule applied to the whole account. The LLM's part (the satellite, and its ±0.2x shift of the core) can't be backtested honestly, because the model has read what happened; it is measured forward against the twin (§8).

**No parameter is tuned.** Every value comes from a source or is fixed here before any data is read. The comparisons (§6) are reported, never adopted: there is one period and one scored run (decision D3), so adopting a comparison would be choosing on the scored data.

## 2. Order of work

1. This page approved and committed, with the README rewrite and the decisions (task 3.17).
2. **Build and test on hand-built data only** (3.18-3.21). Loading the data to check that a loader works is allowed if no return of the strategy or the benchmark is printed. The data checks of §4.4 belong to step 3.
3. **The blind mechanics check** (3.22): one run over the whole period with the blind flag set. It writes counts, invariants and the data checks only (§7): no equity, return, drawdown, tax total or comparison of the strategy.
   - A bug found here may be fixed and step 3 re-run.
   - **If a rule can't work as written** or a data check fails (§7), work stops and it comes to the user before step 4.
4. **The scored run** (3.23): one invocation, for the primary, the comparisons, the diagnostics and the rolling windows. The report is committed as produced, before it is read for meaning.
5. **The reading** by §5, and the path of §9.

## 3. Definitions

### 3.1 Sessions and periods

- **Sessions** are the dates of the daily market file (§4.1). Every session is treated as a full session (the file has no intraday data).
- **R** is the last session of each month; **E** is the next session (the first of the next month). The signal is computed on R's close and traded at E's close. (The live system uses the existing calendar: R/E as in `rebalances()`, half days moving E.)
- **Warm-up:** 1926-07-01 to 1927-12-30 (the 10-month average needs 10 month-ends; the volatility needs 21 sessions).
- **The scored period:** from the first E of 1928 (cash at the close of R = the last session of 1927) to the last R in the market file at download, recorded in the report. The final day is the **end**.
- **Rolling windows:** a 10-year window starts at every R from the start through the R 120 months before the end. Each window is an **independent run** of both the strategy and the benchmark: cash at its first R, the trend state initialized at that R with no buffer (§3.3), liquidated and taxed at its last R.

### 3.2 The instruments (synthesized from the market file, §4)

| Instrument | Daily return | Live equivalent |
|---|---|---|
| **MKT** (the S&P proxy) | the market's total return − 0.09%/252 (SPY's fee) | SPY |
| **L2** (2x daily) | 2 × the market's total return − 1 × (the T-bill rate + 0.50%)/252 − 0.90%/252 (fee) | SSO |
| **TB** (T-bills) | the T-bill rate − 0.14%/252 (fee) | BIL (SGOV where its history allows) |

The financing spread (0.50%) and the fees are fixed here. L2 holds no distributions: its whole return is in its price and is taxed when sold (§3.7).

### 3.3 The trend state (on R, from data through R's close)

- **The index** is the cumulative MKT total-return index; **its month-end value** is its value on each R.
- **SMA10** is the mean of the last 10 month-end values, including R's.
- **The buffer:** the state turns **DOWN** when the month-end value < SMA10 × 0.98, turns **UP** when it is > SMA10 × 1.02, and otherwise stays as it was.
- **The first state** of a run is UP if the value ≥ SMA10, else DOWN (no buffer).
- **A flip** is a change of state on R.

### 3.4 The exposure (on R; traded on E)

The target exposure to MKT, in this order:

1. **The trend level:** UP → **1.3**; DOWN → **0.5**.
2. **The volatility cap:** if the annualized standard deviation of MKT's last 21 daily returns through R (× √252) is above **25%**, the level is at most **1.0**.
3. **The drawdown halt:** if the halt is on, the level is at most **1.0**. The halt turns on at any session's close where equity ≤ 75% of its running peak, and **acts at once**: at the next session's close any L2 is sold into MKT (the one trade outside E). It turns off at the first R where the state flips DOWN → UP (the peak is then reset to the equity at that R), or where equity is back at its old peak.

**The holdings for a target x:**
- x > 1: MKT (2 − x) and L2 (x − 1) of equity. For 1.3: 70% MKT, 30% L2.
- x ≤ 1: MKT x and TB (1 − x). For 0.5: 50% MKT, 50% TB; for 1.0: 100% MKT.

### 3.5 Trading on E

- **When:** on E (and on the halt's next session, §3.4), if the target holdings changed, or if any holding's weight differs from its target by more than **5 points** (the drift band). Otherwise no trade.
- **How:** to the exact target weights, sells first, at E's close with the slippage of §3.6.
- **Units:** dollar amounts, not whole shares. At $100,000 in SPY-sized prices the rounding is under 0.1% of equity and is ignored. Live orders are whole shares (CLAUDE.md §6).
- **Starting equity:** $100,000.

### 3.6 Costs

Slippage as a fraction of the price, on every buy and sell, from the `sim:` tiers by each fund's liquidity today: **MKT 0.0005, TB 0.0005, L2 0.001**. No commissions. The same costs apply to the benchmark's single buy and its final sale.

### 3.7 The catastrophe stop (on L2 only)

- While L2 is held, a stop rests at **70% of L2's price at the last E** where L2 was bought or the stop was set. On each later E where L2 is still held, it is raised to 70% of that E's L2 price if higher; never lowered.
- **Triggered** when L2's close is at or below the stop (the market file has no intraday lows); **filled** at that close with L2's slippage. The proceeds go to TB until the next E.
- MKT and TB carry no stop (decision C1).

### 3.8 The tax model (Israel, decision D5)

- **25% on realized gains in nominal USD**, lot by lot, **FIFO** per instrument. A loss offsets gains in the same calendar year; a net loss for the year is carried forward without limit against later gains.
- **Dividends:** MKT pays the S&P dividend yield (§4.2), accrued daily on its value and taxed at 25% when paid. The tax-paid remainder stays in MKT. (The US withholding of 25% under the treaty is assumed fully credited, so the net rate is 25%.)
- **Interest:** TB's whole return is interest, taxed at 25% in its year.
- **Payment:** the year's tax is paid on the year's last session, from TB first, then pro rata from the other holdings. Selling to pay tax realizes no further gain in this model: a small optimism, applied to the benchmark too.
- **At the end** (and at each window's end) everything is sold and taxed.

### 3.9 The benchmark

MKT bought with all the cash at the first E, dividends taxed yearly as in §3.8 (the remainder reinvested in MKT), sold and taxed at the end. Its slippage: one buy, one sale. It is "SPY bought, held and sold at the end" for an Israeli investor.

## 4. Data

### 4.1 The market file

- **Kenneth French's data library, "Fama/French 3 Factors [Daily]"** (free, no key): Mkt-RF and RF, from 1926-07-01. The market's total return = Mkt-RF + RF; the T-bill rate = RF. The market is the CRSP value-weighted US market, the long-history proxy for the S&P 500.
- The file's download date and last session are recorded in the report.

### 4.2 Dividends

- **Robert Shiller's monthly S&P data** (free): the dividend yield, D ÷ P, for each month, applied to every session of that month. Only the tax on dividends uses it; the total return comes from §4.1.

### 4.3 The recent funds

- **SPY, SSO and BIL** daily bars with all adjustments (dividends and splits) from Alpaca, 2016 on (the existing `backfill.py benchmarks` path, extended). They are used only for the checks of §4.4, never for the strategy's returns.

### 4.4 The data checks (run in the blind check)

1. **The proxy:** MKT against SPY's total return, 2016 to the end: daily correlation ≥ 0.98, and the gap in annualized return ≤ 1.5 points.
2. **The synthesis:** L2 against SSO's total return, 2016 to the end: daily correlation ≥ 0.98, and the gap in annualized return ≤ 1.5 points.
3. **Completeness:** no missing or non-finite value in the scored period; no gap of more than 7 calendar days between sessions outside the known closures (1933's bank holiday, 2001-09-11 to 09-14, and the others the report lists).

A failed check stops the work before step 4 (§7).

## 5. Metrics and the gate

### 5.1 Metrics (strategy and benchmark)

On daily equity marks (after tax paid during the run, before the final sale) and on the final after-tax wealth:
- **CAGR after tax:** (final wealth after the final sale's tax ÷ $100,000)^(1/years) − 1.
- **CAGR before tax:** the same run with the tax set to zero.
- **Max drawdown:** the largest peak-to-trough fall of the daily marks.
- **Volatility**, **CAGR ÷ max drawdown**, the **worst calendar year**, **flips a year**, **tax paid a year** (as a share of equity), **time** in UP / DOWN / volatility-capped / halted, **stop-outs**.

### 5.2 The gate (all three must hold)

| | Condition |
|---|---|
| **G1** | After-tax CAGR over the scored period > the benchmark's |
| **G2** | Max drawdown ≤ the benchmark's |
| **G3** | In **≥ 60%** of the rolling 10-year windows, the strategy's after-tax final wealth > the benchmark's |

### 5.3 The near miss (decision D7, as refined by the user)

If the gate fails, it is a **near miss** when **G2 holds and the after-tax CAGR is at most 1.0 point a year below the benchmark's**. Anything else is a **clear fail**. What follows from each is in §9.

### 5.4 Reported beside the gate (it decides nothing)

- the before-tax metrics;
- **the period after publication:** 2016 on (Gayed & Bilello's paper) on its own, and 2007 on (Faber's);
- **per decade**, and **the crash table**: 1929-32, 1937-38, 1973-74, 1987, 2000-02, 2007-09, 2020, 2022 (each: the strategy's and the benchmark's fall and months to recover);
- the excess daily return over the benchmark (before tax), mean × 252, with a 90% block-bootstrap interval (63-session blocks, 2,000 draws, seed 37);
- the distribution of the window excess (G3): median, 10th and 90th percentiles.

## 6. The comparisons (reported, never adopted)

Each changes one thing from the primary and runs over the same period with the same metrics. They show what each part contributes; none can replace the primary (§1).

| | Change |
|---|---|
| C1 | The trend from the 200-session average of the daily index on R (Gayed & Bilello), the same buffer |
| C2 | Exposure 1.5 in UP, 0 in DOWN |
| C3 | Leverage by margin: MKT at x, borrowing (x − 1) at the T-bill rate + 1.5%; a margin call (equity < 25% of MKT's value) sells to 1.0 at that close |
| C4 | No catastrophe stop |
| C5 | No volatility cap |

**Diagnostics** (to decompose the result):
- **D-a** the trend alone: 1.0 in UP, 0 in DOWN, no leverage (Faber's rule with the buffer);
- **D-b** leverage alone: a constant 1.3, no trend, the stop and the halt on;
- **D-c** timing luck: the nominal E moved to the 6th, 11th and 16th session of the month.

## 7. The blind check: what it reports and when it stops

**Reports:** the market file's dates and session count per decade; the data checks of §4.4 (pass or fail, with their statistics); flips per decade; the share of R's in UP, DOWN, volatility-capped and halted; trades per year; stop-outs; tax lots per instrument; the invariants below; the number of rolling windows.

**Invariants** (a failure is a bug: fix and re-run):
- every session's holdings sum to equity (to $0.01), with no negative holding;
- the open lots of each instrument sum to its holding;
- exposure never above 1.5, nor above 1.0 while capped or halted;
- the stop never lowered; L2 held only when the target is above 1.

**Stops the work and goes to the user** (the rule can't work as written):
- a data check of §4.4 fails;
- flips average more than 4 a year over the period (the buffer is not doing its job);
- the halt is on for more than 30% of the R's.

## 8. Forward evidence (not part of the gate)

From the paper trial on, the LLM's book is compared each month with the twin (the primary rules on the whole account), both after the estimated Israeli tax: the excess over the twin, its 90% interval once 24 months exist, and the tilts and their outcomes. The LLM's latitude stays fixed for the first 12 months (B4).

## 9. On a fail, and what isn't allowed

- **A pass:** the plan continues as written (Phases 4-6).
- **A near miss (§5.3):** the plan continues as written, the user's choice of 2026-10-26: the project's purpose is to show the LLM's reasoning, and the core rule is kept with the result stated plainly in the README and on the public site ("failed its gate narrowly: X points a year after tax").
- **A clear fail:** it comes back to the user. Among the options then: a 1.0x core (the trend alone, D-a, or plain MKT) with the LLM satellite.
- **Never:** a second scored run with changed values, a change chosen from the comparisons or diagnostics, or a new period chosen after seeing this one.
