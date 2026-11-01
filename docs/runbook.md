# Runbook

What to do when the system alerts, and how to deploy a change. Every command runs from the repo root. Add `uv run --env-file .env.paper` in front of a command to run it against `paper`; without it, it runs against `dev`.

## A month on paper

The trend is read at the close of the month's last session (R), and the book trades on the next full session (E): `portfolio_manager` at 08:45 (a paid Claude call, about $0.15-0.30), `execute_close` at 15:40, `stop_guard` every 5 minutes 09:35-16:00. That evening `post_market` builds the tax lots from the fills, and `simulate` publishes the dashboard's snapshot. Between rebalances only the stop, the drawdown halt and reconciliation act.

## Deploying a change

Outside market hours (no order job running), in order:

| # | Step | Command | Check |
|---|---|---|---|
| 1 | Migrate the database, if the change has a migration | `uv run --env-file .env.paper alembic upgrade head` (and without `--env-file` for `dev`) | `alembic current` shows the head |
| 2 | If the migration rescored `market_regime` (0013 did), replay its history | `uv run --env-file .env.paper python -m auratrade.jobs.regime --from 2026-09-01 --session <the last session>` | The replay logs its final regime |
| 3 | Deploy the trading stack (it deploys the site first) | `cd infra && npx aws-cdk deploy AuraTradePaper -c trading_mode=paper --profile auratrade` | Outputs show `TradingMode paper` |
| 4 | Watch the next evening | Telegram daily report after 16:20 ET; `job_runs` for `post_market` → `allocate` | No FAILED or BLOCKED alerts |

The remote MCP server is its own stack: `cd infra && npx aws-cdk deploy AuraTradeMcp --profile auratrade`. After a paper account reset: `auratrade halt reset`, and `scripts/reset_trading_state.py` to clear the old account's orders and lots (it plans, then `--apply`; the kill switch is on afterwards).

## Alerts

Every alert starts `[AuraTrade <env>]`. None of them needs an answer for the system to stay safe: on doubt it holds or reduces (CLAUDE.md §3.6). They say what it couldn't do.

| Alert | What it means | What to do |
|---|---|---|
| `<job> <date> FAILED: …` | A job raised | Read the error in CloudWatch (`/aws/lambda/auratrade-paper-<job>`). An evening job's successor won't start; the chain's fallback slots retry until their deadline. Fix, then re-run by hand: `uv run --env-file .env.paper python -m auratrade.jobs.<job> --session <date>` (the evening chain: `post_market --chain`) |
| `<job> <date> BLOCKED: <predecessor> has not succeeded` | The evening chain stopped before this job | Fix the predecessor first (above). On R, `allocate` must run that evening, or E holds |
| `regime` FAILED with `no market_regime row before …: seed it` | The regime table is empty (a new database) | `python -m auratrade.jobs.regime --from <a session about 3 months back> --session <yesterday>` (the replay warms up on the 252 sessions before `--from`; bars are kept 2 years). A gap after missed evenings fills itself |
| `ingest <date>: bars INCOMPLETE` | A required fund has no bar | Usually the vendor is late: re-run `ingest --direct --session <date>` later that evening. Until the session is COMPLETE, no increase of exposure next session |
| `ingest …: steps failed, data left as it was` | Corporate actions, macro or retention failed | Nothing urgent; the next evening retries |
| `database at N MB of 512 MB` | Neon's free tier is filling | Bars older than `ingest.bar_history_sessions` already move to Parquet each evening; find the table that grew (`pg_total_relation_size`) and archive or prune it |
| `stop invariant repaired for …` | SSO's stop didn't cover every share; the job placed or fixed it | Check the stop in the Alpaca dashboard. `STILL UNPROTECTED` means the fix failed: place a GTC stop by hand at the level in `fund_stops` |
| `SSO's stop filled: … Explain it with portfolio_manager --event STOP_FILLED` | The broker's stop sold SSO; the proceeds go into BIL (this run, or the next 15:40) until the next E (§7.2) | Optionally explain it: `portfolio_manager --event STOP_FILLED --detail "…"` (paid, about $0.15-0.30) |
| `SSO is through its stop level: selling …` | The price is below the stop level; the code sells, as the stop would have | Nothing; the proceeds go into BIL at the next 15:40 |
| `… sale … rejected …; restoring the stop` | A sale failed; the stop is back | The next run tries again. Check the Alpaca status page |
| `unknown position at the broker` | The account holds something outside the funds | Sell or explain it by hand, then `auratrade resolve <id> --note "…"`. Increases are blocked until then |
| Split mismatch (`Paper doesn't apply splits`) | A fund split, and paper didn't adjust it | Fix the position by hand at the broker, then `auratrade resolve <id> --note "…"` |
| `DRAWDOWN HALT ON` | Equity is 25% below its peak | Nothing: SSO is sold into SPY at 15:40 next session. Optionally explain it: `portfolio_manager --event HALT --detail "…"` (paid) |
| `the rebalance of E … didn't finish on its last try` | E and its retry left trades owed | The book holds where it is until the next E. Read `portfolio_targets.executions` for why |
| `portfolio_manager …: the LLM failed …` | No valid decision; the rules' level and last month's satellite were used | Read the call's `failure`, `detail` and `turns` in `llm_decisions`. Nothing else: holding is the fallback (#6) |
| `… guardrail clips …` | The decision broke a limit and code clipped it | Nothing; the journal shows it |
| `<job> has waited N s for the order lock` | Two order jobs overlapped | If it recurs, look for a stuck job in `job_runs` |
| `watchdog: - <job> missed` | A scheduled job left no heartbeat | Check the schedule group and CloudWatch; re-run the job by hand if it matters (an order job only on its session) |
| No `watchdog` ping at healthchecks.io | The watchdog itself stopped | Check the AWS account and the Lambda |
| `MCP server auratrade-mcp switched off: …` | The public MCP server took 1,000 calls in an hour; the breaker set its reserved concurrency to 0 | Nothing else is affected. Look at who called (`/aws/lambda/auratrade-mcp`); once it has stopped, re-arm with the command in the alert (`put-function-concurrency … 1`) |

## Operator commands

```bash
uv run --env-file .env.paper auratrade kill on|off|status   # increases only; `on` cancels resting buys
uv run --env-file .env.paper auratrade halt status|reset    # the drawdown halt and its peak
uv run --env-file .env.paper auratrade mismatches           # open reconciliation and split mismatches
uv run --env-file .env.paper auratrade resolve <id> --note "what was done"
uv run --env-file .env.paper auratrade flatten --confirm    # kill switch on, then sell everything (market hours)
```

`flatten` is the emergency exit: it sells SSO with the sell sequence and everything else with marketable limits, and leaves the kill switch on so nothing buys back. Undo with `kill off`; the next E trades back to its targets.

## Pausing and stopping

- **Pause trading, keep the record:** `auratrade kill on`. Stops stay; sales toward lower targets go on.
- **Leave the market:** `auratrade flatten --confirm` during market hours.
- **Stop everything:** `cd infra && npx aws-cdk destroy AuraTradePaper --profile auratrade` (its storage bucket is kept). Resting GTC stops stay at the broker.
