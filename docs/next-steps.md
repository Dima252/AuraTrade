# What's left: status at 2026-10-10

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Done:** Phase 0 except 0.3b, 0.3d and 0.4; Phase 1 tasks 1.1, 1.2, 1.4; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. Everything is merged into `main` (last: PR #14), CI is green, and no other branches or worktrees exist.
- **Blocked:** every remaining build task waits for the **0.4 review**, because its decisions change the order layer, ingest, macro data and sizing.
- **The 0.4 review waits for** two time-bound spike checks next week (below).

## 1. Finish Phase 0 (time-bound, needs market hours)

Run from the `AuraTrade` folder (the spike state files are in `spikes/alpaca_orders/output/`, git-ignored). Times are New York (ET); your local time (UTC+3) is 7 hours later.

| When | Command | What it settles | Then |
|---|---|---|---|
| **Mon 2026-10-12, after 09:31 ET** (16:31 local) | `uv run python -m spikes.alpaca_orders opg_check` then `uv run python -m spikes.alpaca_orders corp_check` | Did Saturday's `pending_replace` on F resolve at the open to the 10-share stop (then restore it to 11)? Did paper split-adjust DXJ (1 → 3 shares)? Did the SOFI OTO `day` leg expire? | Notes in `docs/spikes/alpaca-orders.md` |
| **Mon 2026-10-12, 15:55–22:15 ET** (22:55–05:15 local; the PC must stay on) | `uv run --group spikes python -m spikes.macro poll --until 22:15 --every 120` (in the background) | When CBOE's delayed-quote JSON has the final close, when each CBOE CSV and FRED update, and whether the JSON value equals the CSV row | Finish `docs/spikes/macro.md`, tick **0.3d** |
| **Thu 2026-10-15, after 09:31 ET** (16:31 local) | `uv run python -m spikes.alpaca_orders corp_check` then `uv run python -m spikes.alpaca_orders cleanup --flatten` | Was the QDTE GTC stop ($25.05) lowered by the dividend (FINRA 5330)? Then close the paper positions (F, QDTE, DXJ, SOFI) and cancel the `spk-` orders | Final go/adjust in `docs/spikes/alpaca-orders.md`, tick **0.3b** |

All of this is on the paper account (fake money) and costs nothing.

## 2. The 0.4 review (needs you)

Walk through [spikes/review-agenda.md](spikes/review-agenda.md) and decide each open row. The settled rows can be done before Thursday.

- **Open spike decisions:** order changes (#1–4), evening VIX source (#5), T−1 bar re-fetch (#6), earnings timing (#7), decision price (#7b), Claude cost (#8), the $5,000 small-account problem (#9), the PDT rule being gone (#10).
- **Open build decisions:** bad ticks in SIP bars (B13), `count_reliable` as half risk (B14), failure resets (B15), `bases` retention (B16), listing dates (B17), the stale-day side effect (B18), regime recompute (B19), the 3.5a readings (B20).
- **Already decided, only to be recorded:** the "Already decided" table and B1–B12.

After the review, one PR updates the README, the decision log (#113 onward) and the plan, then tick **0.4**.

## 3. Then the build continues in plan order

Next unticked tasks: **1.3** data clients → **1.5** ingest → **1.6** backfill → **1.7** walking skeleton → **1.8** AWS deploy (needs your OK: it creates AWS resources) → **2.4** regime backtest → **3.2a** onward.

Follow-ups already known for those tasks are under "Notes for later tasks" in the review agenda (outlier filter and listing dates for 1.5, batched calendar sync, the 3.7 leadership-health contract, the `positions` foreign key).

## How to resume in a new chat

- Monday: "run the Monday checks", and in the afternoon "start the VIX watch".
- Thursday: "run the Thursday checks".
- Any time: "start the 0.4 review".
