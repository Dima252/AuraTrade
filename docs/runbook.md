# Runbook

What to do to start the paper trial, and what to do when the system alerts. Every command runs from the repo root. Add `uv run --env-file .env.paper` in front of a command to run it against `paper`; without it, it runs against `dev`.

## Starting the trial (task 6.1)

In order. The trend is read at the close of the month's last session (R), and the book trades on the next one (E). For E on Monday 2 Nov, everything below must be done before **Friday 30 Oct, 16:20 ET**. After that, the first E is Tuesday 1 Dec.

| # | Step | Command | Check |
|---|---|---|---|
| 1 | Reset the `paper` account to $100,000 | Alpaca dashboard → the `paper` account → Reset | Equity $100,000, no positions (it holds 7 SLN from the setup system) |
| 2 | Migrate the `paper` database (at 0006; head is 0011) | `uv run --env-file .env.paper alembic upgrade head` | `alembic current` shows head |
| 3 | Clear its old trading state | `uv run python scripts/reset_trading_state.py --env-file .env.paper`, read the plan, then add `--apply` | Kill switch on afterwards |
| 4 | Reset the drawdown halt's peak to the new equity | `uv run --env-file .env.paper auratrade halt reset` | `halt status`: off, peak 100,000.00 |
| 5 | Put the secrets in SSM | `uv run python scripts/put_parameters.py --env paper --env-file .env.paper`, read the plan, then add `--apply --profile auratrade` | 8 parameters under `/auratrade/paper/` |
| 6 | Deploy (the site first, then the trading stack) | `cd infra && npx aws-cdk deploy AuraTradePaper -c trading_mode=paper --profile auratrade` | Outputs show `TradingMode paper`; the site URL loads |
| 7 | Allow increases | `uv run --env-file .env.paper auratrade kill off` | `kill status`: off |
| 8 | Watch the first evening | Telegram daily report after 16:20 ET; `job_runs` for `post_market` → `allocate` | No FAILED or BLOCKED alerts |

Then on E: `portfolio_manager` at 08:45 (a paid Claude call, about $0.15-0.30), `execute_close` at 15:40, `stop_guard` every 5 minutes 09:35-16:00. That evening `post_market` builds the tax lots from the fills, and `simulate` publishes the dashboard's snapshot.

The order path was rehearsed on `dev` before the deploy: `scripts/rehearse_orders.py`.

## Alerts

Every alert starts `[AuraTrade <env>]`. None of them needs an answer for the system to stay safe: on doubt it holds or reduces (CLAUDE.md §3.6). They say what it couldn't do.

| Alert | What it means | What to do |
|---|---|---|
| `<job> <date> FAILED: …` | A job raised | Read the error in CloudWatch (`/aws/lambda/auratrade-paper-<job>`). An evening job's successor won't start; the chain's fallback slots retry until their deadline. Fix, then re-run by hand: `uv run --env-file .env.paper python -m auratrade.jobs.<job> --session <date>` (the evening chain: `post_market --chain`) |
| `<job> <date> BLOCKED: <predecessor> has not succeeded` | The evening chain stopped before this job | Fix the predecessor first (above). On R, `allocate` must run that evening, or E holds |
| `regime` FAILED with `no market_regime row before …: seed it` | The regime table is empty (a new database) | `python -m auratrade.jobs.regime --from <a session about 3 months back> --session <yesterday>` (the replay warms up on the 252 sessions before `--from`; bars are kept 2 years). A gap after missed evenings fills itself |
| `ingest <date>: bars INCOMPLETE` | A required fund has no bar | Usually the vendor is late: re-run `ingest --direct --session <date>` later that evening. Until the session is COMPLETE, no increase of exposure next session |
| `ingest …: steps failed, data left as it was` | Corporate actions, macro or retention failed | Nothing urgent; the next evening retries |
| `database at N MB of 512 MB` | Neon's free tier is filling | Check `retention` in `risk.yaml`; prune or archive |
| `stop invariant repaired for …` | SSO's stop didn't cover every share; the job placed or fixed it | Check the stop in the Alpaca dashboard. `STILL UNPROTECTED` means the fix failed: place a GTC stop by hand at the level in `fund_stops` |
| `SSO's stop filled: … Explain it with portfolio_manager --event STOP_FILLED` | The broker's stop sold SSO; the proceeds go into BIL (this run, or the next 15:40) until the next E (§7.2) | Optionally explain it: `portfolio_manager --event STOP_FILLED --detail "…"` (paid, about $0.15-0.30) |
| `SSO is through its stop level: selling …` | The price is below the stop level; the code sells, as the stop would have | Nothing; the proceeds go into BIL at the next 15:40 |
| `… sale … rejected …; restoring the stop` | A sale failed; the stop is back | The next run tries again. Check the Alpaca status page |
| `unknown position at the broker` | The account holds something outside the funds | Sell or explain it by hand, then `auratrade resolve <id> --note "…"`. Increases are blocked until then |
| Split mismatch (`Paper doesn't apply splits`) | A fund split, and paper didn't adjust it | Fix the position by hand at the broker, then `auratrade resolve <id> --note "…"` |
| `DRAWDOWN HALT ON` | Equity is 25% below its peak | Nothing: SSO is sold into SPY at 15:40 next session. Optionally explain it: `portfolio_manager --event HALT --detail "…"` (paid) |
| `the rebalance of E … didn't finish on its last try` | E and its retry left trades owed | The book holds where it is until the next E. Read `portfolio_targets.executions` for why |
| `portfolio_manager …: the LLM failed …` | No valid decision; the rules' level and last month's satellite were used | Read the call's `failure`, `detail` and `turns` in `llm_decisions`. Nothing else: holding is the fallback (#164) |
| `… guardrail clips …` | The decision broke a limit and code clipped it | Nothing; the journal shows it |
| `<job> has waited N s for the order lock` | Two order jobs overlapped | If it recurs, look for a stuck job in `job_runs` |
| `watchdog: - <job> missed` | A scheduled job left no heartbeat | Check the schedule group and CloudWatch; re-run the job by hand if it matters (an order job only on its session) |
| No `watchdog` ping at healthchecks.io | The watchdog itself stopped | Check the AWS account and the Lambda |

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
