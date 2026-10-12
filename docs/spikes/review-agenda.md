# Task 0.4: spike review agenda

The points to decide at the 0.4 review. Each spike note has the evidence; this page lists the decisions in one place. Nothing here changes the README until the user approves it (CLAUDE.md §5).

Status: being collected while the last spikes run (2026-10-09 to 2026-10-15). On 2026-10-10 the decisions and readings from the out-of-order build (2.2, 2.3, 3.1, 3.5a; PRs #12–#15) were added at the end. What is left overall: [next-steps.md](../next-steps.md).

## Review progress

**Session 1, 2026-10-10:** approved by the user, all as proposed unless noted:
- **#9** Keep $5,000 for the paper trial and live. The simulator and backtests size with whole shares at the real account size, so 0-share skips appear in the results. A trade sized below 2 shares goes in as a **single tranche** at the trigger. Evidence: the 8,177 edge-check trades re-sized at raw prices with the 25% position cap. At $5,000, 1% / 3% / 10% of trades get 0 shares at 1% / 0.5% / 0.25% risk; 3% / 10% / 28% are under 2 shares; flooring takes about 85–90% of the planned risk; the skipped trades' expectancy doesn't differ from the rest. At $10,000 the 0.5% skip rate is 1%.
- **#10** Adopted.
- **#6, #7, #7b, #8** Adopted (#8: alert above $1.00 per Analyst batch and $0.50 per real-time call).
- **"Already decided" and B1–B12:** recorded as built.
- **B13, B14** (unreliable count = half risk), **B15, B16, B17, B18** (accept the side effect), **B19, B20, B21:** adopted.

**Still open:** #1–4 (after Thursday's order checks) and #5 (after Monday's VIX watch). Then one PR updates the README, the decision log (#113 onward) and the plan.

## Proposed design changes (need approval)

| # | Topic | Finding | Proposal | Evidence |
|---|---|---|---|---|
| 1 | OTO stop leg | The leg's time-in-force **and quantity** can't be changed after the fill (`422`) | Entries as GTC-parent OTOs. Before the first reduce or merge, swap the leg for a standalone GTC stop (cancel, then place: about 1 s with no stop, under the lock, covered by `stop_guard`); from then on every change is an atomic replace | [alpaca-orders.md](alpaca-orders.md) |
| 2 | Adds while a stop rests | A plain buy is rejected as a "potential wash trade" while a sell stop rests; an OTO buy is accepted | Every buy in a held symbol (tranche 2, adds) goes in as an OTO with its own stop leg | alpaca-orders.md |
| 3 | Partial fills | The stop leg stays `held` (inactive) while the parent is only partly filled | A partly filled entry counts as unprotected: cancel the rest and place a standalone stop. Still open: what cancelling a partly filled parent does to its leg | alpaca-orders.md |
| 4 | Splits | On the ex-date Alpaca **rejects resting stops at 04:00 ET**. **Paper never applies the split to the position:** still 1 share at the old cost two sessions later (Mon 10-12), with no split entry in the account activities | Keep §5.7.10 (re-place a scaled stop at 09:20). Add: before any invariant repair, check the corporate-actions feed for a split that day, so an unadjusted position isn't read as "price through the stop" and sold. On paper, a split on a held name also leaves broker and DB quantities apart, so reconciliation must expect it (scale by the feed's ratio when comparing) and alert, instead of failing closed for the rest of the trial | alpaca-orders.md |
| 5 | Evening VIX | CBOE's history files updated at 18:31 / 21:11 ET, after the 17:25 regime job; FRED lags a full session (timing watch Mon 10-12) | The evening VIX/VIX3M come from CBOE's delayed-quote JSON (final about 16:30); the history file and FRED are fallbacks | [macro.md](macro.md) |
| 6 | Daily-bar volume | Volume of bars fetched at 16:58 grew later (187 of 195 bars; up to +62%) | The evening ingest re-fetches session T−1's bar | [alpaca-data.md](alpaca-data.md) |
| 7 | Earnings timing | Finnhub's free calendar has no past dates and leaves before/after-market timing blank for 61% of upcoming reports; 8-K Item 2.02 timing matches news in 96% of events | History from 8-K Item 2.02; timing = the earlier of the 8-K acceptance and the first earnings headline; upcoming timing falls back to the company's own 8-K habit | [fundamentals.md](fundamentals.md) |
| 7b | Decision price | The latest IEX **trade** is a median 4 min old (p90 66 min) outside the 500 most liquid names; IEX **quotes** stay fresh (median 6 s) | Price = last IEX trade if ≤ 2 min old, else the IEX quote midpoint if ≤ 1 min old with a sane spread, else no price (skip entries; a soft exit waits, the hard stop rests) | alpaca-data.md |
| 8 | Claude cost | Analyst requests are about 3,400 input tokens (README assumed 8,000); a Portfolio Manager call took 39 s and $0.10 | Recalibrate README §9 and set Phase 4's daily cost limit from the measurement | [claude-cost.md](claude-cost.md) |
| 11 | **OPG exits pre-market** | Saturday test: a stop replace on a closed market stays `pending_replace` and keeps every share reserved, so the OPG sell is rejected (`403`) and the stop can't be restored (`422 order chain not fully replaced`) | **Approved by the user 2026-10-10: option A.** Drop OPG exits; planned morning exits and trims run at the 09:31 check run as a regular-hours sell sequence (marketable limit). README §3, §5.7, §8 and the decision log are updated at 0.4 together with the other changes | alpaca-orders.md |

