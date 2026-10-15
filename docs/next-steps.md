# What's left: status at 2026-10-15

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Build:** Phase 0 except 0.4; Phase 1 tasks 1.1, 1.2, 1.4; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. 782 tests pass at 99.7% branch coverage; ruff and mypy clean. Everything is merged to `main` (latest: PR #20, `phase-0/spike-wrapup`).
- **0.4 review:** #5–#11 and B1–B21 are approved (session 1 on 2026-10-10, session 2 on 2026-10-13: #5, the evening VIX source). The list and the exact wording are at the top of [spikes/review-agenda.md](spikes/review-agenda.md). None of it is in the README yet.
- **Spikes:** all done. 0.3b finished on Thu 2026-10-15: GO on OTO with the adjustments in agenda #1–4 and #11 ([spikes/alpaca-orders.md](spikes/alpaca-orders.md)). Paper applies neither splits (DXJ) nor the FINRA 5330 dividend reduction to stops (QDTE's stayed at $25.05 on its ex-date). The dev paper account was flattened: no positions, no orders, $4,872.94 equity (the drop is mostly DXJ's false split loss).
- **Accounts:** both Alpaca paper accounts are active (`dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper`); the Neon `dev` database is migrated to head (0004).

**Blocked:** every remaining build task waits for the end of 0.4, which needs only decisions #1–4.

## 1. Finish the 0.4 review (needs you, about 10 minutes)

- Decide **#1–4** (OTO stop leg, OTO adds, partial fills, splits).
- Then one PR updates the README, the decision log (#113 onward) and the plan, with every 0.4 decision. Besides the agenda rows, it includes:
  - #9's whole-share sizing at the real account size in 3.5b, 3.6 and 3.8, with a single tranche under 2 shares
  - B13's outlier filter in 1.5
  - B14's half risk for an unreliable count in 3.5b
  - #10: the stale "PDT rules apply" hint in `scripts/check_keys.py`, and the PDT notes in the plan's "What I need from you"
- Then tick **0.4**.

Say "continue the 0.4 review" when you're ready.

## 2. Things only you can do (before task 1.8, not urgent)

- **AWS account:** root MFA, an admin user via IAM Identity Center, budget alarms at $10 / $25.
- **AWS CLI** installed on this PC (Node and the GitHub CLI are already there).
- **healthchecks.io** free account, for the external dead-man's switch.
- **Anthropic Console:** confirm a monthly spend limit is set (about $20 now, about $40 from 4.6).
- **Domain** for the public site: optional.

## 3. Then the build continues in plan order

Next unticked tasks: **1.3** data clients → **1.5** ingest → **1.6** backfill + real-data smoke run → **1.7** walking skeleton + fake broker and automated chaos tests → **1.8** AWS deploy (needs your OK: it creates AWS resources) → **2.4** regime backtest → **3.2a** onward.

Follow-ups already known for those tasks are under "Notes for later tasks" in the review agenda: the outlier filter and listing dates for 1.5, the batched calendar sync, the 3.7 leadership-health contract, and the `positions` foreign key.

## How to resume in a new chat

- "continue the 0.4 review".
