# What's left: status at 2026-10-21 evening (task 1.8 started; 1.7's acceptance moves to AWS)

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Build:** Phase 0 is complete. Phase 1 tasks 1.1–1.6; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. Task 1.7 is **built and merged** (PR #30); only its live acceptance is left (below). 1,177 tests pass, coverage 97%, ruff and mypy clean.
- **Decisions:** README §16 runs to **#131** (the 1.7 build readings, approved 2026-10-21). The design is frozen (#105).
- **Accounts:** both Alpaca paper accounts are active (`dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper`). Neon `dev` is migrated to head (**0006**), holds the last 2 years of data (about 151 MB of 512), its regime rows are seeded from 2026-09-01, and its kill switch is **off** (entries allowed on `dev`).
- **Known data notes:** Alpaca's TEVA history starts on 2026-09-14 (a vendor gap, not ingest). On 2026-10-21 FRED answered with invalid JSON for VIX, VIX3M and the credit spread in the evening run; ingest carried on (non-critical step) and the regime still ran. Check that it is back on 10-22 (`job_runs` → `ingest` → `details.macro.errors`).

## Change of plan (2026-10-21, approved by the user)

The PC is on a phone hotspot, so it can't be relied on to stay connected for the sessions. **The rest of 1.7's live acceptance (3 sessions + the stop hand check) moves to AWS**: it runs as part of task 1.8, on the separate `paper` Alpaca account. This still has to be written into the plan (1.7 and 1.8 acceptance) and the README decision log as **#132**.

Until AWS is live, the local scheduler keeps running as a bonus (nothing depends on it): it now runs from a separate copy of `main` at `C:\Users\dimat\Documents\GitHub\AuraTrade-live` (a git worktree, so work on a branch can't change the code it runs), log `AuraTrade-live\var\local-session.log`, through 10-26 on the `dev` account. If the PC drops off, nothing is at risk: every position's stop rests at Alpaca. When AWS takes over: stop it, then `git worktree remove ..\AuraTrade-live`, and flatten or keep the `dev` account (your call).

## Your part before the deploy (about 30-45 minutes)

1. **AWS account:** sign up (the Free plan if offered); MFA on the root user.
2. **IAM Identity Center** (region US East, N. Virginia): enable, add yourself, assign the `AdministratorAccess` permission set; accept the invite, set up MFA; note the "AWS access portal URL".
3. **Billing → Budgets:** monthly cost budgets at $10 and $25, alerting your email.
4. **AWS CLI v2** (Windows MSI), then `aws configure sso` (portal URL, region `us-east-1`, profile name `auratrade`).
5. **healthchecks.io:** free account, one check `auratrade-watchdog`; put its ping URL in `.env.paper` as `HEALTHCHECKS_PING_URL=...`.
6. **Neon:** a new branch `paper` from the branch `.env` uses; its connection string into `.env.paper` as `DATABASE_URL` (now `localhost`).

Expected AWS cost: about $0-1/month (Lambda, Scheduler, SSM and logs in the free tiers; about $0.10 for the image). **Nothing is created in AWS until you confirm.** Data note: the first deploy downloads the Lambda base image and uploads the built image, about 600 MB in all, so it is better done on Wi-Fi.

## Task 1.8: in progress (branch `phase-1/aws-deploy`, pushed, work in progress)

**Done on the branch (no new tests yet; ruff and mypy clean):**
- `jobs/schedule.py`: the whole weekday schedule as `Slot`s (`skeleton_slots`), the single source for the AWS stack, the watchdog and `scripts/local_session.py` (now derived from it); the evening chain's fallbacks fire every 10 minutes up to their deadline; `expected_heartbeats` for the watchdog.
- `jobs/watchdog.py`: missing heartbeats past `jobs.watchdog_grace_minutes` (15), stuck RUNNING rows, disabled schedules (AWS); each finding alerted once per session; pings `HEALTHCHECKS_PING_URL`.
- `eod_review`: scheduled at 12:40 and 15:40; the module's `JOB` runs only within `jobs.eod_review_window_minutes` (30) of the session's close, so half days work.
- `parameters.py`: secrets from SSM (`SSM_PARAMETER_PATH`) loaded at function start, wired into `wiring.load_runtime_settings`.
- Settings `healthchecks_ping_url` and `schedule_group`; the two `jobs` keys above in `risk.yaml`; boto3 stubs for ssm and scheduler.
- `Dockerfile` + `.dockerignore` (an allow-list: no `.env` or `var/` can reach the image).
- `infra/stacks/trading.py`: `TradingStack` (11 Lambda functions on one image, async retries on for order jobs only, one schedule per cron group in `America/New_York`, SSM read policy, chain invoke grants, a private bucket, 30-day log groups; `trading_mode` defaults to `dry_run`, `live` refused).

**Left to do, in order:**
1. Tests: `tests/jobs/test_schedule.py` (slot times, chain fallbacks, expected heartbeats incl. half days), `test_watchdog.py` (findings, once-per-session alerts, ping, schedule states), the `eod_review` window, `test_parameters.py` (a fake SSM client), `tests/infra/test_trading_stack.py` (function names, the `dry_run` default, retries, crons and timezone, bucket access only where needed, `cron_expressions`). Then the full suite.
2. Wire `TradingStack` into `infra/app.py` (`AuraTradePaper`, `-c trading_mode=paper`) and check `npx aws-cdk synth`.
3. `scripts/put_parameters.py --env paper --env-file .env.paper [--apply]`: SecureStrings under `/auratrade/paper/`, names only in the output, never `TRADING_MODE`.
4. Docs: the plan (the 1.7/1.8 acceptance move), README §3/§5.13/§12/§13 (watchdog details, the half-day `eod_review`, the image, SSM at start), decision **#132** (the move to AWS, approved) and the 1.8 build readings for your approval, CLAUDE.md commands, this page.
5. With your OK, after your part above: prepare the Neon `paper` branch (clear the trading tables copied from `dev`, kill switch on until ready), `cdk bootstrap`, `put_parameters --apply`, `cdk deploy AuraTradePaper`; then the acceptance: the 3 sessions and the stop hand check, 10 unattended sessions, the watchdog and dead-man's-switch tests, the cost and Neon compute checks, and the public site deploy.

## 1. Other things only you can do (not urgent)

- **Anthropic Console:** confirm a monthly spend limit is set (about $20 now, about $40 from 4.6).
- **Domain** for the public site: optional.

## 2. The build continues in plan order

Next tasks: **1.8** early AWS deploy of the skeleton (in progress, above) → **2.4** regime backtest (the regime has read `CORRECTION` since September, the same kind of miss as 2017 in the smoke run) → **3.2a** onward.

## How to resume in a new chat

- "continue" resumes task 1.8 at "Left to do" above (branch `phase-1/aws-deploy`).
- The test database for local runs is the Docker container `auratrade-test-pg` on port 5434 (another project's Postgres holds 5432); stop it with `docker stop auratrade-test-pg` when not testing.
