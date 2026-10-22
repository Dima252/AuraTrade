# What's left: status at 2026-10-22 18:45 ET (task 1.8 ready to deploy; waiting on your budgets and your OK)

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Build:** Phase 0 is complete. Phase 1 tasks 1.1–1.6; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. Task 1.7 is **built and merged** (PR #30); only its live acceptance is left, now on AWS (below). On the 1.8 branch 1,258 tests pass, coverage 97%, ruff and mypy clean.
- **Decisions:** README §16 runs to **#133** (#132: 1.7's live acceptance moved to AWS; #133: the 1.8 build readings, approved 2026-10-22). The design is frozen (#105).
- **Accounts:** both Alpaca paper accounts are active and flat (no positions, no open orders, checked 2026-10-22 18:40 ET): `dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper` ($5,000). Neon `dev` (endpoint `ep-autumn-sea-…`) is migrated to head (**0006**), holds the last 2 years of data (about 151 MB of 512), its regime rows are seeded from 2026-09-01, and its kill switch is **off**. Neon `paper` (endpoint `ep-summer-math-…`) is a full copy of `dev` made 2026-10-22, also at 0006.
- **Env files (2026-10-22):** `.env` and `.env.paper` have the same keys in the layout of `.env.example` (including `ENVIRONMENT`); every key in both passes `scripts/check_keys.py` (`--env-file .env.paper` for `paper`). Any local command runs against `paper` with `uv run --env-file .env.paper ...` (checked: it resolves to the `paper` database).
- **AWS:** account 391732005061; CLI profile `auratrade` (us-east-1) is an IAM user with `AdministratorAccess`, signed in with `aws login` (not Identity Center; equivalent for this). **Nothing has been created in AWS yet**: no budgets, no `cdk bootstrap`, no parameters.
- **healthchecks.io:** check `auratrade-watchdog`, cron `*/15 7-21 * * 1-5`, `America/New_York`, grace 30 minutes; its ping URL is in `.env.paper`.
- **Known data notes:** Alpaca's TEVA history starts on 2026-09-14 (a vendor gap, not ingest). FRED answered with invalid JSON on the evening of 2026-10-21; on 2026-10-22 it answers again (`VIXCLS` 2026-10-21 = 16.68).

## Change of plan (2026-10-21, approved by the user)

The PC is on a phone hotspot, so it can't be relied on to stay connected for the sessions. **The rest of 1.7's live acceptance (3 sessions + the stop hand check) moves to AWS**: it runs as part of task 1.8, on the separate `paper` Alpaca account. It is recorded as decision **#132** and in the plan (1.7 and 1.8 acceptance).

**The local scheduler is stalled.** It was restarted 10-22 at 11:17 ET from the `AuraTrade-live` worktree (a separate copy of `main` at `C:\Users\dimat\Documents\GitHub\AuraTrade-live`, log `AuraTrade-live\var\local-session.log`); its last run was `stop_guard` at 14:30 ET, so it missed the rest of the day's stop checks, the 15:40 `eod_review` and the evening chain on `dev`. Nothing was exposed: the `dev` account was flat. Its process (`python.exe ... scripts/local_session.py --until 2026-10-26`) was still alive at 18:40 ET; stop it from Task Manager or with `Stop-Process` (the session couldn't). There's no need to restart it: AWS takes over. Once AWS runs, `git worktree remove ..\AuraTrade-live`.

## Task 1.8: in progress (branch `phase-1/aws-deploy`, draft PR #31)

PR #31 stays a **draft** until the AWS acceptance passes; then tick 1.7 and 1.8 in the tracker, mark it ready and merge.

