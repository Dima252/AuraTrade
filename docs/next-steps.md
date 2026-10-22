# What's left: status at 2026-10-22 midday (task 1.8 built and tested offline; your AWS setup next)

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Build:** Phase 0 is complete. Phase 1 tasks 1.1–1.6; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. Task 1.7 is **built and merged** (PR #30); only its live acceptance is left, now on AWS (below). On the 1.8 branch 1,258 tests pass, coverage 97%, ruff and mypy clean.
- **Decisions:** README §16 runs to **#133** (#132: 1.7's live acceptance moved to AWS; #133: the 1.8 build readings, approved 2026-10-22). The design is frozen (#105).
- **Accounts:** both Alpaca paper accounts are active (`dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper`). Neon `dev` is migrated to head (**0006**), holds the last 2 years of data (about 151 MB of 512), its regime rows are seeded from 2026-09-01, and its kill switch is **off** (entries allowed on `dev`).
- **Known data notes:** Alpaca's TEVA history starts on 2026-09-14 (a vendor gap, not ingest). On 2026-10-21 FRED answered with invalid JSON for VIX, VIX3M and the credit spread in the evening run; ingest carried on (non-critical step) and the regime still ran. Check that it is back on 10-22 (`job_runs` → `ingest` → `details.macro.errors`).

## Change of plan (2026-10-21, approved by the user)

The PC is on a phone hotspot, so it can't be relied on to stay connected for the sessions. **The rest of 1.7's live acceptance (3 sessions + the stop hand check) moves to AWS**: it runs as part of task 1.8, on the separate `paper` Alpaca account. It is recorded as decision **#132** and in the plan (1.7 and 1.8 acceptance).

Until AWS is live, the local scheduler keeps running as a bonus (nothing depends on it). It stopped when the 10-21 chat ended (no runs on the morning of 10-22, nothing was open) and was restarted 10-22 at 11:17 ET from this chat, so it stops again when this chat ends. It runs from a separate copy of `main` at `C:\Users\dimat\Documents\GitHub\AuraTrade-live` (a git worktree, so work on a branch can't change the code it runs), log `AuraTrade-live\var\local-session.log`, through 10-26 on the `dev` account. If the PC drops off, nothing is at risk: every position's stop rests at Alpaca. When AWS takes over: stop it, then `git worktree remove ..\AuraTrade-live`, and flatten or keep the `dev` account (your call).

## Your part before the deploy (about 30-45 minutes)

1. **AWS account:** sign up (the Free plan if offered); MFA on the root user.
2. **IAM Identity Center** (region US East, N. Virginia): enable, add yourself, assign the `AdministratorAccess` permission set; accept the invite, set up MFA; note the "AWS access portal URL".
3. **Billing → Budgets:** monthly cost budgets at $10 and $25, alerting your email.
4. **AWS CLI v2** (Windows MSI), then `aws configure sso` (portal URL, region `us-east-1`, profile name `auratrade`).
5. **healthchecks.io:** free account, one check `auratrade-watchdog`; put its ping URL in `.env.paper` as `HEALTHCHECKS_PING_URL=...`.
6. **Neon:** a new branch `paper` from the branch `.env` uses; its connection string into `.env.paper` as `DATABASE_URL` (now `localhost`).

Expected AWS cost: about $0-1/month (Lambda, Scheduler, SSM and logs in the free tiers; about $0.10 for the image). **Nothing is created in AWS until you confirm.** Data note: the first deploy downloads the Lambda base image and uploads the built image, about 600 MB in all, so it is better done on Wi-Fi.

## Task 1.8: in progress (branch `phase-1/aws-deploy`, pushed, work in progress)

**Done on the branch (tested; ruff and mypy clean):**
- `jobs/schedule.py`: the whole weekday schedule as `Slot`s, the single source for the AWS stack, the watchdog and `scripts/local_session.py`; the evening chain's fallbacks fire every 10 minutes up to their deadline.
- `jobs/watchdog.py`: missing heartbeats, stuck RUNNING rows, disabled schedules; each finding alerted once per session; pings `HEALTHCHECKS_PING_URL`.
- `eod_review` at 12:40 and 15:40, running only within 30 minutes of that day's close (half days work).
- `parameters.py`: secrets from SSM at function start. `scripts/put_parameters.py`: plans by default, `--apply` writes.
- `Dockerfile` + `.dockerignore` (allow-list; checked: the staged context holds only `Dockerfile`, `pyproject.toml`, `uv.lock`, `config/`, `src/`).
- `infra/stacks/trading.py` (`TradingStack`), wired into `infra/app.py` as `AuraTradePaper`; `npx aws-cdk synth` passes for both stacks.
- Tests: schedule, watchdog (against the test Postgres), the `eod_review` window, SSM loading, `put_parameters`, the stack synthesized offline (handlers, `dry_run` default, `live` refused, retries, grants, schedules = slots).

**Not yet verified:** the Docker image has never been built (it builds during `cdk deploy`, about 600 MB of download and upload, better on Wi-Fi), and nothing has run in AWS.

**Left to do, in order:**
1. **Your part** above (AWS account, Identity Center, budgets, CLI profile `auratrade`, healthchecks.io, Neon `paper` branch). A dry run of `put_parameters` on today's `.env.paper` refuses only on the two things still missing there: `HEALTHCHECKS_PING_URL` and a Neon `DATABASE_URL` (now `localhost`). The shared keys (Anthropic, FRED, Finnhub, EDGAR, Telegram) are taken from `.env` when `.env.paper` leaves them empty.
2. With your OK: prepare the Neon `paper` branch (migrate to head, clear the trading tables copied from `dev`, regime seed, kill switch on until ready), `cdk bootstrap`, `put_parameters --apply`, `cdk deploy AuraTradePaper -c trading_mode=paper`; then a first `dry_run` invoke of each job, `auratrade kill off` on `paper`.
3. The acceptance: the 3 sessions and the stop hand check (1.7, #132), 10 unattended sessions, the watchdog test (disable a schedule) and the dead-man's-switch test (disable the watchdog's), the cost and Neon compute checks, and the public site deploy. Then stop the local scheduler and remove the `AuraTrade-live` worktree.

## 1. Other things only you can do (not urgent)

- **Anthropic Console:** confirm a monthly spend limit is set (about $20 now, about $40 from 4.6).
- **Domain** for the public site: optional.

## 2. The build continues in plan order

Next tasks: **1.8** early AWS deploy of the skeleton (in progress, above) → **2.4** regime backtest (the regime has read `CORRECTION` since September, the same kind of miss as 2017 in the smoke run) → **3.2a** onward.

## How to resume in a new chat

- "continue" resumes task 1.8 at "Left to do" above (branch `phase-1/aws-deploy`).
- The test database for local runs is the Docker container `auratrade-test-pg` on port 5434 (another project's Postgres holds 5432); stop it with `docker stop auratrade-test-pg` when not testing.
