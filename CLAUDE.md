# CLAUDE.md: Instructions for working on AuraTrade

This file is loaded at the start of every Claude Code session in this repository. Read it fully before doing anything.

## 1. What this project is

AuraTrade is an autonomous **portfolio manager** that aims to beat the S&P 500 over 10 years, after Israeli tax:
- **The book** holds the S&P 500 at 1.3x (SPY + SSO) while a buffered 10-month moving average says the trend is up, and 0.5x (SPY + T-bills) while it is down, with a volatility cap, a −25% drawdown halt and a broker stop on the leveraged fund. **The satellite** (20%) is the LLM's, over approved ETFs; its neutral is SPY, and **the core** (80%) is sized so the neutral book is exactly the rules-only **twin**, the LLM's benchmark (#4). Trading a gain realizes 25% tax: holding is the default.
- An LLM (Claude) makes the allocation decisions inside hard risk limits enforced by code.
- $100,000 on an Alpaca **paper** account, deployed on AWS (serverless, free data tiers, under $150/month), public at [auratrade.dev](https://auratrade.dev). The user's flagship project, shown at interviews.

## 2. Source-of-truth documents

| Document | What it answers | Rule |
|---|---|---|
| [README.md](README.md) | **What** the system does: every component, rule, threshold and table | The specification. Code must match it. |
| README §16 "Decision log" | **Why** things are the way they are: each decision in force, with its reason (#1–#39) | Don't reopen a logged decision unless the user asks. **The design is frozen (#12):** don't start or propose another design review; changes come only from findings in data (§5) or the user. |
| [docs/execution-plan.md](docs/execution-plan.md) | **What is left**, in order, with acceptance criteria; the accounts and environment | Work strictly in its order. |
| [docs/runbook.md](docs/runbook.md) | What to do for each alert, and how to deploy a change | Keep it current with the alerts the code sends. |
| `docs/spikes/*.md` | The findings from data the design rests on | A new finding that contradicts the README goes to the user first (§5). |
| `docs/backtests/` | The pre-registration and the scored run (passed, #8) | Records: never re-run, never edited except to fix references. |

If code and README disagree, the README wins until the user approves a change. Then update the README (and the decision log) in the same PR.

## 3. Hard rules: never break these

1. **No live trading.** Only Alpaca **paper** keys are used unless the user explicitly starts live trading (none is planned). `TRADING_MODE` defaults to `dry_run`. Never write code that silently falls back to `live`.
2. **No secrets in git.** Keys live in `.env` (git-ignored) locally and SSM Parameter Store in AWS. Never print keys in logs or chat.
3. **No paid API calls in tests or CI.** Use recorded HTTP fixtures. Anything that costs money (Claude calls; AWS resources) needs the user's OK first, with an estimated cost.
4. **The LLM decides; code enforces limits.** Don't redesign the system as rules-only and don't suggest a "deterministic-first" approach; the user rejected it (#1). Guardrails must never be bypassable by LLM output. **Price levels come from code:** the LLM never supplies a price; it chooses weights and a shift within the bands `guardrails/portfolio.py` enforces.
5. **Protective stops always rest at the broker on leveraged positions** (#5). Any order code must preserve the invariant: every leveraged fund's GTC stops (one stop, or one OTO leg per buy) together cover its full quantity, each at or above its level; a partly filled buy counts as unprotected. Unleveraged ETFs carry no stop; the trend rule and the halt are their exits. Sells of a stopped position follow the sequence: reduce the stop, sell, restore it on failure (or sell at once if the price is already through the stop level). Every order change runs under the single-writer lock. **No kill switch, halt or error path may cancel a protective stop or block a sale toward a lower target**; the kill switch and halts block increases of exposure only.
6. **Fail closed.** On uncertainty (missing data, LLM error, calendar disagreement, reconciliation mismatch), never increase exposure: hold or reduce, and hold when the LLM fails (no shift, last month's satellite, #6).
7. **Don't push, deploy, or create AWS resources** unless the user asks. Committing on a feature branch is fine.

## 4. How to work in a session

1. **Find the current task:** take the first unchecked task in `docs/execution-plan.md`. Say which task you're starting.
   - First check `git branch` for an unmerged `phase-*` branch. Its task may be done and ticked there but not on `main`. If there is one, ask the user to merge it, or branch from it, before starting.
2. **Read** the matching README sections and the task's acceptance criteria before writing code.
3. **Branch:** `phase-<n>/<short-task-name>`, e.g. `phase-6/site-domain`.
4. **Build in small steps** with tests alongside. Run the checks (§7) before saying anything is done.
5. **Definition of done:** code + tests passing + `ruff` clean + `mypy` clean + the README updated if behaviour changed + the task's box ticked + a short summary to the user of what was built, what was verified and what wasn't.
6. **One task per session is fine.** Prefer finishing and verifying one task over starting several.
7. **Ask the user** when a decision isn't covered by the README, the decision log or this file. Don't guess on anything involving money, orders or risk.

## 5. When reality contradicts the design

When data (a vendor's behaviour, a paper fill, a backtest) contradicts the README:
1. Stop building on that assumption.
2. Write the finding in `docs/spikes/<name>.md` (what was expected, what happened, the evidence).
3. Propose 1–2 workarounds to the user with a recommendation.
4. After approval, update the README, the decision log and the plan, then continue.

## 6. Code conventions

- **Python 3.13**, managed with **uv**; `src/` layout (`src/auratrade/`), structure as in README §13.
- **Typing:** full type hints; `mypy --strict` on `src/`, `scripts/`, `backtest/` and `infra/`. Pydantic v2 for config, API payloads and LLM schemas.
- **Pure logic is pure:** indicators, the regime, the trend dial, the guardrails, the rebalance's orders, the tax book and the scorecard are pure functions with no I/O, unit-tested on hand-built data with known answers. The guardrails and the portfolio's pure layer need 100% branch coverage (CI enforces it).
- **One simulator:** the backtest, the twin, the book's replay, the scorecard and the replay tests all go through `sim/portfolio.py` on `sim/fills.py`, which call the live pure functions (#11). Never write a second fill model.
- **The backtest follows README §15 and its pre-registration** (`docs/backtests/portfolio-preregistration.md`): the long history and the synthesized leveraged fund, the fill model, the Israeli tax model, no tuning, a blind check then one scored run, comparisons reported and never adopted (#7). It has run (#8) and is never re-run.
- **Time:** store UTC; compute in `America/New_York`; timezone-aware datetimes only. **Count trading sessions, never calendar days** (use the calendar helpers, never `timedelta(days=...)` for market windows).
- **Money and prices:** round order prices to the valid tick ($0.01 for prices ≥ $1). Live share counts are integers, floored; the backtest trades dollar amounts (pre-registration §3.5).
- **Config:** every threshold lives in `config/risk.yaml` (typed and hashed), never hard-coded.
- **Jobs:** each job in `src/auratrade/jobs/` runs via the shared runner (calendar guard, heartbeat, logging, alerts) and is runnable locally: `uv run python -m auratrade.jobs.<job> --session YYYY-MM-DD --dry-run`.
- **HTTP:** httpx + tenacity, with rate limiting per vendor. **Logging:** structlog, JSON, no secrets.
- **DB:** SQLAlchemy 2.0 + Alembic. Every schema change is a migration. Mind the Neon 0.5 GB limit (retention in README §10).
- **Claude API:** use the official `anthropic` SDK. Model `claude-opus-5-5`, adaptive thinking with explicit `effort`, structured outputs, prompt caching, refusal handling (README §9). Load the `claude-api` skill before writing LLM code.
- **Comments** explain *why*, not *what*, and describe the code as it is: no build history (task numbers, earlier designs). Cite the README by section and the decision log by number (`#18`).
- Match the style of surrounding code.

## 7. Commands

```bash
uv sync                                   # install dependencies (dev, infra and mcp groups by default)
uv run pre-commit install                 # once per clone: lint, format, mypy, secret check on commit
docker compose up -d                      # local Postgres for tests
uv run pytest                             # tests (add --cov for branch coverage)
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # src/, scripts/, backtest/ and infra/ (strict)
uv run pre-commit run --all-files         # every hook, as on commit
uv run python scripts/check_keys.py       # confirm every key in .env works (free calls only; --env-file .env.paper for paper)
uv run alembic upgrade head               # apply migrations
uv run python -m auratrade.calendar.sync  # sync market_sessions from Alpaca + cross-check
uv run python -m auratrade.jobs.<job> --session 2026-10-07 --dry-run   # any job, locally
uv run python -m auratrade.jobs.post_market --session 2026-10-20 --dry-run --chain   # the evening chain in-process
uv run python -m auratrade.jobs.ingest --direct --session 2026-10-14   # ingest alone (the ETF list), without the runner
uv run python -m auratrade.jobs.regime --from 2026-09-01 --session 2026-10-30   # replay the regime's history
uv run python -m auratrade.jobs.allocate --session 2026-10-30 --dry-run   # on R: rule level, twin, the PM's facts
uv run python -m auratrade.jobs.portfolio_manager --session 2026-11-02 --dry-run   # on E: PAID Claude call (~$0.15-0.30)
uv run python -m auratrade.jobs.portfolio_manager --event HALT --detail "..." --session YYYY-MM-DD --dry-run   # an event, by hand
uv run python -m auratrade.jobs.execute_close --session 2026-11-02 --dry-run   # 15:40: the halt's sale, the rebalance on E, the stop's proceeds into BIL
uv run python scripts/backfill.py longhistory  # Kenneth French daily, Shiller dividends, SPY/SSO/BIL from 2016 (free)
uv run python -m backtest.portfolio --blind  # the backtest's blind check (done) -> docs/backtests/portfolio-blind.md
uv run python -m backtest.portfolio          # the scored run (done 2026-10-27, passed, #8; never re-run) -> docs/backtests/portfolio.md
uv run python scripts/record_claude_fixtures.py --paid   # PAID (< $0.10, user's OK first): real Claude responses as fixtures
uv run auratrade kill on|off|status       # the kill switch (increases only); also: auratrade mismatches / resolve <id> --note
uv run auratrade halt status|reset        # the drawdown halt and its peak (reset after a paper account reset)
uv run --env-file .env.paper auratrade kill status   # any command against `paper` (its file's values override .env)
uv run --env-file .env.paper auratrade flatten --confirm   # emergency: kill switch on, sell everything (market hours); docs/runbook.md for every alert
TRADING_MODE=paper uv run python scripts/local_session.py --until YYYY-MM-DD   # the jobs on schedule locally (dev paper; PAID Claude call on E)
uv run python scripts/rehearse_orders.py plan --level 1.3 --satellite SPY=0.15,GLD=0.05   # dev only: a decided rebalance for today, traded by the real execute_close (then `clear`)
uv run python scripts/reset_trading_state.py --env-file .env.paper   # plan clearing a copied branch's trading state (--apply: kill switch on)
uv run python scripts/put_parameters.py --env paper --env-file .env.paper   # plan the SSM secrets (--apply --profile auratrade writes)
uv run python scripts/preview_site.py     # the public site and its dashboard on 127.0.0.1:8000 (the local snapshot; --snapshot FILE for another)
uv run --env-file .env.paper python -m auratrade.mcp_server   # the read-only MCP server over stdio (Claude Code reads .mcp.json; --public: the snapshot's tools only)
cd infra && npx aws-cdk synth             # synthesize the CDK stacks (needs Node; no AWS calls)
cd infra && npx aws-cdk deploy AuraTradePaper -c trading_mode=paper --profile auratrade   # the trading stack (user's OK first; deploys the site first; docs/runbook.md "Deploying a change")
cd infra && npx aws-cdk deploy AuraTradeMcp --profile auratrade   # the remote MCP server (user's OK first; -c mcp_uncapped=true while the Lambda limit is 10); prints McpUrl
```

CI (`.github/workflows/ci.yml`) runs the same lint → type-check → test steps on every PR, with the coverage floors. Keep this section up to date.

## 8. Glossary

| Term | Meaning |
|---|---|
| **Session** | A trading day in `market_sessions`. All windows count sessions. |
| **Ranking / execution session (R / E)** | R: the last session of each month, when the trend is read. E: the next full session, when the book trades at 15:40 |
| **Core / satellite** | The satellite (20%): the LLM's weights over the approved ETF list. The core (80%): SPY / SSO / BIL sized so the book, with the satellite at its neutral, is at the rule level plus the shift: core level = (level − 0.2) / 0.8 (#4) |
| **Neutral** | The satellite all in SPY with no shift: the book is then exactly the twin. Tilts are scored against it |
| **Trend state** | UP or DOWN, from the month-end total-return value against its 10-month SMA with a ±2% buffer (README §6) |
| **Rule level** | The book's S&P exposure the rules give: 1.3x UP, 0.5x DOWN, at most 1.0x while capped or halted |
| **Shift** | The LLM's change to the rule level: ±0.2x, never above 1.5x, downward only while DOWN, capped or halted |
| **Volatility cap** | SPY's 21-session realized volatility above 25% a year on R → at most 1.0x |
| **Drawdown halt** | Equity ≤ 75% of its peak → at most 1.0x at once (SSO sold the next session); off at a DOWN → UP flip or a new peak |
| **Twin** | The rules-only portfolio: the rule level on the whole account, simulated each evening; the LLM's benchmark |
| **Sim return** | The book's own decisions replayed through the simulator; the like-for-like comparison with the twin (paper fills are optimistic) |
| **Drift band** | A holding is traded only if its target changed or it is more than 5 points from it |
| **Hold rule / tax budget** | A satellite weight within 2 points of last month's target keeps it; the satellite's sales may realize at most 0.2% of equity in tax a year, except while DOWN, capped or halted (#4) |
| **Near miss** | The pre-registered result that would have continued the plan despite a failed gate: drawdown no deeper, after-tax CAGR at most 1 point a year behind |
| **Decision price** | The 15:40 price trades use (12:40 on half days): the last IEX trade if ≤ 2 min old, else the IEX quote midpoint if fresh, else none (#23) |
| **Regime** | The three-pillar regime classifier (`CONFIRMED_UPTREND` / `UNDER_PRESSURE` / `EARLY_UPTREND` / `CORRECTION`): a fact for the LLM, no rule reads it (#27) |
