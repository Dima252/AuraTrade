# CLAUDE.md: Instructions for building AuraTrade

This file is loaded at the start of every Claude Code session in this repository. Read it fully before doing anything.

## 1. What this project is

AuraTrade is an autonomous **portfolio manager** (#153-#156) that aims to beat the S&P 500 over 10 years, after Israeli tax:
- **The core** (80%) holds the S&P 500 at 1.3x (SPY + SSO) while a buffered 10-month moving average says the trend is up, and 0.5x (SPY + T-bills) while it is down, with a volatility cap, a −25% drawdown halt and a broker stop on the leveraged fund. **The satellite** (20%) is the LLM's, over approved ETFs. A rules-only **twin** is the LLM's benchmark.
- An LLM (Claude) makes the allocation decisions inside hard risk limits enforced by code.
- $100,000 on an Alpaca **paper** account; the user's flagship project. It runs serverless on AWS, on free data tiers, under $150/month.
- Two earlier stock-selection systems failed their pre-registered tests: the chart-pattern setups (Minervini, O'Neil/IBD, Kullamägi) and a momentum book (#146-#152). Their specs are archived in `docs/archive/`; their code stays, unwired.

## 2. Source-of-truth documents

| Document | What it answers | Rule |
|---|---|---|
| [README.md](README.md) | **What** the system does: every component, rule, threshold and table | The specification. Code must match it. |
| [docs/execution-plan.md](docs/execution-plan.md) | **How and in what order** to build it: phases, tasks, acceptance criteria, **progress tracker** | Work strictly in its order. |
| README §16 "Decision log" | **Why** things are the way they are (157 decisions; #51–65 come from the 2026-10-07 design review, #66–78 from the 2026-10-08 follow-up, #79–91 from the third review the same day, #92–104 from the fourth, #105–107 from the build-order review, #108 the public site, #109–112 spike findings, #113–128 the 0.4 spike review, #129 the ingest build, #130 the backfill build, #131 the skeleton build, #132 1.7's acceptance moved to AWS, #133 the AWS skeleton, #134 the public site plan, #135 the regime thresholds from 2.4, #136 the strategy-scan readings from 3.2a, #137 those from 3.2b, #138 the ranking from 3.3, #139 the charts from 3.4, #140 the guardrail readings from 3.5b, #141 the simulator readings from 3.6, #142 the watchlist, baseline, tracker and scorecard readings from 3.7, #143 the EP proxy at 09:36 from the 3.8 diagnostics, #144 the close-entry candidates after the failed gate, #145 the gate moved to before capital, #146 the strategy change to a momentum book (`docs/strategy-change-momentum.md`), #147-#149 the momentum book's rules, risk limits and pre-registered backtest, and the re-plan (`docs/momentum-plan.md`, `docs/backtests/momentum-preregistration.md`), #150 the book simulator readings from 3.12, #151 the backtest harness readings from 3.13, #152 the book's scored run (the gate failed), #153 the change to a portfolio manager (`docs/strategy-change-portfolio.md`), #154-#156 its rules, risk limits (and §3.5 below) and pre-registered backtest, and the re-plan (`docs/portfolio-plan.md`, `docs/backtests/portfolio-preregistration.md`), #157 the long-history readings from 3.18 | Don't reopen a logged decision unless the user asks. **The design is frozen (#105):** don't start or propose another design review; changes come only from spike/backtest findings (§5) or the user. |
| `docs/spikes/*.md` | Findings from Phase 0 experiments | Where a spike contradicts the README, stop and ask the user (§5). |
| `docs/archive/` | The setup system's and the momentum book's READMEs and plans (superseded by #146 and #153) | Code written for them cites `archived README §x`; a plain `README §x` always means the current README. Their code stays, unwired; don't wire it into the portfolio. |

If code and README disagree, the README wins until the user approves a change. Then update the README (and the decision log) in the same PR.

## 3. Hard rules: never break these

1. **No live trading.** Only Alpaca **paper** keys are used unless the user explicitly starts live trading (none is planned). `TRADING_MODE` defaults to `dry_run`. Never write code that silently falls back to `live`.
2. **No secrets in git.** Keys live in `.env` (git-ignored) locally and SSM Parameter Store in AWS. Never print keys in logs or chat.
3. **No paid API calls in tests or CI.** Use recorded HTTP fixtures. Anything that costs money (Claude calls, especially batches; AWS resources) needs the user's OK first, with an estimated cost.
4. **The LLM decides; code enforces limits.** Don't redesign the system as rules-only and don't suggest a "deterministic-first" approach; the user rejected it. Guardrails must never be bypassable by LLM output. **Price levels come from code:** the LLM never supplies a price; it chooses weights and a shift within the bands `guardrails/portfolio.py` enforces.
5. **Protective stops always rest at the broker on leveraged positions** (#155). Any order code must preserve the invariant: every leveraged fund's GTC stops (one stop, or one OTO leg per buy) together cover its full quantity, each at or above its plan level; a partly filled buy counts as unprotected. Unleveraged ETFs carry no stop; the trend rule and the halt are their exits. Sells of a stopped position follow the sequence: reduce the stop, sell, restore it on failure (or sell at once if the price is already through the stop level). Every order change runs under the single-writer lock. **No kill switch, halt or error path may cancel a protective stop or block a sale toward a lower target**; the kill switch and halts block increases of exposure only.
6. **Fail closed.** On uncertainty (missing data, LLM error, calendar disagreement, reconciliation mismatch), never increase exposure: hold or reduce, and take the twin's targets when the LLM fails.
7. **Don't push, deploy, or create AWS resources** unless the user asks. Committing on a feature branch is fine.

## 4. How to work in a session

1. **Find the current task:** open the progress tracker at the top of `docs/execution-plan.md` and take the first unchecked task. Say which task you're starting.
   - First check `git branch` for an unmerged `phase-*` branch. Its task may be done and ticked there but not on `main`. If there is one, ask the user to merge it, or branch from it, before starting.
2. **Read** the matching README sections and the task's acceptance criteria before writing code.
3. **Branch:** `phase-<n>/<short-task-name>`, e.g. `phase-1/market-calendar`.
4. **Build in small steps** with tests alongside. Run the checks (§7) before saying anything is done.
5. **Definition of done:** code + tests passing + `ruff` clean + `mypy` clean + the README updated if behaviour changed + the tracker box ticked + a short summary to the user of what was built, what was verified and what wasn't.
6. **One task per session is fine.** Prefer finishing and verifying one task over starting several.
7. **Ask the user** when a decision isn't covered by the README, the decision log or this file. Don't guess on anything involving money, orders or risk.

## 5. When reality contradicts the design

Phase 0 spikes exist to test assumptions. When a spike or a later finding contradicts the README:
1. Stop building on that assumption.
2. Write the finding in `docs/spikes/<name>.md` (what was expected, what happened, the evidence).
3. Propose 1–2 workarounds to the user with a recommendation.
4. After approval, update the README, the decision log and the plan, then continue.

## 6. Code conventions

- **Python 3.13**, managed with **uv**; `src/` layout (`src/auratrade/`), structure as in README §13.
- **Typing:** full type hints; `mypy --strict` on `src/`. Pydantic v2 for config, API payloads and LLM schemas.
- **Pure logic is pure:** indicators, regime scoring, plans, guardrails and sizing are pure functions with no I/O, unit-tested on hand-built data with known answers. Guardrails need 100% branch coverage.
- **One simulator:** backtests, the twin, the scorecard and replay tests all go through `sim/` (`sim/portfolio.py` for the portfolio, on `sim/fills.py`; `sim/book.py` and `sim/trade.py` for the earlier systems), which calls the live pure functions. Never write a second fill model. The one exception is the spike-grade edge check (spike 0.3h): it lives in `spikes/`, tunes nothing, and nothing outside `spikes/` may import it or its data.
- **Backtests follow README §15 "Backtest validity" and their pre-registration** (`docs/backtests/portfolio-preregistration.md` for the portfolio): the long history and the synthesized leveraged fund, the fill model, the Israeli tax model, no tuning, a blind check then one scored run, comparisons reported and never adopted (#156). 2017–2022 informs no choice (#152); the earlier systems' pre-registrations stay as the record.
- **Time:** store UTC; compute in `America/New_York`; timezone-aware datetimes only. **Count trading sessions, never calendar days** (use the calendar helpers, never `timedelta(days=...)` for market windows).
- **Money and prices:** round order prices to the valid tick ($0.01 for prices ≥ $1). Live share counts are integers, floored; the backtest trades dollar amounts (pre-registration §3.5).
- **Config:** every threshold lives in `config/risk.yaml` (typed and hashed), never hard-coded.
- **Jobs:** each job in `src/auratrade/jobs/` runs via the shared runner (calendar guard, heartbeat, logging, alerts) and is runnable locally: `uv run python -m auratrade.jobs.<job> --session YYYY-MM-DD --dry-run`.
- **HTTP:** httpx + tenacity, with rate limiting per vendor. **Logging:** structlog, JSON, no secrets.
- **DB:** SQLAlchemy 2.0 + Alembic. Every schema change is a migration. Mind the Neon 0.5 GB limit (retention rules in README §10).
- **Claude API:** use the official `anthropic` SDK. Model `claude-opus-5-5`, adaptive thinking with explicit `effort`, structured outputs, prompt caching, refusal handling (README §9). Load the `claude-api` skill before writing LLM code.
- Match the style of surrounding code; keep comments for *why*, not *what*.

## 7. Commands

```bash
uv sync                                   # install dependencies (dev + infra groups by default)
uv run pre-commit install                 # once per clone: lint, format, mypy, secret check on commit
docker compose up -d                      # local Postgres for tests
uv run pytest                             # tests (add --cov for branch coverage)
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # src/, scripts/ and infra/ (strict)
uv run pre-commit run --all-files         # every hook, as on commit
uv run python scripts/check_keys.py       # confirm every key in .env works (free calls only; --env-file .env.paper for paper)
uv run alembic upgrade head               # apply migrations (from task 1.2)
uv run python -m auratrade.calendar.sync  # sync market_sessions from Alpaca + cross-check (from task 1.4)
uv run python -m auratrade.jobs.<job> --session 2026-10-07 --dry-run   # from task 1.1
uv run python -m auratrade.jobs.post_market --session 2026-10-20 --dry-run --chain   # the evening chain in-process (1.7)
uv run python -m auratrade.jobs.ingest --direct --session 2026-10-14   # ingest alone, without the runner
uv run python scripts/backfill.py all     # the Parquet history since 2016 (hours, resumable; from task 1.6)
uv run python scripts/backfill.py benchmarks   # SPY and MTUM total-return bars (3.12; free calls)
uv run python scripts/backfill.py longhistory  # Kenneth French daily, Shiller dividends, SPY/SSO/BIL from 2016 (3.18; free)
uv run python scripts/backfill.py postgres   # the 2-year window's earnings, fundamentals, VIX into Postgres (after an ingest run)
uv run python scripts/smoke_real_data.py  # the early modules over the whole history -> docs/backtests/smoke.md
uv run python backtest/regime.py         # the regime backtest, 2016-2022 only (holdout closed) -> docs/backtests/regime.md
uv run python scripts/scan_real_data.py   # setup system: every strategy scan over 2017-2022 -> docs/backtests/scans.md
uv run python -m backtest.baseline regime   # 3.8: the 5-pillar regime re-run (tuning only) -> docs/backtests/regime-5-pillar.md
uv run python -m backtest.baseline pool     # setup system (3.8, done): the pool, the gate and the comparisons -> docs/backtests/baseline.md
uv run python -m backtest.diagnostics       # setup system (3.8, done): why the pool loses -> docs/backtests/baseline-diagnostics.md
uv run python -m backtest.baseline close --blind   # setup system (3.8c, done): the close entry (#144), counts only -> docs/backtests/close-entry-blind.md
uv run python -m backtest.baseline close           # setup system (3.8d, done; never re-run: pre-registered once) -> docs/backtests/close-entry.md
uv run python -m backtest.momentum --blind   # the book (3.14, done): counts and invariants only -> docs/backtests/momentum-blind.md
uv run python -m backtest.momentum           # the book (3.15, done 2026-10-26: gate failed; never re-run) -> docs/backtests/momentum.md
uv run python -m backtest.portfolio --blind  # the portfolio (3.22): counts, invariants, data checks -> docs/backtests/portfolio-blind.md
uv run python -m backtest.portfolio          # the portfolio (3.23): ONE scored run, with the user -> docs/backtests/portfolio.md
uv run auratrade kill on|off|status       # the kill switch (entries only); also: auratrade mismatches / resolve <id> --note
uv run --env-file .env.paper auratrade kill status   # any command against `paper` (its file's values override .env)
TRADING_MODE=paper uv run python scripts/local_session.py --until YYYY-MM-DD   # the skeleton's jobs on schedule (dev paper)
cd infra && npx aws-cdk synth             # synthesize the CDK stacks (needs Node; no AWS calls)
uv run python scripts/put_parameters.py --env paper --env-file .env.paper   # plan the SSM secrets (--apply --profile auratrade writes)
uv run python scripts/reset_trading_state.py --env-file .env.paper   # plan clearing dev's trading state from a copied branch (--apply: kill switch on)
cd infra && npx aws-cdk deploy AuraTradePaper -c trading_mode=paper --profile auratrade   # the skeleton on AWS (user's OK first; 1.8)
```

CI (`.github/workflows/ci.yml`) runs the same lint → type-check → test steps on every PR. Keep this section up to date.

## 8. Glossary

| Term | Meaning |
|---|---|
| **Session** | A trading day in `market_sessions`. All windows count sessions. |
| **Ranking / execution session (R / E)** | R: the last session of each month, when the trend is read. E: the next full session, when the book trades at 15:40 |
| **Core / satellite** | The core (80%): the S&P 500 at the rule level plus the LLM's shift. The satellite (20%): the LLM's weights over the approved ETF list |
| **Trend state** | UP or DOWN, from the month-end total-return value against its 10-month SMA with a ±2% buffer (README §6) |
| **Rule level** | The core's S&P exposure the rules give: 1.3x UP, 0.5x DOWN, at most 1.0x while capped or halted |
| **Shift** | The LLM's change to the rule level: ±0.2x, never above 1.5x, downward only while DOWN, capped or halted |
| **Volatility cap** | SPY's 21-session realized volatility above 25% a year on R → at most 1.0x |
| **Drawdown halt** | Equity ≤ 75% of its peak → at most 1.0x at once (SSO sold the next session); off at a DOWN → UP flip or a new peak |
| **Twin** | The rules-only portfolio: the rule level on the whole account, simulated each evening; the LLM's benchmark |
| **Drift band** | A holding is traded only if its target changed or it is more than 5 points from it |
| **Near miss** | The pre-registered result that continues the plan despite a failed gate: drawdown no deeper, after-tax CAGR at most 1 point a year behind |
| **Decision price** | The 15:40 price trades use (12:40 on half days): the last IEX trade if ≤ 2 min old, else the IEX quote midpoint if fresh, else none (#120) |
| **Regime** | The five-pillar regime classifier (`CONFIRMED_UPTREND` / `UNDER_PRESSURE` / `EARLY_UPTREND` / `CORRECTION`): a fact for the LLM, no rule reads it |
| *Earlier systems (archived):* **12-1, slot, hold zone, candidates, rule book, information coefficient** | The momentum book's terms; see `docs/archive/README-momentum-book.md` |
| *Earlier systems (archived):* **Pivot, R, ORH, ADR/ATR, VCP, EP, base count, tranche, baseline book, breakout tracker, heat** | The setup system's terms; see `docs/archive/README-setup-system.md` |