**Done on the branch (tested; ruff and mypy clean):**
- `jobs/schedule.py`: the whole weekday schedule as `Slot`s, the single source for the AWS stack, the watchdog and `scripts/local_session.py`; the evening chain's fallbacks fire every 10 minutes up to their deadline.
- `jobs/watchdog.py`: missing heartbeats, stuck RUNNING rows, disabled schedules; each finding alerted once per session; pings `HEALTHCHECKS_PING_URL`.
- `eod_review` at 12:40 and 15:40, running only within 30 minutes of that day's close (half days work).
- `parameters.py`: secrets from SSM at function start. `scripts/put_parameters.py`: plans by default, `--apply` writes.
- `scripts/reset_trading_state.py`: clears the trading tables a Neon branch copied from `dev` carries, resets the flags (kill switch on); plans by default, refuses the `dev` database and localhost.
- README §3, §5.13, §12, §13, §14 and decisions #132, #133 written; CLAUDE.md commands and decision count updated.
- `Dockerfile` + `.dockerignore` (allow-list; checked: the staged context holds only `Dockerfile`, `pyproject.toml`, `uv.lock`, `config/`, `src/`).
- `infra/stacks/trading.py` (`TradingStack`), wired into `infra/app.py` as `AuraTradePaper`; `npx aws-cdk synth` passes for both stacks.
- Tests: schedule, watchdog (against the test Postgres), the `eod_review` window, SSM loading, `put_parameters`, the stack synthesized offline (handlers, `dry_run` default, `live` refused, retries, grants, schedules = slots).

**Checked 2026-10-22 (plans only, nothing written):**
- `put_parameters --env paper --env-file .env.paper`: all 10 keys planned, nothing refused, nothing taken from `.env`.
- `reset_trading_state --env-file .env.paper`: would clear 6 orders, 20 signals, 16 job runs, 2 account snapshots and 4 flags (no positions, plans, mismatches or journal rows); kill switch on afterwards.

**Not yet verified:** the Docker image has never been built (it builds during `cdk deploy`, about 600 MB of download and upload, better on Wi-Fi), and nothing has run in AWS.

**Left to do, in order:**
1. **You:** Billing → Budgets: monthly cost budgets at $10 and $25, alerting your email (in progress 2026-10-22).
2. **Your explicit OK** (expected AWS cost about $0–1/month: Lambda, Scheduler, SSM and logs in the free tiers, about $0.10 for the image), then in order:
   1. `uv run python scripts/reset_trading_state.py --env-file .env.paper --apply`
   2. `cd infra && npx aws-cdk bootstrap --profile auratrade`
   3. `uv run python scripts/put_parameters.py --env paper --env-file .env.paper --apply --profile auratrade`
   4. `cd infra && npx aws-cdk deploy AuraTradePaper -c trading_mode=paper --profile auratrade` (Docker Desktop running; on Wi-Fi)
3. After the deploy: invoke each function once in `dry_run`, check `job_runs` and the logs, then `uv run --env-file .env.paper auratrade kill off`.
4. The acceptance: the 3 sessions and the stop hand check (1.7, #132), 10 unattended sessions, the watchdog test (disable a schedule) and the dead-man's-switch test (disable the watchdog's), the cost and Neon compute checks, and the public site deploy. Then remove the `AuraTrade-live` worktree.

## 1. Other things only you can do (not urgent)

- **Anthropic Console:** confirm a monthly spend limit is set (about $20 now, about $40 from 4.6).
- **Domain** for the public site: optional.

## 2. The build continues in plan order

Next tasks: **1.8** early AWS deploy of the skeleton (in progress, above) → **2.4** regime backtest (the regime has read `CORRECTION` since September, the same kind of miss as 2017 in the smoke run) → **3.2a** onward.

## How to resume in a new chat

- "continue" resumes task 1.8 at "Left to do" above (branch `phase-1/aws-deploy`).
- **Where the 10-22 session stopped:** the setup is done except the budgets; env files synced and checked; everything committed and pushed. Nothing has been created in AWS, and neither `--apply` has been run.
- **First steps:** `aws sts get-caller-identity --profile auratrade` (if it fails: `aws login --profile auratrade`), `aws budgets describe-budgets --account-id 391732005061 --profile auratrade` to see the two budgets, re-run both plans above, then ask for the explicit OK before step 2.
- The test database for local runs is the Docker container `auratrade-test-pg` on port 5434 (another project's Postgres holds 5432). If it is gone: `docker run -d --name auratrade-test-pg -e POSTGRES_USER=auratrade -e POSTGRES_PASSWORD=auratrade -e POSTGRES_DB=auratrade -p 5434:5432 postgres:16`. Run the tests with `TEST_DATABASE_URL=postgresql+psycopg://auratrade:auratrade@localhost:5434/auratrade AURATRADE_REQUIRE_DB=1 uv run pytest`. Stop it with `docker stop auratrade-test-pg` when not testing.
