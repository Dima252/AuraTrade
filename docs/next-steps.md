# What's left: status at 2026-10-17

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Build:** Phase 0 is complete. Phase 1 tasks 1.1–1.5; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. 968 tests pass; ruff and mypy clean.
- **Task 1.5 (ingest):** built and checked live on a scratch local database (not Neon). Merged as PR #27; its build readings are README decision **#129**. Neon storage: 2 years of bars for 2,761 tickers is about 140 MB per branch, so `dev` + `paper` together are about 55% of the free tier (open question for you: acceptable, or trim before 1.8).
- **0.4 review: closed** (sessions on 2026-10-10, 10-13 and 10-15). Every decision is in README §16 as **#113–128** (plus #129 from the 1.5 build), and the plan tasks carry the follow-ups. The record of what was decided and why is [spikes/review-agenda.md](spikes/review-agenda.md).
- **Spikes:** all done. 0.3b finished on Thu 2026-10-15: GO on OTO with adjustments ([spikes/alpaca-orders.md](spikes/alpaca-orders.md)). The dev paper account was flattened: no positions, no orders, $4,872.94 equity (the drop is mostly DXJ's false split loss).
- **Accounts:** both Alpaca paper accounts are active (`dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper`); the Neon `dev` database is migrated to head (0004).
- **Open PRs:** none. `main` has everything through task 1.5 and decision #129. **Task 1.6 is in progress on branch `phase-1/backfill`** (pushed, no PR yet): see the section below.
- **Neon `dev`** is now migrated to head (0005).

## Task 1.6 in progress (paused 2026-10-17 by the user)

Code, tests and the first two smoke runs are done (branch `phase-1/backfill`). The long data run stopped part way; every stage resumes where it left off (progress lives in `var/backfill/` and `var/storage/`, both on this PC, git-ignored).

Done: discovery (12,463 symbols), bars (13,672 securities, 16.3M bars), build, VIX history, EDGAR downloads (5,302 companies cached). **Paused: the news history, 73 of 130 months saved (through 2022-01), about 1.5-2 hours left.**

To finish, from the AuraTrade folder, one at a time (the first and third use Alpaca; never run two Alpaca steps at once):

1. `uv run python scripts/backfill.py news` (resumes at 2022-02)
2. `uv run python scripts/backfill.py edgar` (minutes: from the cache)
3. `uv run python -m auratrade.jobs.ingest --direct --session 2026-10-16` (the 2-year bars into Neon `dev`; use the latest session)
4. `uv run python scripts/backfill.py postgres`
5. `uv run python scripts/smoke_real_data.py`, then write the `## Reading` section of `docs/backtests/smoke.md`
6. Docs, tick 1.6, PR. Findings needing your approval (README §5.1/§10/§13 and a decision #130): renamed-ticker duplicates dropped (860), a 4x one-day move starts a new security, CIKs only for securities still trading (SEC's map is today's), retention merges into the backfill's session files, the `history/` package. For 2.4: the regime sat in CORRECTION Apr-Jun 2017 (no follow-through day in a calm market).

## 1. Things only you can do (before task 1.8, not urgent)

- **AWS account:** root MFA, an admin user via IAM Identity Center, budget alarms at $10 / $25.
- **AWS CLI** installed on this PC (Node and the GitHub CLI are already there).
- **healthchecks.io** free account, for the external dead-man's switch.
- **Anthropic Console:** confirm a monthly spend limit is set (about $20 now, about $40 from 4.6).
- **Domain** for the public site: optional.

## 2. The build continues in plan order

Next unticked tasks: **1.6** backfill + real-data smoke run → **1.7** walking skeleton + fake broker and automated chaos tests → **1.8** AWS deploy (needs your OK: it creates AWS resources) → **2.4** regime backtest → **3.2a** onward.

The 0.4 follow-ups are in those tasks now: 8-K + headline earnings timing (1.6), the OTO/leg/partial-fill order core and the fake broker's 0.3b behaviours (1.7), whole-share sizing (3.5b, 3.6, 3.8).

## How to resume in a new chat

- "continue task 1.6" (the steps above). Note for 1.6: Alpaca's TEVA history starts on 2026-09-14 (a vendor gap, not ingest), and the live run's scratch database showed 74% of upcoming earnings timings inferred, because EDGAR history arrives with the backfill.
