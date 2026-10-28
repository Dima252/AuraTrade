# What's next: status at 2026-10-28

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) is the source of truth for task order; this page says where things stand and what blocks what. The earlier record (the setup system, the momentum book, the 1.8 acceptance log) is archived in [archive/next-steps-2026-10-26.md](archive/next-steps-2026-10-26.md).

## Where things stand

- **Strategy:** the portfolio manager (#153-#156): the S&P 500 at 1.3x in an up-trend, 0.5x in a down-trend (a buffered 10-month SMA), a volatility cap, a −25% drawdown halt, a broker stop on the 2x fund; an LLM satellite of 20%; a rules-only twin as the LLM's benchmark; judged after Israeli tax. [strategy-change-portfolio.md](strategy-change-portfolio.md), [portfolio-plan.md](portfolio-plan.md).
- **Backtest: passed (#160,** [backtests/portfolio.md](backtests/portfolio.md)**).** After tax +9.2% a year against +8.7% for the market held (1928-2026), max drawdown 68% against 85%, ahead in 69% of 1,065 ten-year windows. A thin, tax-sensitive edge from the long bear markets; a tie since the rules were published. Never re-run.
- **Build:** Phases 0-5 are built; the public dashboard (6.2) is built and waits for the deploy. The 1.7 / 1.8 live acceptance moved into 6.1.
- **Pre-deploy check (#172, #173):** the jobs ran on `dev` in `paper` mode on real data; what it found is fixed, and a sweep removed the earlier systems' remaining code and stale text. The order rehearsal on `dev` (`scripts/rehearse_orders.py`) is still to run: the portfolio's orders have so far only met the fake broker.

## Next

1. **The order rehearsal on `dev`:** the user resets the `dev` Alpaca account to $100,000; then, during market hours, the rehearsal's three passes (up-trend buys with SSO's stop, a down-trend sale through the stop sequence, `auratrade flatten`), and that evening's tax lots from the real fills.
2. **Task 6.1, the deploy:** [runbook.md](runbook.md)'s start checklist. For the first rebalance to be Monday 2 Nov, it must be done before **Friday 30 Oct, 16:20 ET** (R's evening); otherwise the first E is Tuesday 1 Dec.

## Open items for the user

- **The `paper` account** is reset to $100,000 in the Alpaca dashboard (it holds $5,000 and 7 SLN from the setup system).
- **The `paper` database** is at migration 0006; the checklist migrates it to head (0012).
- **The old stack (`AuraTradePaper`)** still runs the setup skeleton with the kill switch on; the deploy replaces it.
- **README §5.9** says LLM payloads go to S3; they are kept in `llm_decisions`. The user decides which is right.

## Environment notes

- **Accounts:** two Alpaca paper accounts, `dev` (`.env`) and `paper` (`.env.paper`); Neon branches `dev` and `paper`. Any command runs against `paper` with `uv run --env-file .env.paper ...`.
- **AWS:** account 391732005061, CLI profile `auratrade` (us-east-1), signed in with `aws login`. `cdk deploy` is run by the user (Claude Code's auto mode blocks it).
- **Local tests** need Docker Desktop running (`docker compose up -d`) for the database tests; CI runs them always.
- **Time zones in Git Bash:** `TZ=America/New_York date` prints UTC; use PowerShell for Eastern time.
- **The long history** is in the local store (`scripts/backfill.py longhistory`, #157): 26,317 sessions from 1926-07-01 to 2026-08-31.
