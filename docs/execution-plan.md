# AuraTrade: the plan

What is built, what is left, and how to work on it. The system itself is specified in the [README](../README.md); the builder's rules are in [CLAUDE.md](../CLAUDE.md).

Tasks marked 👤 need you (accounts, approvals, deploys, money).

## How to start a session

1. **Merge the previous task's branch into `main` first.** Its box is ticked on that branch, so until it's merged the next session sees the task as open.
2. Open a new Claude Code chat in this folder (`CLAUDE.md` loads automatically) and say: **"Take the next task from the plan."**
3. Claude names the task, reads the README sections it touches, builds and verifies it against its acceptance criteria, and ticks the box.

## Where things stand

**The system is built and running on paper.** Deployed on AWS on 2026-10-28 against a $100,000 Alpaca paper account; the first trend reading was R 2026-10-30, the first rebalance E 2026-11-02. The public dashboard is at [auratrade.dev](https://auratrade.dev).

| Area | Built | README |
|---|---|---|
| The backtest | The pre-registered test of the twin on 1926-2026: **passed** (+9.2% a year after tax against +8.7%, max drawdown 68% against 85%) | §15, #7, #8 |
| Data | The market calendar and its cross-check; the evening ingest of the ETF list (bars, corporate actions, VIX/VIX3M, FRED); the long history for the backtest | §4, §5.1 |
| The rules | The trend dial, the volatility cap, the drawdown halt, the twin's targets (pure, 100% branch coverage) | §5.3, §6, §7 |
| The regime | The three-pillar classifier, a fact for the LLM | §5.2 |
| The LLM | The Claude client, the Portfolio Manager's facts, tools, prompt and answer; the guardrails (100% branch coverage) | §5.4, §8, §9 |
| Simulation | One simulator for the backtest, the twin, the book's replay and SPY held; the scorecard | §5.5 |
| Execution | Reconciliation, the stop invariant and `stop_guard`, the sell sequence, the single-writer lock, the rebalance at 15:40, the halt's sale, the stop's proceeds; chaos-tested on a fake broker | §5.6, §7 |
| Tax and journal | The live tax lots from the broker's activities, the account snapshots, the monthly journal, the nightly backup | §5.7 |
| Operations | The jobs on AWS (one Lambda each), the evening chain, the schedule, the watchdog and dead-man's switch, Telegram alerts, the operator CLI, [the runbook](runbook.md) | §2, §3, §5.9, §12 |
| The showcase | The public site and its dashboard on auratrade.dev; the read-only MCP server (local and remote) with its dashboard as an MCP App | §5.8, §5.10 |

## Open tasks

- [ ] **6.1 👤 Three monthly rebalances unattended.** The trial runs; check each E: the fills, SSO's stop at the broker, that evening's tax lots and snapshot. The carried checks, each once: the stop hand check on SSO (a stop deleted at the broker is replaced by `stop_guard` within one run), the watchdog and the dead-man's switch (a missed heartbeat alerts; a stopped watchdog alerts from healthchecks.io), and the month's AWS cost and Neon compute. **Accept:** three rebalances executed or correctly blocked, README §15's operations criteria met.
- [x] **6.2 👤 Deploy the sweep of 2026-10-31.** *(Done 2026-10-31: the dropped data saved locally first (`var/backup-0013/`); `paper` and `dev` at 0013, their models matching the schema; the regime replayed (`paper` to 10-30, `dev` to 10-27, its last bars); `AuraTradePaper` deployed, all 10 functions on the new image, a smoke run of `regime` clean; the site's page updated.)* Migration 0013 drops the unused tables and the regime's two empty pillars. On `paper` and `dev`: `alembic upgrade head`, then replay the regime history (`python -m auratrade.jobs.regime --from 2026-09-01 --session <last session>`), then `cdk deploy AuraTradePaper -c trading_mode=paper`, outside market hours ([the runbook](runbook.md#deploying-a-change)). **Accept:** `alembic current` shows 0013 on both branches; the next evening's chain runs clean.
- [ ] **6.3 👤 The first real snapshot.** After Monday 2 Nov's evening: the dashboard on auratrade.dev checked by eye on real data; then the `dashboard` tool checked in claude.ai (web and phone). **Accept:** both pages draw the trial's first month correctly.
- [x] **6.4 👤 The remote MCP server's cap.** *(Done 2026-11-01: the account's Lambda concurrency limit raised to 1,000; `AuraTradeMcp` deployed capped, reserved concurrency 1, 999 left unreserved for the trading jobs; `initialize` and `tools/list` answer on the live URL.)* Reserved concurrency 1 on the public server, so a flood can hold one run at most and never delays a trading job; the breaker switches it off at 1,000 calls in an hour (re-armed with `aws lambda put-function-concurrency --function-name auratrade-mcp --reserved-concurrent-executions 1`). **Accept:** deployed capped; the URL can be shared.
- [ ] **6.5 👤 The presentation.** The codebase, the documentation and the dashboard, for a reader new to the project (an interviewer). The sweep of 2026-10-31 (only the current system, a renumbered decision log) is its first part; the rest to your requirements. Next: the dashboard, to your design. Then the codebase, in this order:
  1. **A front door:** a one-page overview before the specification (what it is, a diagram, the results, the live dashboard, how to run it, where to look).
  2. **A code tour and showcase touches:** the 6-8 files worth reading (the guardrails, the trend dial, the stop invariant, the simulator, the Claude client), a CI badge, a dashboard screenshot.
  3. **Names that reflect the design:** `strategies/` holds only the trend dial; `journal/` mixes the books' simulation with the journal; `market_regime.computed_regime`.
  4. **Test doubles out of `src/`:** the fake broker and the in-memory job store into `tests/`.
  5. **Large files split along their seams:** `journal/portfolio.py`, `sim/portfolio.py`, `config.py`.
- [ ] **6.6 👤 Twelve months of paper.** Monthly journal reviews (the MCP server's `monthly_review` prompt). Before month 12, pre-register the rule for the LLM's latitude in `docs/backtests/latitude.md`, **on process, not returns** (#4): rule-following, the calibration of the expectations, the tax realized. **Accept:** the rule committed before month 12; the year's scorecard reported.
- [ ] **7.1 👤 A second broker** (optional; only if real money is ever planned): an adapter behind `execution/broker.py` (e.g. Interactive Brokers, which serves Israeli residents), a separate stack, live keys, and your explicit start (CLAUDE.md §3.1).

## Ground rules

- **Branching:** `main` is always deployable. Every change is a branch → PR → CI green → merge.
- **Definition of done:** code + tests + `mypy --strict` + ruff clean + the README updated if behaviour changed (and its decision log, with the reason).
- **Tests:** pure logic on hand-built data with known answers; recorded HTTP fixtures, never a live or paid call in CI; the order layer's chaos tests against the fake broker. **CI enforces at least 95% branch coverage overall, and 100% on `guardrails/`, `core_allocation`, `tax/`, `calendar/rebalance.py`, `journal/portfolio_scorecard.py` and `execution/rebalance.py`.**
- **Every job runs locally** with `--session YYYY-MM-DD --dry-run` before it is deployed.
- **No secrets in git:** `.env` / `.env.paper` locally (`.env.example` committed), SSM Parameter Store on AWS.
- **Findings from data** go in `docs/spikes/`; backtest reports in `docs/backtests/`.

## Accounts and environment

| Item | Notes |
|---|---|
| Alpaca: **two paper accounts** | `dev` (`.env`, local work) and `paper` (`.env.paper`, the deployed trial, …0O7Z). Sharing one would make each environment's reconciliation fight the other's positions |
| Anthropic API key | With a monthly spend limit set in the Console |
| FRED API key | Free |
| Neon | Free tier; `dev` and `paper` branches |
| Telegram bot + chat ID | The alerts |
| healthchecks.io | The watchdog's external dead-man's switch |
| AWS | CLI profile `auratrade` (us-east-1), signed in with `aws login`; budget alarms at $10 and $25. `cdk deploy` is run by you |
| The domain | `auratrade.dev`, registered at Porkbun (auto-renew on; Route 53 doesn't sell domains to Free Tier accounts), its nameservers on a Route 53 zone; the domain and the zone's ID are set in `infra/cdk.json`, so every deploy keeps them. Keep DNSSEC off at Porkbun unless it is set up in Route 53 first |
| Local tests | Docker Desktop running (`docker compose up -d`, or a Postgres container with `TEST_DATABASE_URL`) for the database tests; CI runs them always |
| The long history | In the local store (`scripts/backfill.py longhistory`): 26,317 sessions from 1926-07-01 to 2026-08-31 |
