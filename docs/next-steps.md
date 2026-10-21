# What's left: status at 2026-10-21 (task 1.7 paused mid-way)

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Build:** Phase 0 is complete. Phase 1 tasks 1.1–1.6; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. 998 tests pass; ruff and mypy clean.
- **Task 1.5 (ingest):** built and checked live on a scratch local database (not Neon). Merged as PR #27; its build readings are README decision **#129**. Neon storage: 2 years of bars for 2,761 tickers is about 140 MB per branch, so `dev` + `paper` together are about 55% of the free tier (taken as acceptable on 2026-10-18; say so if not).
- **0.4 review: closed** (sessions on 2026-10-10, 10-13 and 10-15). Every decision is in README §16 as **#113–128** (plus #129 from the 1.5 build), and the plan tasks carry the follow-ups. The record of what was decided and why is [spikes/review-agenda.md](spikes/review-agenda.md).
- **Spikes:** all done. 0.3b finished on Thu 2026-10-15: GO on OTO with adjustments ([spikes/alpaca-orders.md](spikes/alpaca-orders.md)). The dev paper account was flattened: no positions, no orders, $4,872.94 equity (the drop is mostly DXJ's false split loss).
- **Accounts:** both Alpaca paper accounts are active (`dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper`); the Neon `dev` database is migrated to head (0005).
- **Open branch:** `phase-1/walking-skeleton` (task 1.7, pushed, no PR yet). `main` has everything through task 1.6 and decision #130.
- **Neon `dev`** is migrated to head (0005) and loaded (see below).

## Task 1.6: done (2026-10-21)

- **Parquet history** on this PC (`var/storage/`, git-ignored, about 1.5 GB): 13,672 securities and 16.3M bars since 2016, VIX history, 1.2M headlines, and EDGAR data for 5,305 companies (131,839 earnings events, 167,506 quarters). `scripts/backfill.py` resumes any stage.
- **Neon `dev`** holds the last 2 years: bars for about 2,750 tickers, plus 29,741 EDGAR earnings events, 43,151 fundamentals rows and the VIX history. Size 151 MB of 512. A second ingest run for the same session writes nothing (checked 2026-10-21).
- **Upcoming earnings timing:** of the 4,227 upcoming reports, 2,945 have no vendor timing. 2,396 of those now take the company's usual timing from its EDGAR history; 549 (13% of all) fall back to before the open.
- **Smoke run:** [backtests/smoke.md](backtests/smoke.md), with a hand-written Reading. No exceptions or NaN. One clear miss goes to task 2.4: the regime sat in `CORRECTION` from April to October 2017.
- **Found and fixed on 2026-10-21:** the fundamentals upsert exceeded Postgres's 65,535-parameter limit when it wrote all 45k backfill rows at once (it is now done in slices), and 14 EDGAR quarters with a filing date before their period end (mis-tagged facts) are now skipped.

## Task 1.7: in progress (paused 2026-10-21, 07:50 ET)

Branch `phase-1/walking-skeleton`, commit `6d0904b` plus this handoff. Paused by the user (credits).

**Built and tested (1,173 tests pass locally on a throwaway Postgres; ruff and mypy clean):**
- `execution/`: `broker.py` (interface + errors), `alpaca_broker.py` (paper adapter + IEX decision price), `invariant.py` (the stop invariant, pure), `orders.py` (the order desk: protect/repair, the sell sequence with the leg swap, sell-at-once escalation limit → re-price → market, retries that reuse the client_order_id), `lock.py` (advisory lock on a direct connection, `idle_session_timeout` backstop), `reconcile.py` (fills → positions + plan v1, closes → journal rows, unknown positions → mismatch, splits → scaled plan + mismatch), `store.py`, `signals.py` + `skeleton.py` (scan-lite, sizing on the limit, entry decision), `runtime.py` (order section, invariant enforcement, owed sales, entry blocks, read-only broker in dry_run), `ids.py`, `prices.py`.
- `sim/fake_broker.py`: the 0.3b behaviours + fault injection (500, dropped answer, process killed, deleted order).
- Jobs: `scan` (skeleton), `execute_open`, `execute_entries`, `eod_review`, `post_market`, `stop_guard` (quiet: records a run only when it acts or fails), placeholders `simulate` and `analyst_submit` (keep the evening chain whole). Runner: `GuardPolicy.MARKET_HOURS` and `quiet` jobs.
- `journal/report.py` (snapshot + Telegram daily report), `journal/backup.py` (nightly Parquet export).
- Migration **0006** (signals, positions, orders, trade_journal, account_snapshots; FKs from position_plans/events). `auratrade` CLI: `kill on|off|status`, `mismatches`, `resolve`.
- Config: `execution:` and `skeleton:` sections in `risk.yaml`.
- Tests: `tests/execution/` (fake broker, invariant, desk, jobs, and **all 7 automated chaos scenarios** of the plan, 17 tests).
- Bug found by the chaos tests and fixed: a retried `execute_entries` skipped its own already-submitted entry; it now adopts it by client_order_id.

**Live state:**
- **Neon `dev` is migrated to 0006** (done 2026-10-21). It has no `market_regime` rows yet and the kill switch is ON (entries disabled).
- Alpaca `dev` paper: flat, $4,872.92. The adapter was checked read-only against it (account, clock, positions, orders, lookup by client id).
- A throwaway test Postgres container `auratrade-test-pg` on port 5434 may still be running: `docker stop auratrade-test-pg`. Tests: `TEST_DATABASE_URL=postgresql+psycopg://auratrade:auratrade@localhost:5434/auratrade uv run pytest`.

**Left to do, in order:**
1. Unit tests still missing (for the 95% coverage floor): the Alpaca adapter's mapping and errors (`to_broker_order`, `flatten`, `to_request`, `map_error`, with alpaca-py models built from JSON), `lock.py` (`direct_conninfo`, wait/alert/timeout, `holder`), `prices.decision_price`, `ids.parse`, `skeleton.entry_levels`/`size`, `signals.scan` on synthetic bars + the `scan` job, the runner's quiet mode (no record when OK, a record when `acted` or failed), the CLI, `journal/backup` and `report`. Then `uv run pytest --cov` and check coverage.
2. Seed the regime on Neon dev (was about to run): `uv run python -m auratrade.jobs.regime --from 2026-09-01 --session <last session before the chain run>`.
3. Run the evening chain once to produce signals: `TRADING_MODE=paper uv run python -m auratrade.jobs.post_market --session <session> --chain` (post_market → ingest → simulate → regime → scan → analyst_submit).
4. `uv run auratrade kill off` on dev (entries on; paper money on the dev account).
5. **Accept: 3 sessions on Alpaca paper `dev`**, jobs with `TRADING_MODE=paper` at 09:20 / 09:31 `execute_open`, 09:36 `execute_entries`, `stop_guard` every 5 min, 15:40 `eod_review`, 16:20 `post_market --chain`. Idea: a small local scheduler script run in the background for the day. Check by hand: a forced concurrent run waits on the lock; a stop cancelled by hand right after a reduce is restored by `stop_guard` within 5 minutes; no duplicates; every job run or skipped correctly.
6. Readings to put to the user for approval (then README §5.7/§10/§13/§14 + decision #131): provisional `execution.decision_price.max_spread` 2% ("sane spread" of #120), `marketable_limit_slippage` 0.5%, `lock_wait_max_seconds` 480, `lock_idle_timeout_minutes` 10; skeleton trades labelled `VCP_BREAKOUT` with `source = SKELETON` (excluded from your numbers); no regime gating in the skeleton; placeholder `simulate`/`analyst_submit` jobs; `stop_guard` as a quiet job with the broker clock as its calendar guard; a split handled by scaling the plan + a mismatch; the `auratrade mismatches/resolve` commands added now.
7. README updates (layout §13: `cli.py`, `execution/` files; §7 commands in CLAUDE.md), tick 1.7, PR, merge on green CI.

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

- "continue" resumes task 1.7 at "Left to do" step 1 above (branch `phase-1/walking-skeleton`). Until `post_market` exists (1.7), keep Neon `dev` current by hand: `uv run python -m auratrade.jobs.ingest --direct --session <latest session>`. Note: Alpaca's TEVA history starts on 2026-09-14 (a vendor gap, not ingest).
