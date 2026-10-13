# Spike 0.3b: Alpaca orders (paper)

**Status: PART 1 done** (2026-10-08, after the close); **PART 2 mostly done** (2026-10-09, market hours: `session`, the DXJ split check, `corp_setup QDTE`, `pdt`; results below). The OPG exit (Saturday) and its Monday check are done too. Remaining: the QDTE dividend check on Thursday 10-15, cleanup. The after-hours checks are below. The fills, sell sequences, stops through the market, OPG exits, PDT and the split/dividend checks need market hours and overnight holds. They are scripted and listed in the [run sheet](#run-sheet-part-2).
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

- **A (recommended): submit entries as GTC-parent OTOs.** The leg is then GTC from the start, with no conversion window. The parent must not rest past the day, so `eod_review` (or the 16:00 reconcile) cancels any unfilled entry parent. §5.7.8 already cancels the unfilled rest of a partly filled entry at EOD. Cancelling an unfilled parent also cancels its held leg, which is the behaviour we want. One caveat to check in part 2: a GTC parent that is partly filled at the close keeps its leg for the filled part.
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

This looks like the FINRA pattern-day-trader rule change that the plan says to check ("they have been under revision"). If confirmed, it **contradicts README §5.7.9 / §8** ("if PDT-restricted, entries are limited by the remaining day trades"; the $25k threshold in the plan's "What I need from you"). **Concluded in part 2:** the PDT rule is gone (see "PDT" below). Per CLAUDE.md §5 this goes to the user at 0.4, not into the README yet.

### Account independence (`independence`)

The `dev` and `paper` keys are different and reach different accounts (...JG3C vs ...CDFD). An order placed on `dev` is not visible on `paper`. They are independent, as §12 requires.

### Other observations

- **After-hours fills on paper:** an extended-hours limit buy of DXJ filled in 0.3 s at $184.67 while the IEX quote was $174.69 × $190.21. Paper fills are optimistic, as §17 says.
- After hours, orders are `accepted`, not `new`: they can be cancelled but not replaced.

## Part 2 results (2026-10-09, market hours, `dev` account)

### `session` on F (09:40–09:44 ET), 2 tranches of 6 shares, plan stop $11.34

| Test | Result | For the design |
|---|---|---|
| Buy stop-limit with the trigger **below** the market (OTO, day) | **Accepted and rested untriggered** (status `new` for 20 s, then cancelled by the script) | Confirms §7.1: past the trigger, enter with a limit buy at the limit; a stop-limit would just sit there |
| Tranche 1: OTO buy stop-limit, `day` parent | Filled in 25 s (`partially_filled` 5/6, then filled 2 s later). **The leg stayed `held` while the parent was partly filled** and went `new` (active) only when the parent filled | A partly filled entry has no active stop on its filled shares (agenda #3) |
| Change the filled leg's TIF to GTC | `422 time_in_force cannot be changed for advanced orders` | Confirms part 1: a `day` parent's leg expires at 16:00 |
| Tranche 2 with a **GTC parent** | Its leg is **GTC** from the start, and stayed so after the fill | Option A from part 1 works on a real fill |
| Grow the GTC leg to the full position (12) | **`422 qty cannot be changed for advanced orders`**, both while leg 1 rested and after leg 1 was cancelled | **New:** a leg's *quantity* is fixed too, so the sell sequence's "reduce the stop" can't act on a leg (agenda #1) |
| Merge into one stop | Cancel both legs, place a standalone GTC stop for 12: **about 1 s with no stop** on tranche 2's shares (09:43:48 → 09:43:50) | The only way to merge; do it once, under the lock |
| Sell 1 while the full stop rests | `403 insufficient qty available for order (requested: 1, available: 0)`, `held_for_orders: 12` | Confirms share reservation (§5.7.2) |
| Sell sequence, success: reduce stop 12 → 11 (replace), sell 1 (marketable limit) | Replace accepted at once; the sale filled in 2 s | ✅ |
| Sell sequence, failure: reduce to 10, a limit sell that won't fill, cancel, restore to 11 | ✅ All steps accepted; the stop was back on the full quantity within 1 s of the cancel | ✅ |
| Sell **stop above** the market | **`422 stop price must be less than current price`** | Settles §5.7.2's open question: Alpaca rejects, it doesn't trigger, so "price already through the stop → sell at once" is required |
| Marketable-limit **add** (plain buy) while the sell stop rests | **`403 potential wash trade detected. use complex orders`** (`reject_reason: opposite side market/stop order exists`). The OTO buy for tranche 2 had been accepted with a sell stop resting | **New:** every buy in a held symbol must be an OTO (agenda #2) |
| Raise the stop 11.34 → 11.63 (replace on the standalone stop) | ✅ Accepted at once | ✅ §5.7.5 |

The position kept over the weekend: **F 11 shares, standalone GTC stop $11.63** (input for the OPG test).

### Split: DXJ 3-for-1, ex-date 2026-10-09 (`corp_check`, 09:31 ET)

- The GTC sell stop placed on 10-08 (1 share at $156.97) is **`rejected`**, with `failed_at` **2026-10-09 08:00 UTC (04:00 ET)**. Alpaca removes resting stops on the ex-date before the open; it neither adjusts nor keeps them.
- The paper **position wasn't adjusted** by 09:31: still 1 share at $184.67 average cost, priced at $60.34. Alpaca's corporate-actions API lists the split (`old_rate 1`, `new_rate 3`, ex/process/payable date 10-09). Whether paper applies it later was checked on Monday: it doesn't ([Monday status](#monday-status-2026-10-12-1103-et-opg_check-corp_check)).
- **For the design (agenda #4):** README §5.7.10's "re-place a scaled stop at 09:20 on the ex-date" is needed on every split. And invariant repair must look up the corporate-actions feed first: an unadjusted position plus an unscaled plan stop ($156.97 against a $60 price) would otherwise read as "price through the stop" and be sold.

### PDT (`pdt` on SOFI, 14:30 ET)

- **Four same-day round trips (buy 1, sell 1), then two more same-day exits: all filled, none rejected**, on a $4,875 account.
- An OTO entry, a plain buy, a standalone GTC stop on same-day shares, and a same-day exit were all accepted. A sell stop above the market was rejected as before (`422`).
- The account still has no PDT fields (`daytrade_count` / `pattern_day_trader` absent), only margin fields (`multiplier` 4, `buying_power` 4× equity, `regt_buying_power` 2×).
- **Why:** the SEC approved FINRA's amendment to Rule 4210 on 2026-04-14 (Release 34-105226, FINRA Regulatory Notice 26-10). The day-trading provisions (4 day trades in 5 days, the $25,000 minimum) are replaced by an intraday margin standard for all margin accounts, effective 2026-06-04 with a phase-in to 2027-10-20.
- **For the design (agenda #10):** drop the PDT allowance (§5.7.9, §8, the "PDT status" input in §5.6). Check instead that entries never create an intraday margin deficit; the design never uses margin (exposure ≤ 100%). Confirm the live-account behaviour with Alpaca before Phase 8.
- Left open: SOFI 1 share with its OTO `day` leg, which expired at 16:00 (confirmed Sunday, below).

### Dividend setup (`corp_setup QDTE`, 09:45 ET)

1 share of QDTE bought, with a GTC stop at $25.05 (15% below). QDTE goes ex-dividend weekly (Thursdays): Thursday 10-15's `corp_check` shows whether the stop is lowered by the dividend (FINRA Rule 5330).

### OPG trim on a closed market (`opg_exit`, Saturday 2026-10-10 07:48 ET)

The F position (11 shares) kept its standalone GTC stop ($11.63, status `new`) through Friday night: a GTC stop survives the night. Then the sell sequence for a 1-share opening trim:

| Step | Result |
|---|---|
| Reduce the stop 11 → 10 (replace) | **Accepted, but not carried out:** the old stop went to **`pending_replace` and still holds all 11 shares**; the new stop (10) is `new` and takes over only when the market opens |
| Sell 1 share `opg` (market-on-open) | **`403 insufficient qty available`** (`held_for_orders: 11`, related order = the old stop). Accepted on a weekend in principle, but the shares are still reserved |
| Restore the stop to 11 | **`422 order chain not fully replaced`**: a chain with a pending replace can't be replaced again |

**Consequence for README §5.7.3 (09:20 `execute_open`: OPG exits and trims via the sell sequence):** while the market is closed, "reduce the stop, then sell" doesn't free the shares, so the OPG sale is rejected, and the restore fails too. If the 09:20 pre-market window behaves like the weekend, **OPG exits through the sell sequence can't work at all.** Nothing is unprotected meanwhile (stops only trigger in regular hours, and both stop orders stay in place), but the planned exit doesn't happen.

**Decision (user, 2026-10-10): option A.** Options considered (agenda #11):
- **A (likely simplest):** drop OPG exits. A planned morning exit runs at the **09:31 check run** as a regular-hours sell sequence (replace is processed at once in the session, as `session` showed), with a marketable limit. The cost is one minute of exposure to the open, which a stop at the plan level covers anyway.
- **B:** pre-market, **cancel** the stop (if a cancel is processed while closed, the shares are freed), place the OPG sell plus a new stop for the rest before 09:28. To test on Monday: whether a cancel is processed pre-market, and whether a new stop placed pre-market is accepted.

### Weekend status (Sunday 2026-10-11, read-only: `corp_check` and an order listing)

| Position | State | Meaning |
|---|---|---|
| **SOFI** 1 share | The OTO's `day` stop leg is **`expired`** (Friday 16:00). The position now has no stop | Answers Monday's question. A `day` parent gives a `day` leg that leaves the position unprotected at the close, which is why entries are GTC-parent OTOs (agenda #1). Spike position only; closed at cleanup |
| **DXJ** 1 share | **Still not split-adjusted** two days after the 3:1 ex-date: 1 share at $184.67 average, priced at $60.71. The stop stays `rejected` | So far paper doesn't apply splits to positions at all. If Monday shows the same, a split during the paper trial leaves a wrong position (and a false −67% on paper equity) until fixed by hand. For agenda #4, decided Thursday |
| **F** 11 shares | The original stop (11 sh, $11.63) is still **`pending_replace`**; its replacement (10 sh, `new`, created Saturday) waits for the open | Unchanged since Saturday, as expected. Monday's `opg_check` shows what the open does with it |
| **QDTE** 1 share | GTC stop $25.05 × 1, `new`, untouched | Thursday's ex-dividend check |

### Monday status (2026-10-12, 11:03 ET: `opg_check`, `corp_check`)

| Position | State | Meaning |
|---|---|---|
| **F** 11 shares | The pending replace went through at **04:00 ET** (`replaced_at` 08:00 UTC), not at the 09:30 open: the old 11-share stop is `replaced`, the 10-share stop became live then. Restoring it to 11 at 11:03 was accepted at once (`new`, $11.63 × 11) | Alpaca handles a queued replace when its pre-market session opens at 04:00 ET, the same moment it rejected the DXJ stop on the split's ex-date. Nothing was ever unprotected: an order covered the shares the whole time. Option A (#11) stands: it doesn't depend on what happens pre-market |
| **DXJ** 1 share | **Still not split-adjusted** after two sessions past the 3:1 ex-date (Fri and Mon): 1 share at $184.67, priced at $60.80 (−67%). The account activities have **no split entry** | **Paper doesn't apply splits to positions.** Paper equity shows a false loss of about $124 that stays until the position is closed. For agenda #4 |
| **SOFI** 1 share | No stop (the `day` leg expired on Friday) | As on Sunday |
| **QDTE** 1 share | GTC stop $25.05 × 1, `new` | Thursday's ex-dividend check |

## Run sheet (part 2)

All on the `dev` account. The positions are small (≈ $80 a tranche) and every one has a GTC stop.

| When (ET) | Command | Answers |
|---|---|---|
| ✅ Fri 2026-10-09, 09:31 | `uv run python -m spikes.alpaca_orders corp_check` | **Split:** DXJ 3:1, ex-date 10-09. Holding 1 share + a GTC sell stop at $156.97 (placed 10-08). Is the stop cancelled, left at $156.97 × 1 (it would then trigger at once on the post-split price), or adjusted to $52.32 × 3? |
| ✅ Fri, 09:40 | `uv run python -m spikes.alpaca_orders session --symbol F` (≈ 20 min) | Buy stop at/below the market; OTO fill → the leg after the fill (TIF, can it be changed?); tranche 2 with a GTC parent → merge into one full stop; share reservation; the sell sequence (success and failure); a sell stop above the market; a marketable-limit add; a stop raise. Leaves a position with a GTC stop overnight |
| ✅ Fri, 09:45 | `uv run python -m spikes.alpaca_orders corp_setup QDTE` | **Dividend:** QDTE pays weekly (ex-date Thursdays; its after-hours buy didn't fill on 10-08) |
| ✅ Fri, 14:30 | `uv run python -m spikes.alpaca_orders pdt --symbol SOFI` | Four same-day round trips: is the 4th rejected? Is a stop leg / standalone stop on same-day shares rejected? Is a same-day exit rejected? **Uses up the day trades for 5 sessions if PDT still applies**, so run it last |
| ✅ Sat 10-10, 07:48 (any time; the window runs to Mon 09:28) | `uv run python -m spikes.alpaca_orders opg_exit` | Did the GTC stop survive the night and weekend? An OPG trim via the sell sequence. (Moved from Friday evening: the machine was offline. Is an OPG order accepted on a weekend?) |
| Skipped (optional; moot after option A) Mon 10-12, 18:55 then 19:05 | `uv run python -m spikes.alpaca_orders opg_exit` | The window's start: a 403 just before 19:00, accepted after |
| ✅ Mon 2026-10-12, 11:03 | `uv run python -m spikes.alpaca_orders opg_check`, then `corp_check` | The pending replace went through at 04:00 ET and the stop is back to 11 shares; paper hasn't applied the DXJ split (see [Monday status](#monday-status-2026-10-12-1103-et-opg_check-corp_check)) |
| Thu 2026-10-15, after 09:31 | `uv run python -m spikes.alpaca_orders corp_check` | **Dividend:** QDTE's stop lowered by the dividend (FINRA 5330) or not? |
| Afterwards | `uv run python -m spikes.alpaca_orders cleanup --flatten` | Cancels `spk-` orders and closes the spike positions |

Then this note is finished with the part-2 results and the final go / adjust decision.
