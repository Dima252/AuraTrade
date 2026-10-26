# What's next: status at 2026-10-27

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) is the source of truth for task order; this page says where things stand and what blocks what. The earlier record (the setup system, the momentum book, the 1.8 acceptance log) is archived in [archive/next-steps-2026-10-26.md](archive/next-steps-2026-10-26.md).

## Where things stand

- **Strategy:** the portfolio manager (#153-#156): the S&P 500 at 1.3x in an up-trend, 0.5x in a down-trend (a buffered 10-month SMA), a volatility cap, a −25% drawdown halt, a broker stop on the 2x fund; an LLM satellite of 20%; a rules-only twin as the LLM's benchmark; judged after Israeli tax. [strategy-change-portfolio.md](strategy-change-portfolio.md), [portfolio-plan.md](portfolio-plan.md).
- **Backtest: passed (#160,** [backtests/portfolio.md](backtests/portfolio.md)**).** After tax +9.2% a year against +8.7% for the market held (1928-2026), max drawdown 68% against 85%, ahead in 69% of 1,065 ten-year windows. A thin, tax-sensitive edge from the long bear markets; a tie since the rules were published. Never re-run.
- **Build:** Phase 3 is complete (3.17-3.23). Phases 0-2 are complete except the 1.7 / 1.8 live acceptance, which moves into 6.1.
- **Cleanup (#161):** the momentum book's code and every earlier backtest harness are deleted; their reports are in `backtests/archive/`, their plans in `archive/`. The setup system's code that the running skeleton and the regime still use stays until task 5.2 retires it (its checklist is in the plan).

## Next

1. **Discuss with the user** what comes next (2026-10-28).
2. **Phase 4, task 4.1: the Claude client.** Load the `claude-api` skill first; a cost estimate for the user's OK before any paid call (CLAUDE.md §3.3).

## Open items for the user

- **The old AWS stack (`AuraTradePaper`)** runs the setup skeleton with the **kill switch on** (since 2026-10-27), so it places nothing. To remove it: `cd infra && npx aws-cdk destroy AuraTradePaper --profile auratrade` (its S3 bucket is kept). Task 6.1 deploys the portfolio's stack.
- **The public site** (`AuraTradePublicSite`) shows the portfolio manager from #161 once redeployed: `cd infra && npx aws-cdk deploy AuraTradePublicSite --profile auratrade`.
- **The `paper` account** is reset to $100,000 in the Alpaca dashboard before the trial (6.1); it holds $5,000 now.

## Environment notes

- **Accounts:** two Alpaca paper accounts, `dev` (`.env`) and `paper` (`.env.paper`); Neon branches `dev` and `paper`. Any command runs against `paper` with `uv run --env-file .env.paper ...`.
- **AWS:** account 391732005061, CLI profile `auratrade` (us-east-1), signed in with `aws login`. `cdk deploy` is run by the user (Claude Code's auto mode blocks it).
- **Local tests** need Docker Desktop running (`docker compose up -d`) for the database tests; CI runs them always.
- **Time zones in Git Bash:** `TZ=America/New_York date` prints UTC; use PowerShell for Eastern time.
- **The long history** is in the local store (`scripts/backfill.py longhistory`, #157): 26,317 sessions from 1926-07-01 to 2026-08-31.
