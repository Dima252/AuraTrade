# What's left: status at 2026-10-10 (evening)

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Done:** Phase 0 except 0.3b, 0.3d and 0.4; Phase 1 tasks 1.1, 1.2, 1.4; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. PR #18 (testing gates in the plan, CI coverage floor 95%) is merged.
- **0.4 review, session 1 done** (2026-10-10, branch `phase-0/review-0-4`, pushed, no PR yet): #6–#10 and B1–B21 approved. The list is at the top of [spikes/review-agenda.md](spikes/review-agenda.md).
- **Checked on 2026-10-10:**
  - both Alpaca paper accounts are active: `dev` ...JG3C in `.env` and `paper` ...CDFD in `.env.paper`
  - the Neon `dev` database is migrated to head (0004)
  - 782 tests pass at 99.7% branch coverage
- **Blocked:** every remaining build task waits for the end of 0.4. That needs Monday's and Thursday's spike checks.

## 1. The spike checks (time-bound, needs market hours)

**Times:** Tel Aviv is **7 hours ahead of New York** this week (IDT UTC+3, EDT UTC−4). Israel moves its clocks back on Sun 2026-10-25 and the US on Sun 2026-11-01, so between those dates the gap is 6 hours.

Run everything from the `AuraTrade` folder; the spike state files are in `spikes/alpaca_orders/output/` (git-ignored). All of it is on the paper account and costs nothing.

| When (Tel Aviv) | New York | Say to Claude | Commands | What it settles |
|---|---|---|---|---|
| **Mon 2026-10-12, from 16:35** | after 09:31 | "run the Monday checks" | `uv run python -m spikes.alpaca_orders opg_check`, then `uv run python -m spikes.alpaca_orders corp_check` | Did Saturday's `pending_replace` on F resolve at the open to the 10-share stop (then restore it to 11)? Did paper split-adjust DXJ (1 → 3 shares)? Did the SOFI OTO `day` leg expire? |
| **Mon 2026-10-12, start by 22:50**, runs until **05:15 Tue** | 15:50–22:15 | "start the VIX watch" | `uv run --group spikes python -m spikes.macro poll --until 22:15 --every 120` (in the background) | When CBOE's delayed-quote JSON has the final close, when each CBOE CSV and FRED update, and whether the JSON value equals the CSV row. Finishes **0.3d** and decides **#5** |
| **Thu 2026-10-15, from 16:35** | after 09:31 | "run the Thursday checks" | `uv run python -m spikes.alpaca_orders corp_check`, then `uv run python -m spikes.alpaca_orders cleanup --flatten` | Was the QDTE GTC stop ($25.05) lowered by the dividend (FINRA 5330)? Then close the paper positions (F, QDTE, DXJ, SOFI) and cancel the `spk-` orders. Finishes **0.3b** and decides **#1–4** |

**Monday night checklist:**
- Keep the PC **plugged in**: on mains power it never sleeps, but on battery it sleeps after 10 minutes.
- Pause Windows Update for the night.
- Leave the Claude chat open.

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

- **Monday 16:35 Tel Aviv:** "run the Monday checks".
- **Monday by 22:50 Tel Aviv:** "start the VIX watch".
- **Thursday 16:35 Tel Aviv:** "run the Thursday checks", then "continue the 0.4 review".
