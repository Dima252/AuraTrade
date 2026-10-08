# Spike 0.3b: Alpaca orders (paper)

**Status: PART 1 of 2 done** (2026-10-08, after the close). The after-hours checks are below. The fills, sell sequences, stops through the market, OPG exits, PDT and the split/dividend checks need market hours and overnight holds. They are scripted and listed in the [run sheet](#run-sheet-part-2).
**Decision so far: GO on OTO, with one adjustment** (entry parents are GTC, so the stop leg is GTC; below). The final go / adjust waits for part 2.

Script: `spikes/alpaca_orders/` (`uv run python -m spikes.alpaca_orders <scenario>`). Every order uses a `spk-` `client_order_id`. Raw events are in `spikes/alpaca_orders/output/*.jsonl` (git-ignored). Accounts: `dev` (...JG3C) and `paper` (...CDFD), $5,000 each.

## Results so far

### OTO structure (`oto_structure`, `leg_cancel`)

| Question | Answer | Evidence |
|---|---|---|
| Does OTO accept a **stop-limit buy** parent with a stop leg? | **Yes** | parent `stop_limit`/`oto`, one leg `stop`/`sell`, leg status `held` |
| Does OTO accept a **plain limit buy** parent (§7.1 "already past the trigger")? | **Yes** | same shape |
| Can the stop leg be a **stop-limit**? | Yes (not needed) | leg `stop_limit` |
| **What TIF does the child stop get?** | **The parent's**: a `day` parent gives a `day` leg (expires 16:00 the same day), a `gtc` parent gives a `gtc` leg | `time_in_force` on the leg |
| Can the leg's TIF be changed later? | **No**: `422 time_in_force cannot be changed for advanced orders` (tested while held; re-tested after a fill in part 2) | `leg_to_gtc_while_held` |
| Can the leg's stop price be changed while held? | Yes: the replace gives a new leg id (`replaces` → old id) | `leg_raise_stop_while_held` |
| Cancelling a held leg | **Cancels the whole OTO, parent included** | `leg_cancel`: parent `canceled` 2 s later |
| Changing a parent's quantity after hours | Rejected: `cannot replace order in accepted status` (after hours, orders sit in `accepted`; during the session they are `new`) | `parent_qty_to_2` |
| Leg `client_order_id` | Set by Alpaca (a UUID), **not ours**: reconciliation must find legs through the parent's `legs`, not by our prefix | every leg |

**Consequence for §5.7 (the design anticipated this):** "OTO child legs may inherit the parent's `day` time-in-force, so after a fill the stop is converted to a standalone GTC stop if needed." It does inherit, and it **can't be converted in place**. Two ways out:

- **A (recommended): submit entries as GTC-parent OTOs.** The leg is then GTC from the start, with no conversion window. The parent must not rest past the day, so `eod_review` (or the 16:00 reconcile) cancels any unfilled entry parent. §5.8 already cancels the unfilled rest of a partly filled entry at EOD. Cancelling an unfilled parent also cancels its held leg, which is the behaviour we want. One caveat to check in part 2: a GTC parent that is partly filled at the close keeps its leg for the filled part.
- **B: day parent, then replace the leg after the fill.** Cancel the day leg and place a standalone GTC stop. That leaves a gap of milliseconds to seconds with no stop between the cancel and the new stop. The `stop_guard` covers it, but A has no gap at all.

Part 2 confirms A on a real fill (tranche 2 of `session` uses a GTC parent).

### `client_order_id` dedupe (`dedupe`)

- A duplicate id while the first order is open: **rejected**, `422 {"code":42210000,"message":"client_order_id must be unique"}`.
- A duplicate id **after the first was cancelled**: still **rejected** (`code 40010001`, same message). Ids are unique for the account's lifetime, not just among open orders.
- `GET /v2/orders:by_client_order_id` returns the original order (status `canceled`).
- **Consequence:** idempotency through `client_order_id` works (§5.7.6). A retried job that resubmits gets a 422. It must treat that as "already submitted" and look the order up by client id. A deliberate re-submission (e.g. a re-priced entry) needs a new id, so the id must encode the attempt (signal id + attempt number).

### OPG window (`opg_cutoff`)

- At 16:36 ET: `403 opg orders must be submitted after 7:00pm and before 9:28am` (both limit-on-open and market-on-open).
- **Consequence:** the 09:20 `execute_open` is inside the window. The fallback "a sale still unfilled at the close goes in as an OPG order for the next morning" (§5.7.2) **can't be placed until 19:00**: the 15:40/16:20 jobs can't do it directly. The 09:20 `execute_open` (or a 19:00+ job) must pick it up. Part 2 confirms the window's start (19:00) and end (09:28).

### Account fields (`account`)

Raw `GET /v2/account` keys (both accounts): `account_number, status, crypto_status, currency, buying_power, regt_buying_power, effective_buying_power, non_marginable_buying_power, options_buying_power, cash, accrued_fees, portfolio_value, trading_blocked, transfers_blocked, account_blocked, created_at, trade_suspended_by_user, multiplier, shorting_enabled, equity, last_equity, long_market_value, short_market_value, position_market_value, initial_margin, maintenance_margin, last_maintenance_margin, sma, balance_asof, crypto_tier, intraday_adjustments, pending_reg_taf_fees, options_approved_level, options_trading_level, admin_configurations, user_configurations`.

**The PDT fields are gone.** There is no `pattern_day_trader`, `daytrade_count` or `daytrading_buying_power`. `alpaca-py` 0.44 still models them, so they read as `None` (which is why `check_keys.py` printed "PDT flag None"). `GET /v2/account/configurations` has no `pdt_check` / `dtbp_check` either. A **$5,000** account shows `multiplier: 4` and `buying_power: 20000` (4× equity), where classic Reg T for a non-PDT account under $25k would give 2×.

This looks like the FINRA pattern-day-trader rule change that the plan says to check ("they have been under revision"). If confirmed, it **contradicts README §5.7.9 / §8** ("if PDT-restricted, entries are limited by the remaining day trades"; the $25k threshold in the plan's "What I need from you"). **Not concluded yet:** the `pdt` scenario in part 2 runs four same-day round trips on the `dev` account and shows whether a 4th day trade is rejected. Per CLAUDE.md §5 this goes to you with the results, not into the README yet.

### Account independence (`independence`)

The `dev` and `paper` keys are different and reach different accounts (...JG3C vs ...CDFD). An order placed on `dev` is not visible on `paper`. They are independent, as §12 requires.

### Other observations

- **After-hours fills on paper:** an extended-hours limit buy of DXJ filled in 0.3 s at $184.67 while the IEX quote was $174.69 × $190.21. Paper fills are optimistic, as §17 says.
- After hours, orders are `accepted`, not `new`: they can be cancelled but not replaced.

## Run sheet (part 2)

All on the `dev` account. The positions are small (≈ $80 a tranche) and every one has a GTC stop.

| When (ET) | Command | Answers |
|---|---|---|
| Fri 2026-10-09, after 09:31 | `uv run python -m spikes.alpaca_orders corp_check` | **Split:** DXJ 3:1, ex-date 10-09. Holding 1 share + a GTC sell stop at $156.97 (placed 10-08). Is the stop cancelled, left at $156.97 × 1 (it would then trigger at once on the post-split price), or adjusted to $52.32 × 3? |
| Fri, 09:40–15:00 | `uv run python -m spikes.alpaca_orders session --symbol F` (≈ 20 min) | Buy stop at/below the market; OTO fill → the leg after the fill (TIF, can it be changed?); tranche 2 with a GTC parent → merge into one full stop; share reservation; the sell sequence (success and failure); a sell stop above the market; a marketable-limit add; a stop raise. Leaves a position with a GTC stop overnight |
| Fri, market hours | `uv run python -m spikes.alpaca_orders corp_setup QDTE` | **Dividend:** QDTE pays weekly (ex-date Thursdays; its after-hours buy didn't fill on 10-08) |
| Fri, after `session` | `uv run python -m spikes.alpaca_orders pdt --symbol SOFI` | Four same-day round trips: is the 4th rejected? Is a stop leg / standalone stop on same-day shares rejected? Is a same-day exit rejected? **Uses up the day trades for 5 sessions if PDT still applies**, so run it last |
| Fri 19:00 – Mon 09:28 | `uv run python -m spikes.alpaca_orders opg_exit` | Did the GTC stop survive overnight? An OPG trim via the sell sequence (the window's start: run once just before 19:00 to see the 403, then after) |
| Mon 2026-10-12, after 09:31 | `uv run python -m spikes.alpaca_orders opg_check` | The OPG fill; restore the stop; also look at the `pdt` scenario's day leg (expired at 16:00?) |
| Thu 2026-10-15, after 09:31 | `uv run python -m spikes.alpaca_orders corp_check` | **Dividend:** QDTE's stop lowered by the dividend (FINRA 5330) or not? |
| Afterwards | `uv run python -m spikes.alpaca_orders cleanup --flatten` | Cancels `spk-` orders and closes the spike positions |

Then this note is finished with the part-2 results and the final go / adjust decision.
