# What's left: status at 2026-10-13

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Done so far

- **Build:** Phase 0 except 0.3b, 0.3d and 0.4; Phase 1 tasks 1.1, 1.2, 1.4; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. 782 tests pass at 99.7% branch coverage; ruff and mypy clean.
- **Sat 2026-10-10:**
  - testing review → testing gates added to the plan (1.6 real-data smoke run; 1.7 fake broker + automated chaos tests; CI coverage floor 95%, guardrails 100% from 3.5b). PR #18, merged
  - **0.4 review, session 1:** #6–#10 and B1–B21 approved (list at the top of [spikes/review-agenda.md](spikes/review-agenda.md)). #9 measured on the edge-check trades: keep $5,000, model whole shares, under 2 shares = single tranche
  - both Alpaca paper accounts verified active: `dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper`
  - the Neon `dev` database migrated to head (0004)
- **Sun 2026-10-11, read-only spike checks:** the SOFI `day` leg expired; DXJ still not split-adjusted; the F stop still `pending_replace` (as expected); CBOE's quote JSON = its history file for Friday; FRED had no weekend update. Details in [spikes/alpaca-orders.md](spikes/alpaca-orders.md) and [spikes/macro.md](spikes/macro.md).
- **Mon 2026-10-12, Monday checks (11:03 ET):** F's pending stop replace went through at 04:00 ET, when pre-market opens, and the stop is back to 11 shares. Paper still hasn't applied the DXJ split two sessions later and shows no split entry: paper doesn't apply splits (agenda #4).
- **Mon 2026-10-12, VIX watch (0.3d ✅):** CBOE's delayed-quote JSON had the final close at 16:32 ET (VIX 15.21, VIX3M 17.93); the history files followed at 19:00 and 21:33 ET with the same values; FRED hadn't published Monday by 22:17 ET. Details in [spikes/macro.md](spikes/macro.md).
- **Branch:** `phase-0/spike-wrapup` holds the Sunday and Monday notes and the 0.3d write-up. PR #19 was merged before the Sunday commits, so they're on this branch, not on `main`.

**Blocked:** every remaining build task waits for the end of 0.4, which needs Monday's and Thursday's spike checks.

## 1. The spike checks (time-bound, needs market hours)

**Times:** Tel Aviv is **7 hours ahead of New York** this week (IDT UTC+3, EDT UTC−4). Israel moves its clocks back on Sun 2026-10-25 and the US on Sun 2026-11-01, so between those dates the gap is 6 hours.

Run everything from the `AuraTrade` folder; the spike state files are in `spikes/alpaca_orders/output/` (git-ignored). All of it is on the paper account and costs nothing.

| When (Tel Aviv) | New York | Say to Claude | Commands | What it settles |
|---|---|---|---|---|
| ~~*Optional:* **Mon 2026-10-12, 15:00–16:25**~~ (skipped: window passed) | 08:00–09:25 | "take the pre-market VIX snapshot" | `uv run --group spikes python -m spikes.macro snapshot` | What CBOE's delayed-quote JSON shows before the open (CBOE computes VIX in its overnight session). Decides what the 07:30 `premarket_check` can read |
| ✅ **Mon 2026-10-12, from 16:35** (done 18:03) | after 09:31 | "run the Monday checks" | `uv run python -m spikes.alpaca_orders opg_check`, then `uv run python -m spikes.alpaca_orders corp_check` | ✅ F's replace went through at 04:00 ET; stop restored to 11. ✅ DXJ: paper doesn't apply splits. ✅ SOFI leg expired |
| ✅ **Mon 2026-10-12** (ran 18:05 → 05:17 Tue) | 11:05–22:17 | "start the VIX watch" | `uv run --group spikes python -m spikes.macro poll --until 22:15 --every 120` (in the background) | When CBOE's delayed-quote JSON has the final close, when each CBOE CSV and FRED update, and whether the JSON value equals the CSV row. ✅ JSON final 16:32 ET, equal to the CSV rows; **0.3d** done, **#5** ready to decide |
| *Optional:* **Tue 2026-10-13, from 17:00** | after 10:00 | "check FRED" | `uv run --group spikes python -m spikes.macro fred_lag` | Did FRED publish Monday's value by Tuesday morning (normal lag) or later (the holiday)? FRED is only the last fallback, so this doesn't change #5 |
| **Thu 2026-10-15, from 16:35** | after 09:31 | "run the Thursday checks" | `uv run python -m spikes.alpaca_orders corp_check`, then `uv run python -m spikes.alpaca_orders cleanup --flatten` | Was the QDTE GTC stop ($25.05) lowered by the dividend (FINRA 5330)? Then close the paper positions (F, QDTE, DXJ, SOFI) and cancel the `spk-` orders. Finishes **0.3b** and decides **#1–4** |

**Columbus Day:** Monday 2026-10-12 is a US federal holiday. The stock market and CBOE are open, but FRED may publish late. Monday's CBOE timings count as normal; if FRED is late, check its timing again on Tuesday with `uv run --group spikes python -m spikes.macro fred_lag` before calling the FRED lag.

## 2. Finish the 0.4 review (needs you, about 15 minutes, after Thursday)

- Decide **#5** (evening VIX source) with Monday's results and **#1–4** (OTO stop leg, OTO adds, partial fills, splits) with Thursday's.
- Then one PR updates the README, the decision log (#113 onward) and the plan, with every 0.4 decision. Besides the agenda rows, it includes:
  - #9's whole-share sizing at the real account size in 3.5b, 3.6 and 3.8, with a single tranche under 2 shares
  - B13's outlier filter in 1.5
  - B14's half risk for an unreliable count in 3.5b
  - #10: the stale "PDT rules apply" hint in `scripts/check_keys.py`
- Then tick **0.4**.

Say "continue the 0.4 review" when you're ready.

## 3. Things only you can do (before task 1.8, not urgent)

- **AWS account:** root MFA, an admin user via IAM Identity Center, budget alarms at $10 / $25.
- **AWS CLI** installed on this PC (Node is already there for CDK).
- **healthchecks.io** free account, for the external dead-man's switch.
- **Anthropic Console:** confirm a monthly spend limit is set (about $20 now, about $40 from 4.6).
- **Domain** for the public site: optional.

## 4. Then the build continues in plan order

Next unticked tasks: **1.3** data clients → **1.5** ingest → **1.6** backfill + real-data smoke run → **1.7** walking skeleton + fake broker and automated chaos tests → **1.8** AWS deploy (needs your OK: it creates AWS resources) → **2.4** regime backtest → **3.2a** onward.

Follow-ups already known for those tasks are under "Notes for later tasks" in the review agenda: the outlier filter and listing dates for 1.5, the batched calendar sync, the 3.7 leadership-health contract, and the `positions` foreign key.

## How to resume in a new chat

- **Thursday 16:35 Tel Aviv:** "run the Thursday checks", then "continue the 0.4 review".
