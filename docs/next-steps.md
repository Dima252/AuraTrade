# What's left: status at 2026-10-18

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Build:** Phase 0 is complete. Phase 1 tasks 1.1–1.5; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. 997 tests pass on `phase-1/backfill`; ruff and mypy clean.
- **Task 1.5 (ingest):** built and checked live on a scratch local database (not Neon). Merged as PR #27; its build readings are README decision **#129**. Neon storage: 2 years of bars for 2,761 tickers is about 140 MB per branch, so `dev` + `paper` together are about 55% of the free tier (taken as acceptable on 2026-10-18; say so if not).
- **0.4 review: closed** (sessions on 2026-10-10, 10-13 and 10-15). Every decision is in README §16 as **#113–128** (plus #129 from the 1.5 build), and the plan tasks carry the follow-ups. The record of what was decided and why is [spikes/review-agenda.md](spikes/review-agenda.md).
- **Spikes:** all done. 0.3b finished on Thu 2026-10-15: GO on OTO with adjustments ([spikes/alpaca-orders.md](spikes/alpaca-orders.md)). The dev paper account was flattened: no positions, no orders, $4,872.94 equity (the drop is mostly DXJ's false split loss).
- **Accounts:** both Alpaca paper accounts are active (`dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper`); the Neon `dev` database is migrated to head (0004).
- **Open PRs:** none. `main` has everything through task 1.5 and decision #129. **Task 1.6 is in progress on branch `phase-1/backfill`** (pushed, no PR yet): see the section below.
- **Neon `dev`** is now migrated to head (0005).

## Task 1.6 in progress (paused 2026-10-18 by the user)

Code, tests and the first two smoke runs are done (branch `phase-1/backfill`). Every stage resumes where it left off (progress lives in `var/backfill/` and `var/storage/`, both on this PC, git-ignored).

Done:
- **Parquet history:** discovery (12,463 symbols), bars (13,672 securities, 16.3M bars), build, VIX history, **news** (130 months, 1.2M headlines), **EDGAR** (5,305 companies, 131,839 earnings events, 167,520 quarters, 563 listing dates cleared).
- **Your approval** of the findings (2026-10-18): README §5.1/§10/§13 and decision **#130** are written and committed on the branch; the 2017 CORRECTION spell is noted under task 2.4.
- **Two fixes found on the way** (committed, tested): EDGAR company facts without a `cik` field (3 companies failed), and Neon ending an ingest transaction idle for 5 minutes (new `ingest.transaction_idle_minutes: 30` in `risk.yaml`).
- **Neon `dev` load, partly:** the bars are committed (1.33M rows, 2,750 tickers, 136 MB). The run was stopped in its company-data step, so CIKs, SICs, earnings and fundamentals are not in Neon yet.

To finish, from the AuraTrade folder, one at a time:

1. `uv run python -m auratrade.jobs.ingest --direct --session 2026-10-16` (re-run: the bars are a no-op, then company data, earnings, fundamentals, retention; about 20-30 minutes. On a later day, use the latest session instead)
2. `uv run python scripts/backfill.py postgres`
3. `uv run python scripts/smoke_real_data.py`, then write the `## Reading` section of `docs/backtests/smoke.md`. Check the Apr-Jun 2017 CORRECTION spell; note that headline timing covers 71% of earnings events in 2016, rising to 92% in 2026 (the rest are timed by the 8-K alone)
4. Tick 1.6 in the tracker, update this page, push, PR, merge when CI is green

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
