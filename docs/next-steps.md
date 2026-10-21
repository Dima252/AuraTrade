# What's left: status at 2026-10-21

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Build:** Phase 0 is complete. Phase 1 tasks 1.1–1.6; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. 998 tests pass; ruff and mypy clean.
- **Task 1.5 (ingest):** built and checked live on a scratch local database (not Neon). Merged as PR #27; its build readings are README decision **#129**. Neon storage: 2 years of bars for 2,761 tickers is about 140 MB per branch, so `dev` + `paper` together are about 55% of the free tier (taken as acceptable on 2026-10-18; say so if not).
- **0.4 review: closed** (sessions on 2026-10-10, 10-13 and 10-15). Every decision is in README §16 as **#113–128** (plus #129 from the 1.5 build), and the plan tasks carry the follow-ups. The record of what was decided and why is [spikes/review-agenda.md](spikes/review-agenda.md).
- **Spikes:** all done. 0.3b finished on Thu 2026-10-15: GO on OTO with adjustments ([spikes/alpaca-orders.md](spikes/alpaca-orders.md)). The dev paper account was flattened: no positions, no orders, $4,872.94 equity (the drop is mostly DXJ's false split loss).
- **Accounts:** both Alpaca paper accounts are active (`dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper`); the Neon `dev` database is migrated to head (0005).
- **Open PRs:** task 1.6 (branch `phase-1/backfill`), merged once CI is green. After it, `main` has everything through task 1.6 and decision #130.
- **Neon `dev`** is migrated to head (0005) and loaded (see below).

## Task 1.6: done (2026-10-21)

- **Parquet history** on this PC (`var/storage/`, git-ignored, about 1.5 GB): 13,672 securities and 16.3M bars since 2016, VIX history, 1.2M headlines, and EDGAR data for 5,305 companies (131,839 earnings events, 167,506 quarters). `scripts/backfill.py` resumes any stage.
- **Neon `dev`** holds the last 2 years: bars for about 2,750 tickers, plus 29,741 EDGAR earnings events, 43,151 fundamentals rows and the VIX history. Size 151 MB of 512. A second ingest run for the same session writes nothing (checked 2026-10-21).
- **Upcoming earnings timing:** of the 4,227 upcoming reports, 2,945 have no vendor timing. 2,396 of those now take the company's usual timing from its EDGAR history; 549 (13% of all) fall back to before the open.
- **Smoke run:** [backtests/smoke.md](backtests/smoke.md), with a hand-written Reading. No exceptions or NaN. One clear miss goes to task 2.4: the regime sat in `CORRECTION` from April to October 2017.
- **Found and fixed on 2026-10-21:** the fundamentals upsert exceeded Postgres's 65,535-parameter limit when it wrote all 45k backfill rows at once (it is now done in slices), and 14 EDGAR quarters with a filing date before their period end (mis-tagged facts) are now skipped.

## 1. Things only you can do (before task 1.8, not urgent)

- **AWS account:** root MFA, an admin user via IAM Identity Center, budget alarms at $10 / $25.
- **AWS CLI** installed on this PC (Node and the GitHub CLI are already there).
- **healthchecks.io** free account, for the external dead-man's switch.
- **Anthropic Console:** confirm a monthly spend limit is set (about $20 now, about $40 from 4.6).
- **Domain** for the public site: optional.

## 2. The build continues in plan order

Next unticked tasks: **1.7** walking skeleton + fake broker and automated chaos tests → **1.8** AWS deploy (needs your OK: it creates AWS resources) → **2.4** regime backtest → **3.2a** onward.

The 0.4 follow-ups are in those tasks now: the OTO/leg/partial-fill order core and the fake broker's 0.3b behaviours (1.7), whole-share sizing (3.5b, 3.6, 3.8).

## How to resume in a new chat

- "continue" starts task 1.7. Until `post_market` exists (1.7), keep Neon `dev` current by hand: `uv run python -m auratrade.jobs.ingest --direct --session <latest session>`. Note: Alpaca's TEVA history starts on 2026-09-14 (a vendor gap, not ingest).