## Points raised by the user or during the build (need a decision)

| # | Topic | Detail |
|---|---|---|
| 9 | **Small account vs share prices** | Shares are whole numbers (GTC stops can't be fractional). At $5,000 and 0.5–1% risk ($25–50), a $620 stock with a 5% stop (about $31 a share) gets 0–1 shares, and a $1,048 stock gets 0. The system would automatically skip many high-priced leaders. Options: accept it and make the backtests simulate the real account size, including 0-share skips; or run the paper trial with a larger balance. Raised 2026-10-09. |
| 10 | **PDT rule is gone** | The SEC approved FINRA's Rule 4210 amendment on 2026-04-14 (Release 34-105226, Regulatory Notice 26-10): the pattern-day-trader test (4 day trades in 5 days) and the $25,000 minimum are replaced by an intraday margin standard for all margin accounts, effective 2026-06-04 with phase-in to 2027-10-20. Spike 2026-10-09 (`pdt`, $4,875 dev account): **6 same-day round trips, none blocked**; the account API has no `daytrade_count` / `pattern_day_trader` fields (only margin fields: `multiplier` 4, `buying_power`, `regt_buying_power`); a GTC stop on same-day shares and a same-day exit were accepted. **Proposal:** drop the PDT allowance from README §5.7.9 / §8 and the "PDT status" input of §5.6; replace it with a check that entries never create an intraday margin deficit (the design never needs margin: exposure ≤ 100% of equity). Before Phase 8, confirm Alpaca's live-account implementation (paper may not model intraday margin calls). This also removes the PDT reason behind the user's $5,000 question (#9). |

## Already decided (record in the decision log at 0.4)

| Topic | Decision | Where |
|---|---|---|
| Live capital | $5,000, confirmed by the user on 2026-10-09 | tracker 0.2 |
| Long-weekend input | `sessions_until_next_open` → `days_until_next_open` (calendar days; in sessions the next open is always 1 away). Approved by merging PR #9 | README §4, task 1.4 |
| Climax setting | `fast_gain_min_move_sessions = 15`, provisional; the baseline backtest (3.8) chooses the value | `config/risk.yaml`, task 1.2 |
| New database | Starts with entries disabled (kill switch on) until `auratrade kill off` | README §14, task 1.2 |
| Out-of-order tasks | 2.1 Indicators built before Phase 1 finished; 2.2, 2.3, 3.1 and 3.5a built in parallel on 2026-10-10 (PRs #12–#15). All with approval | tracker |
| OPG exits | Option A (#11 above) | alpaca-orders.md |
| Testing gates | Added to the plan on 2026-10-10 at the user's request, after a review of the tests so far: a real-data smoke run of the early-built modules (1.6); a minimal fake broker in `sim/` and automated chaos tests of the order core (1.7), before it runs unattended in 1.8; CI coverage floors (95% overall now, 100% on `guardrails/` from 3.5b). Plan-only: no README behaviour changes | execution-plan.md |

## From the out-of-order build (2.2, 2.3, 3.1, 3.5a)

Each PR description has the full reasoning: [#13](https://github.com/Dima252/AuraTrade/pull/13) (2.2 bases), [#15](https://github.com/Dima252/AuraTrade/pull/15) (2.3 regime), [#14](https://github.com/Dima252/AuraTrade/pull/14) (3.5a plans). The code already implements every row below; the README and decision log don't record them yet.

### Decided by the user during the build (record in the decision log)

| # | Task | Decision |
|---|---|---|
| B1 | 2.2 | **Interim-low reset.** The base count also resets when any intraday low *between* two bases undercuts the prior base's low, not only a low inside the next base. Without it, a crash that recovers into a higher base keeps counting up. |
| B2 | 2.2 | **`pivot_min_position: 0.5`.** The final contraction's high is the pivot only in the upper half of the base's range; otherwise the pivot is the base high. This lowers counts, so it allows more risk. |
| B3 | 2.3 | **"50 SMA rising/falling"** compares the 50 SMA with its value 10 sessions earlier (`index_trend.slope_sessions`). |
| B4 | 2.3 | **Crisis override with stale macro data.** VIX/VIX3M = the latest value on or before the session, from CBOE or from FRED when CBOE is late. `crisis = true` (plus an alert) only when both sources are more than 2 sessions stale, the volatility pillar's threshold. |
| B5 | 2.3 | **Expiration flag:** the quarterly expiration sessions (third Friday of Mar/Jun/Sep/Dec, or the session before on a holiday; also the S&P 500 and Nasdaq-100 rebalance dates). Russell left out. Their distribution days count by default; 2.4 decides whether to exclude them. |
| B6 | 2.3 | **Starting state:** `CORRECTION` (fail closed); a replay starts ≥ 252 sessions before the first label that matters. |
| B7 | 2.3 | **Breadth universe before 1.5:** `COMMON`/`ADR` securities listed and not delisted *as of each session*, with a bar that session, passing the §5.1 filters as of that session (point in time). The thresholds live in `regime.breadth.universe_*` until 1.5 takes them over. |
| B8 | 2.3 | **Readings of §5.2:** one rally attempt across both indexes (an undercut by either restarts both day counts; each index keeps its own low); `EARLY_UPTREND` → `CORRECTION` counts distribution days since entering it; the label at session t governs t+1, and the upgrade and §6 step-up need 10 `EARLY_UPTREND` labels; a downgrade beats an upgrade and an FTD beats the `no_ftd` fallback; an `EARLY_UPTREND` LLM view when the computed regime isn't `EARLY_UPTREND` fails closed to `CORRECTION`. |
| B9 | 3.5a | **Exhaustion-gap climax signal:** the open must be ≥ 1% above the prior session's high (`climax.exhaustion_min_gap`). |
| B10 | 3.5a | **The "10-day" trail** is a 10-session **SMA**. |
| B11 | 3.5a | **Downgrade during an active 8-week hold:** to `UNDER_PRESSURE`, the hold's trail tightens one step (50 SMA → 21 EMA); to `CORRECTION`, the hold still ends. |
| B12 | 3.5a | **The late-stage sale** ranks with the climax sales (precedence slot 4); an 8-week hold doesn't suppress it; it fires once per position. |

### Need a decision or a check at 0.4

| # | Task | Topic | Detail and proposal |
|---|---|---|---|
| B13 | 2.2 / 1.5 | **Bad ticks in SIP daily bars** | Bad highs pass a plain OHLC sanity check: NVDA high 195.95 on 2024-06-10 (true ≈ 122), TSLA 290.67 / 291.31 on 2021-03-01 / 03-04. NVDA's one print created a false `FAILED` base and a false count reset. **Proposal:** ingest (1.5) gets an outlier filter (a high or low far outside both the bar's open–close range and the neighbouring bars), and the base count never trusts a single-bar extreme. |
| B14 | 2.2 / 3.5b | **`count_reliable`** | New concept: a base count is reliable only from a real IPO in the history or from a reset seen in the data. **Proposal:** 3.5b sizes `count_reliable = false` as stage 3 (half risk). Affects risk, so it needs your OK. |
| B15 | 2.2 | **Failure resets** | A decline deeper than 50% is a `FAILED` row, resets the count and makes it reliable, like an undercut. Every failure in the spot-check also undercut the prior low. Confirm. |
| B16 | 2.2 / §10 | **Retention of `bases`** | The count carries forward from `bases` rows, so they must outlive the 2-year `daily_bars` window. Add `bases` (keep forever; small) to README §10's retention list. |
| B17 | 2.2 / 1.5–1.6 | **Listing date** | The IPO anchor needs the first *public trading* date, not the exchange listing date (CELH's 2017 uplisting from OTC is not an IPO). A relisting after a > 30-session gap is a separate security with an unreliable count (§5.1; SMCI tested). Ingest must store the right date. |
| B18 | 2.3 | **Stale-day side effect** | With one stale macro day, the "VIX/VIX3M > 1.0 for ≥ 2 sessions" check can read the same observation twice. Accept, or require two distinct observation dates. |
| B19 | 2.3 | **Recomputing a regime row** keeps the stored `llm_regime`; once a view exists, Phase 4 code maintains the effective regime. Confirm. |
| B20 | 3.5a | **Readings to check** (PR #14 lists them): the entry session is session 1 and the "breakout" session for the 8-week hold; breakeven at +3R and the round-trip rule read the highest high since entry, while partials, the hold trigger, the cushion and trails read the decision price; the Kullamägi partial is checked at session N exactly; strength partial, climax ½ and late-stage ½ fire once each; "breakeven after the first partial" counts profit-taking sales only; any close below the pivot in the first 20 sessions (not a heavy-volume-reversal exit) holds, blocks tranche 2 and raises `FAILED_BREAKOUT`; a downgrade to `UNDER_PRESSURE` keeps the plan's trail tightened after a hold ends; a stop raised through the price exits that session as `HARD_STOP`; `DURING_MARKET` / `UNKNOWN` earnings timing count as before the open; with no known date the cushion runs at last report + 52 sessions; all of a session's changes fold into one plan version. |
| B21 | 3.5a / 3.8 | **Provisional menu defaults** | Kullamägi ⅓ at session 3, Minervini ⅓ at +2R, O'Neil ¼ at +20%. The baseline backtest (3.8) chooses the final values. |

### Notes for later tasks (no decision needed)

- **3.2a:** `contraction_count` from 2.2 is noisy on volatile names (fixed 3% zigzag; 30–50 in some long bases). The VCP scan applies its own contraction rules.
- **3.7:** the leadership-health pillar needs a result for every breakout in T−24…T−5 (`sessions_ago`, above trigger 5 sessions later); a missing one is an error. Until then the pillar scores 0.
- **5.x:** `position_plans.position_id` has no foreign key yet; add it with the `positions` table.
- **1.5:** the calendar sync inserts row by row (about 8 min from the PC to Neon); batch it.
