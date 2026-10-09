# Task 0.4: spike review agenda

The points to decide at the 0.4 review. Each spike note has the evidence; this page lists the decisions in one place. Nothing here changes the README until the user approves it (CLAUDE.md §5).

Status: being collected while the last spikes run (2026-10-09 to 2026-10-15).

## Proposed design changes (need approval)

| # | Topic | Finding | Proposal | Evidence |
|---|---|---|---|---|
| 1 | OTO stop leg | The leg's time-in-force **and quantity** can't be changed after the fill (`422`) | Entries as GTC-parent OTOs. Before the first reduce or merge, swap the leg for a standalone GTC stop (cancel, then place: about 1 s with no stop, under the lock, covered by `stop_guard`); from then on every change is an atomic replace | [alpaca-orders.md](alpaca-orders.md) |
| 2 | Adds while a stop rests | A plain buy is rejected as a "potential wash trade" while a sell stop rests; an OTO buy is accepted | Every buy in a held symbol (tranche 2, adds) goes in as an OTO with its own stop leg | alpaca-orders.md |
| 3 | Partial fills | The stop leg stays `held` (inactive) while the parent is only partly filled | A partly filled entry counts as unprotected: cancel the rest and place a standalone stop. Still open: what cancelling a partly filled parent does to its leg | alpaca-orders.md |
| 4 | Splits | On the ex-date Alpaca **rejects resting stops at 04:00 ET**; the paper position wasn't split-adjusted by 09:31 | Keep §5.7.10 (re-place a scaled stop at 09:20). Add: before any invariant repair, check the corporate-actions feed for a split that day, so an unadjusted position isn't read as "price through the stop" and sold | alpaca-orders.md |
| 5 | Evening VIX | CBOE's history files updated at 18:31 / 21:11 ET, after the 17:25 regime job; FRED lags a full session (timing watch Mon 10-12) | The evening VIX/VIX3M come from CBOE's delayed-quote JSON (final about 16:30); the history file and FRED are fallbacks | [macro.md](macro.md) |
| 6 | Daily-bar volume | Volume of bars fetched at 16:58 grew later (187 of 195 bars; up to +62%) | The evening ingest re-fetches session T−1's bar | [alpaca-data.md](alpaca-data.md) |
| 7 | Earnings timing | Finnhub's free calendar has no past dates and leaves before/after-market timing blank for 61% of upcoming reports; 8-K Item 2.02 timing matches news in 96% of events | History from 8-K Item 2.02; timing = the earlier of the 8-K acceptance and the first earnings headline; upcoming timing falls back to the company's own 8-K habit | [fundamentals.md](fundamentals.md) |
| 7b | Decision price | The latest IEX **trade** is a median 4 min old (p90 66 min) outside the 500 most liquid names; IEX **quotes** stay fresh (median 6 s) | Price = last IEX trade if ≤ 2 min old, else the IEX quote midpoint if ≤ 1 min old with a sane spread, else no price (skip entries; a soft exit waits, the hard stop rests) | alpaca-data.md |
| 8 | Claude cost | Analyst requests are about 3,400 input tokens (README assumed 8,000); a Portfolio Manager call took 39 s and $0.10 | Recalibrate README §9 and set Phase 4's daily cost limit from the measurement | [claude-cost.md](claude-cost.md) |

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
| Out-of-order task | 2.1 Indicators built before Phase 1 finished, with approval | tracker 2.1 |
