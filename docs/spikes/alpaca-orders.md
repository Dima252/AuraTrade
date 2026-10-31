# Spike 0.3b: Alpaca orders (paper)

**Run:** 2026-10-08 to 10-15 on the `dev` paper account, in market hours, after hours and over a weekend, with real orders in small positions (F, DXJ, QDTE, SOFI). Every step was recorded; the raw events are kept locally, not in git.

**Result: Alpaca's OTO orders can carry the design's broker stop, with five adjustments**, all in the order layer today (README §5.6, decisions #18, #21, #22).

## What the broker does

| Question | Answer | Where it lands |
|---|---|---|
| Does an OTO accept a limit buy parent with a sell stop leg? | Yes; the leg rests `held` until the parent fills | The leveraged fund is bought as an OTO |
| **What time-in-force does the leg get?** | **The parent's.** A `day` parent gives a `day` leg, which expired at 16:00 and left the position unprotected; a `gtc` parent gives a GTC leg from the start | OTOs with a **GTC parent** (#18) |
| Can a leg's time-in-force or quantity be changed after the fill? | **No** (`422 … cannot be changed for advanced orders`); only its stop price | Before a leg must change, it is swapped for one standalone GTC stop, under the lock (#18) |
| Cancelling a held leg | Cancels the whole OTO, parent included | Unfilled parents are cancelled with their legs |
| A partly filled parent | Its leg stays `held` (inactive) until the parent fills completely | A partly filled buy counts as unprotected (#18) |
| A plain buy while a sell stop rests in the symbol | **`403 potential wash trade detected. use complex orders`**; an OTO buy is accepted | Every buy of the leveraged fund is an OTO (#18) |
| Selling while a stop holds the shares | `403 insufficient qty available` (`held_for_orders`) | The sell sequence: reduce the stop, sell, restore on failure (#18) |
| The sell sequence in market hours | Reduce (replace) accepted at once, the sale filled in 2 s; on a failed sale the stop was restored within 1 s | As designed |
| A sell stop above the market | **`422 stop price must be less than current price`**: rejected, not triggered | Price through the level → sell at once (#20) |
| A stop raise (replace) | Accepted at once; the replacement gets a new id | Raised on each E, never down |
| `client_order_id` reuse | Rejected for the account's lifetime, even after the first order was cancelled; the original is found by its client id | Ids are the idempotency key; an unanswered call is retried with the same id after a lookup (#24) |
| Leg ids | Set by Alpaca (UUIDs), not ours | Legs are found through their parent |

## On a closed market

| Test | Result | Where it lands |
|---|---|---|
| A GTC stop overnight and over a weekend | Survives, untouched | The stop rests at the broker between sessions |
| Reduce a stop, then sell OPG, on a closed market | The replace stays **`pending_replace` and reserves every share**, so the OPG sale is rejected (`403`), and restoring the stop is rejected too (`422 order chain not fully replaced`). The queued replace went through at 04:00 ET | **Nothing is sold before the open** (#21): sales left unfilled run at the 09:31 check run |
| OPG order window | Accepted only from 19:00 to 09:28 ET | Not used |
| After hours | Orders sit in `accepted` (cancellable, not replaceable); paper fills extended-hours limits optimistically | Trades go in at 15:40 |

## Corporate actions

| Test | Result | Where it lands |
|---|---|---|
| A split (DXJ 3-for-1) | The resting GTC stop was **rejected at 04:00 ET on the ex-date**; **paper never adjusted the position** (1 share at the pre-split cost, two sessions later and at the close), and the account activities have no split entry | A scaled stop placed at 09:20 on the ex-date; a `CORPORATE_ACTION` mismatch until fixed by hand; no sale at once in that fund meanwhile (#22) |
| A cash dividend (QDTE) | Paper didn't lower the resting stop (FINRA Rule 5330) | Nothing needed: the invariant re-raises a stop a broker lowers |

## Accounts

- The `dev` and `paper` keys reach independent accounts: an order on one isn't visible on the other (README §12).
- The account API has no pattern-day-trader fields; FINRA's amended Rule 4210 (effective 2026-06-04) replaced the day-trading test. The design never uses margin, so nothing depends on it.

## Not tested

What cancelling a partly filled OTO parent does to its leg (paper fills partly only at random). The order layer covers the filled shares with a standalone stop either way.
