# What's next: status at 2026-10-28 (deployed)

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) is the source of truth for task order; this page says where things stand and what blocks what. The earlier record (the setup system, the momentum book, the 1.8 acceptance log) is archived in [archive/next-steps-2026-10-26.md](archive/next-steps-2026-10-26.md).

## Where things stand

- **Strategy:** the portfolio manager (#153-#156): the S&P 500 at 1.3x in an up-trend, 0.5x in a down-trend (a buffered 10-month SMA), a volatility cap, a −25% drawdown halt, a broker stop on the 2x fund; an LLM satellite of 20%; a rules-only twin as the LLM's benchmark; judged after Israeli tax. [strategy-change-portfolio.md](strategy-change-portfolio.md), [portfolio-plan.md](portfolio-plan.md).
- **Backtest: passed (#160,** [backtests/portfolio.md](backtests/portfolio.md)**).** After tax +9.2% a year against +8.7% for the market held (1928-2026), max drawdown 68% against 85%, ahead in 69% of 1,065 ten-year windows. A thin, tax-sensitive edge from the long bear markets; a tie since the rules were published. Never re-run.
- **Build:** Phases 0-5 are built; the public dashboard (6.2) is built and waits for the deploy. The 1.7 / 1.8 live acceptance moved into 6.1.
- **Pre-deploy check (#172, #173):** the jobs ran on `dev` in `paper` mode on real data; what it found is fixed, and a sweep removed the earlier systems' remaining code and stale text. The order rehearsal on `dev` (`scripts/rehearse_orders.py`) is still to run: the portfolio's orders have so far only met the fake broker.

## Next

1. **Deployed 2026-10-28** (task 6.1's checklist, [runbook.md](runbook.md)): the `paper` account (…0O7Z) at $100,000, the kill switch off. The site is https://dk5a9tokky27z.cloudfront.net.
2. **Friday 30 Oct (R), evening:** `allocate` reads the trend and writes the facts; the trial starts there, and the dashboard's snapshot with it.
3. **Monday 2 Nov (E):** `portfolio_manager` at 08:45 (a paid call), `execute_close` at 15:40: the first real orders. Check the fills, SSO's stop at the broker, and that evening's tax lots.
4. **The carried 1.7 / 1.8 checks** (the stop hand check on SSO, the watchdog and dead-man's-switch tests, cost and Neon compute), then three rebalances unattended.

## Open items for the user

- **The remote MCP server's cap (#175): keep it uncapped, or cap it.** Deployed 2026-10-29 **without** the cap (`-c mcp_uncapped=true`): the account's Lambda concurrency limit is 10, and AWS refuses to reserve any concurrency below that (it keeps 10 unreserved). The increase to 1,000 is a support case (request `a4d984ad803345238b813986cb565fd4ntaN4rpM`, opened 2026-10-29). Both ways the public tools work the same; what differs:
  - **Uncapped (as now):** the bill stays bounded: the circuit breaker switches the server off at 1,000 calls in an hour (reserved concurrency 0: every request throttled, neither run nor billed) and alerts on Telegram, so a flood costs about an hour of calls at most (roughly 100 a second uncapped: under $1). **The risk is the trading jobs, not money:** until the alarm fires (its period is an hour, so up to an hour or two), a flood can hold all 10 of the account's concurrent Lambda runs and throttle a job; a late `execute_close` could miss its 15:40 decision price. It is safe while nobody but you has the URL (its ID is random, not guessable). If you keep it: shorten the alarm (e.g. 100 calls in 5 minutes) to narrow that window, make uncapped the default in `infra/app.py`, and update README §5.10 and #175, which describe the cap as the design.
  - **Capped:** once AWS raises the limit, `cd infra && npx aws-cdk deploy AuraTradeMcp --profile auratrade` (no flag): one run at a time, at most 10 requests a second, the trading jobs' concurrency never touched, the breaker still on top. Then the URL can be shared (the site, an interviewer). Nothing else changes.
  - Re-arming after a trip, either way: `aws lambda put-function-concurrency --function-name auratrade-mcp --reserved-concurrent-executions 1 --profile auratrade` (uncapped: `delete-function-concurrency` instead, which removes the 0).
- **MCP UI (planned, task 6.6):** a small dashboard rendered inside the AI client through MCP Apps (the SDK's `mcp.server.apps`): book vs twin vs SPY before and after tax, the exposure and satellite, the latest decision; for the showcase, after the first real snapshot and before 6.5's presentation.

- **The order rehearsal on `dev`** wasn't run (`dev` not reset); it stays available (`scripts/rehearse_orders.py`).
- **README §5.9** says LLM payloads go to S3; they are kept in `llm_decisions`. The user decides which is right.

## Environment notes

- **Accounts:** two Alpaca paper accounts, `dev` (`.env`) and `paper` (`.env.paper`); Neon branches `dev` and `paper`. Any command runs against `paper` with `uv run --env-file .env.paper ...`.
- **AWS:** account 391732005061, CLI profile `auratrade` (us-east-1), signed in with `aws login`. `cdk deploy` is run by the user (Claude Code's auto mode blocks it).
- **Local tests** need Docker Desktop running (`docker compose up -d`) for the database tests; CI runs them always.
- **Time zones in Git Bash:** `TZ=America/New_York date` prints UTC; use PowerShell for Eastern time.
- **The long history** is in the local store (`scripts/backfill.py longhistory`, #157): 26,317 sessions from 1926-07-01 to 2026-08-31.
