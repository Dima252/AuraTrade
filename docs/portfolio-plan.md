# The portfolio manager: the plan (#153)

**Status 2026-10-26: proposed; awaits the user's approval.** The input is [strategy-change-portfolio.md](strategy-change-portfolio.md); the user accepted every recommendation in its §4 on 2026-10-26, refining D7 (§2 below). The backtest is pre-registered in [backtests/portfolio-preregistration.md](backtests/portfolio-preregistration.md). On approval, task 3.17 writes the README, the decisions and `risk.yaml` `portfolio:` from this page.

## 1. The strategy in one paragraph

AuraTrade manages a $100,000 account (paper) to beat the S&P 500 over 10 years after Israeli tax. **The core** (80%) holds the US market at 1.3x while its trend is up and 0.5x while it is down, the rest in T-bills. The trend is a buffered 10-month moving average checked once a month. A volatility cap, a drawdown halt and a catastrophe stop on the leveraged fund keep the leverage careful. **The satellite** (20%) is allocated monthly by the LLM among 12 approved ETFs, and the LLM may shift the core by ±0.2x with a written reason, never above 1.5x and never toward more risk when code says to hold less. **A rules-only twin** (the core rule on the whole account) runs beside it on paper: the LLM's value is the book's after-tax excess over the twin.

## 2. The decisions (the user, 2026-10-26)

Every recommendation in `strategy-change-portfolio.md` §4 was accepted:

| Group | Decided |
|---|---|
| **A. Core** | A1 10-month SMA, monthly · A2 a ±2% band · A3 SPY + SSO (margin as a comparison) · A4 1.3x up, 0.5x down (1.5x / 0x as a comparison) · A5 a simple volatility cap, monthly · A6 T-bills · A7 80/20 · A8 a ±5-point drift band |
| **B. LLM** | B1 the satellite + ±0.2x on the core · B2 about 12 liquid ETFs, no leveraged or inverse products · B3 every fact source, cached · B4 latitude fixed for 12 months, then a pre-registered rule · B5 scored by the after-tax excess over the twin |
| **C. Risk** | C1 **catastrophe stops on the leveraged fund only**: CLAUDE.md §3.5 changes · C2 a -25% drawdown halt (no leverage) · C3 fail closed = never increase exposure; exits never blocked · C4 each satellite ETF ≤ 10% of equity |
| **D. Validation** | D1 Ken French daily from 1926 · D2 SSO synthesized, checked against the real fund · D3 no tuning, one scored run · D4 the three-part after-tax gate, 60% of windows · D5 25% on nominal USD gains, FIFO, offsets and carry-forward · D6 five comparisons · D7 below |
| **E. Operations** | E1 kill switch on `paper`, stack kept · E2 `paper` reset to $100,000 when the trial starts · E3 ingest slimmed · E4 the broker interface kept; IBKR only if real money · E5 the dashboard on the public site · E6 within the $150 cap |

**D7, as the user refined it:** "if it's a small difference we probably go with this plan and not only the S&P, because we need to showcase the LLM reasoning." The pre-registration makes it exact: a **near miss** (the drawdown condition holds and the after-tax CAGR is at most 1.0 point a year below the benchmark's) continues the plan unchanged, with the result stated plainly; a **clear fail** comes back to the user (§9 there).

## 3. The readings (choices I made to make §4 exact)

These were not in the brainstorm. Each is fixed in the pre-registration; the user may change any of them before approving it, and not after.

| # | Reading | Why |
|---|---|---|
| R1 | **The twin is the core rule on the whole account** (its "satellite" follows the core too), and the backtest tests exactly that | A clean benchmark: everything the LLM does (satellite picks and the core shift) shows up as excess over it |
| R2 | The signal uses the **total-return** index's month-end values | Dividends are part of the return; one series for signal and returns |
| R3 | The buffer is **±2%** around the SMA, not two months' confirmation | One parameter, decided before data; a band reacts within the month it's crossed |
| R4 | The volatility cap: **21-session realized volatility > 25% (annualized) → at most 1.0x**, checked on R | 25% is well above the market's long-run ~16-19%, so it binds only in turbulent spells |
| R5 | The synthetic 2x fund: **2 × market − 1 × (T-bill + 0.50%) − 0.90% fee**, no distributions | How a daily-reset swap fund works; the spread and fee near SSO's today; checked against SSO 2016 on |
| R6 | The catastrophe stop: **70% of the leveraged fund's price at the last rebalance**, raised monthly, on the close | A 2x fund at -30% means the market fell ~15% within the month; wide enough not to trigger on noise |
| R7 | The halt acts **at once** (sells the leveraged fund the next session) and clears on a DOWN → UP flip **or** a new equity peak | "Careful": a halt that waits a month isn't one; and it must not stay on forever in a recovery without a flip |
| R8 | The backtest trades **dollar amounts**, not whole shares | At $100,000 the rounding is < 0.1% of equity; the market file has no share prices before 1993 |
| R9 | Costs: slippage 0.05% on SPY and T-bills, 0.10% on SSO, from the existing `sim:` tiers | The one fill model (CLAUDE.md §6) |
| R10 | Tax: dividends from **Shiller's monthly yield**; tax paid at year-end without modelling the extra realization; the benchmark taxed identically | The market file has no dividend split; the optimism is small and symmetric |
| R11 | **D3 and D6 together:** with one scored run, comparisons can't be adopted (that would be choosing on the scored data). They are **reported only**, plus three diagnostics that decompose the result (trend alone, leverage alone, timing luck) | Keeps D3's promise of no tuning |
| R12 | The rolling windows are **independent runs** (fresh cash, fresh tax lots), not slices of one run | Taxes are path-dependent; a slice would inherit another start's lots |
| R13 | The report shows the **period after publication** (2016 on for Gayed & Bilello, 2007 on for Faber) on its own | The honest out-of-sample part of a published rule |

## 4. The LLM's role (live; Phase 4)

- **When:** once a month, on the evening of R (after the close and the evening data), so its decision is ready for E. It also runs on an **event**: the drawdown halt, a catastrophe stop, or a trend flip (to explain it; it can't undo it).
- **Sees:** the trend state, the SMA and the buffer distance, volatility, the halt, the twin's target; account state, holdings and their tax lots (unrealized gains, the year's realized gains and carry-forward); macro (FRED, VIX), news and ETF facts; its own past decisions with their outcomes against the twin; the regime classifier's view, as a fact.
- **Decides** (structured output):
  - the **core shift**: −0.2 to +0.2 on the rule level, but never above 1.5, and **only downward** while the rule level is DOWN, capped or halted;
  - the **satellite**: weights over the approved list (below), each ≤ 10% of equity, summing to the satellite's 20% (holding BIL counts);
  - a **written reason** for each change, a one-line expectation for each tilt (scored later), and the tax impact it accepted.
- **Can't:** buy outside the list, use leveraged or inverse products in the satellite, lift the exposure ceiling, block an exit or a stop, or supply a price (code prices every order).
- **The approved satellite list (proposal, final in task 4.2):** QQQ (Nasdaq-100), IWM (US small caps), AVUV (US small-cap value), MTUM (momentum), QUAL (quality), VEA (developed ex-US), VWO (emerging), GLD (gold), IEF (7-10y Treasuries), TLT (20y+ Treasuries), DBMF (managed futures), BIL (T-bills), plus SPY itself (neutral).
- **On failure** (an error, a refusal, invalid output, stale data): the book takes the twin's allocation for that month (fail closed: no added risk), and it is alerted.
- **Cost:** one decision a month plus rare events; estimated with the `claude-api` skill in task 4.1 and approved by the user before any paid call.

## 5. Risk limits (README §8, rewritten in 3.17)

| Limit | Value | Enforced by |
|---|---|---|
| Exposure to the S&P (core level) | 0.3x to 1.5x; ≤ 1.0x while capped or halted | `guardrails/portfolio.py` |
| The LLM's core shift | ±0.2x; downward only when the rule says hold less | same |
| Leveraged products | SSO only, only in the core, only while the level is above 1.0x | same |
| Satellite | 20% of equity; approved list only; each ETF ≤ 10%; no leveraged or inverse | same |
| Turnover | trades only on E (and the halt's next session), within the drift band | same |
| Catastrophe stop | a GTC stop on SSO's full quantity at 70% of its last rebalance price, raised only | the order layer + `stop_guard` (CLAUDE.md §3.5 as amended) |
| Drawdown halt | -25% from the peak → no leverage, at once | `core_allocation` + a daily check |
| Kill switch / halts | block **increases** of exposure only; never an exit, a sale toward the target, or a stop | as today |
| Fail closed | on any uncertainty: the twin's allocation or less, never more | the monthly job |

**CLAUDE.md §3.5 (proposed wording):** "Protective stops always rest at the broker **on leveraged positions**: every leveraged fund's GTC stop covers its full quantity at or above its plan level. Unleveraged ETFs carry no stop; the trend rule and the halt are their exits. Sells of a stopped position follow the sequence: reduce the stop, sell, restore it on failure. Every order change runs under the single-writer lock. No kill switch, halt or error path may cancel a protective stop or block an exit."

## 6. What to expect (honestly)

- The published results for trend + leverage on the US market are strong before tax and in-sample for their authors; the decade after publication includes the 2020 whipsaw, which cost such rules.
- After Israeli tax the margin is thin: each move out of stocks realizes taxed gains, and the benchmark defers its tax for the whole period. **A near miss is a realistic outcome**, and the plan continues through one (§2).
- If it passes, expect years 10-20 points behind the S&P (sharp V-shaped recoveries) and years far ahead (long bear markets, strong bull markets at 1.3x).

## 7. README changes (task 3.17)

Rewritten for the portfolio manager: §1 (principles), §2 (architecture), §3 (schedule: monthly decision, daily checks), §5 (components: ingest, the trend dial, the Portfolio Manager, the twin, the order layer, the journal, the dashboard), §6 (the trend dial replaces the regime overlay; the regime stays as a fact), §7 (the rebalance, exposure, the stop, exits), §8 (§5 above), §9 (the LLM design and its budget), §10 (tax lots, monthly decisions), §12 (cost), §13 (layout), §14 (`portfolio:`), §15 (validation: the pre-registration), §17 (limitations: the proxy, synthesis, tax approximations), §18 (sources), the glossary. The momentum book's sections move to `docs/archive/README-momentum-book.md`. Decisions: **#153** (the strategy change), **#154** (the rules, §2-§3 here), **#155** (the risk limits and the CLAUDE.md §3.5 change), **#156** (the pre-registration).

## 8. The execution plan from 3.17 on

The momentum book's tasks (3.9-3.16) stay ticked as the record; its planned Phases 4-7 move to `docs/archive/execution-plan-momentum-book.md`. The 1.7 / 1.8 live acceptance (stop hand check, unattended sessions, watchdog tests) is carried into 6.1 on the new jobs.

### Phase 3 (continued): the portfolio manager and its backtest

| Task | What | Acceptance |
|---|---|---|
| 3.17 👤 | The spec: README rewrite, CLAUDE.md (§1, §2, §3.5, §6, §7, §8), decisions #153-#156, `risk.yaml` `portfolio:` (typed, hashed), the archive, the tracker, the pre-registration's approved commit; kill switch on `paper` (E1) | The user approves the PR |
| 3.18 | Long-history data: Ken French daily, Shiller monthly dividends (`history/`, `scripts/backfill.py longhistory`), SPY/SSO/BIL adjusted bars 2016 on | Recorded-fixture tests; files land in the Parquet store; nothing about returns printed |
| 3.19 | The pure layer: the shared rebalance calendar (moved out of `momentum_book.py`), `strategies/core_allocation.py` (state, buffer, cap, halt, targets), `tax/israel.py` (FIFO lots, offsets, carry-forward, dividends, interest), `guardrails/portfolio.py` (§5) | Known-answer tests; 100% branch coverage on all three |
| 3.20 | `sim/portfolio.py`: the instruments of pre-reg §3.2, the drift band, the stop, the halt, taxes, the invariants of §7 raised as errors, on `sim/fills.py` | Known-answer portfolios for every rule (a flip, a cap, a halt and its two exits, a stop, a loss carried forward, a window run) |
| 3.21 | `backtest/portfolio.py` (+ report, render): the primary, C1-C5, D-a to D-c, the windows, the gate, the near-miss rule, `--blind` | Tested on a synthetic history end to end; the blind report holds no performance words |
| 3.22 | The blind check: `uv run python -m backtest.portfolio --blind` → `docs/backtests/portfolio-blind.md` | Pre-reg §7; a stop condition goes to the user |
| 3.23 👤 | The one scored run → `docs/backtests/portfolio.md`, committed as produced; the reading; the path of pre-reg §9 | With the user |

### Phase 4: the LLM layer (after 3.23 passes or nearly misses)

| Task | What |
|---|---|
| 4.1 | The Claude client: `anthropic` SDK, `claude-opus-5-5`, adaptive thinking with explicit effort, structured outputs, caching, refusals (the `claude-api` skill first); recorded fixtures; the cost estimate for the user |
| 4.2 | The Portfolio Manager decision: the facts it sees (§4), the schema, the prompt, every output through `guardrails/portfolio.py`, the twin fallback; the final ETF list |
| 4.3 | `jobs/rebalance.py` in `dry_run`: rule level → LLM → guardrails → target orders (not sent); the twin's fills simulated; the monthly journal (migration: decisions, targets, tax lots) |
| 4.4 | The scorecard: the book vs the twin vs SPY, before and after the estimated tax |

### Phase 5: execution

| Task | What |
|---|---|
| 5.1 | Rebalance orders: target weights → whole-share orders, sells first, marketable limits from the decision price, the drift band; SSO's stop placed and raised; `invariant.py` and `stop_guard` for C1; chaos tests on the fake broker |
| 5.2 | The daily checks (halt, stop guard, reconcile) and the slimmed ingest (the ETF list, macro, news); the schedule replaced; the setup system's slots retired |
| 5.3 | Live tax lots from the broker's fills; the after-tax estimate in the journal |

### Phase 6: the paper trial and the showcase

| Task | What |
|---|---|
| 6.1 👤 | `paper` reset to $100,000, deploy, kill switch off; the carried 1.7 / 1.8 checks; three monthly rebalances unattended |
| 6.2 | The public dashboard: the book vs the twin vs SPY (before and after tax), the exposure history, the monthly decision journal with the LLM's reasons, the backtest's result stated plainly |
| 6.3 | 12 months of paper: the B4 rule for the LLM's latitude is pre-registered before month 12 |

### Phase 7 (optional)

7.1 👤 An Interactive Brokers adapter behind `execution/broker.py`, and UCITS fund equivalents: only if real money is ever planned.

## 9. Questions only the user can answer

1. **Approve the pre-registration** (with the readings of §3), or change any reading first. After approval nothing changes without a recorded reason.
2. **The CLAUDE.md §3.5 wording** in §5: approve it, or edit it.
3. **E1 now:** may I turn the kill switch on for `paper` (it blocks new entries of the old skeleton; no positions are open)? It's a write to the `paper` database, so it needs your OK.

## 10. Sources

- Faber, M. (2007). A Quantitative Approach to Tactical Asset Allocation. *Journal of Wealth Management*.
- Gayed, M. & Bilello, C. (2016). Leverage for the Long Run. Dow Award paper.
- Moskowitz, T., Ooi, Y. H. & Pedersen, L. H. (2012). Time Series Momentum. *Journal of Financial Economics*.
- Hurst, B., Ooi, Y. H. & Pedersen, L. H. (2017). A Century of Evidence on Trend-Following Investing. *Journal of Portfolio Management*.
- Moreira, A. & Muir, T. (2017). Volatility-Managed Portfolios. *Journal of Finance*; Cederburg, S., O'Doherty, M., Wang, F. & Yan, X. (2020). On the Performance of Volatility-Managed Portfolios. *Journal of Financial Economics*.
- Cheng, M. & Madhavan, A. (2009). The Dynamics of Leveraged and Inverse Exchange-Traded Funds. *Journal of Investment Management*.
- S&P Dow Jones Indices, SPIVA U.S. Scorecard (active managers against their benchmarks).
- Kenneth R. French, Data Library (Fama/French 3 Factors, daily); Robert J. Shiller, online data (monthly S&P dividends).
