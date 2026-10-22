# CLAUDE.md: Instructions for building AuraTrade

This file is loaded at the start of every Claude Code session in this repository. Read it fully before doing anything.

## 1. What this project is

AuraTrade is an autonomous swing-trading system for US stocks:
- It is modelled on Minervini (SEPA/VCP), O'Neil/IBD and Kullamägi.
- An LLM (Claude) makes trade and allocation decisions inside hard risk limits enforced by code.
- It runs serverless on AWS, using free data tiers, on a budget under $150/month.

## 2. Source-of-truth documents

| Document | What it answers | Rule |
|---|---|---|
| [README.md](README.md) | **What** the system does: every component, rule, threshold and table | The specification. Code must match it. |
| [docs/execution-plan.md](docs/execution-plan.md) | **How and in what order** to build it: phases, tasks, acceptance criteria, **progress tracker** | Work strictly in its order. |
| README §16 "Decision log" | **Why** things are the way they are (134 decisions; #51–65 come from the 2026-10-07 design review, #66–78 from the 2026-10-08 follow-up, #79–91 from the third review the same day, #92–104 from the fourth, #105–107 from the build-order review, #108 the public site, #109–112 spike findings, #113–128 the 0.4 spike review, #129 the ingest build, #130 the backfill build, #131 the skeleton build, #132 1.7's acceptance moved to AWS, #133 the AWS skeleton, #134 the public site plan) | Don't reopen a logged decision unless the user asks. **The design is frozen (#105):** don't start or propose another design review; changes come only from spike/backtest findings (§5) or the user. |
| `docs/spikes/*.md` | Findings from Phase 0 experiments | Where a spike contradicts the README, stop and ask the user (§5). |

If code and README disagree, the README wins until the user approves a change. Then update the README (and the decision log) in the same PR.

## 3. Hard rules: never break these

1. **No live trading.** Only Alpaca **paper** keys are used until the user explicitly starts Phase 8. `TRADING_MODE` defaults to `dry_run`. Never write code that silently falls back to `live`.
2. **No secrets in git.** Keys live in `.env` (git-ignored) locally and SSM Parameter Store in AWS. Never print keys in logs or chat.
3. **No paid API calls in tests or CI.** Use recorded HTTP fixtures. Anything that costs money (Claude calls, especially batches; AWS resources) needs the user's OK first, with an estimated cost.
4. **The LLM decides; code enforces limits.** Don't redesign the system as rules-only and don't suggest a "deterministic-first" approach; the user rejected it. Guardrails must never be bypassable by LLM output. **Price levels come from code:** the LLM may only nudge a pivot within the guardrail tolerance or tighten a stop, never supply an unchecked price.
5. **Protective stops always rest at the broker.** Any order code must preserve the invariant: every position's GTC stops (one stop, or one OTO leg per buy) together cover its full quantity, each at or above its plan level; a partly filled buy counts as unprotected. Sells follow the sequence: reduce the stop, sell, restore it on failure (or sell at once if the price is already through the stop level). Every order change runs under the single-writer lock. **No kill switch, halt or error path may cancel a protective stop or block a plan exit**; halts block entries and adds only.
6. **Fail closed.** On uncertainty (missing data, LLM error, calendar disagreement, reconciliation mismatch), open no new positions.
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
- **One simulator:** backtests, the baseline book, the verdict scorecard and replay tests all go through `sim/`, which calls the live pure functions. Never write a second fill model. The one exception is the spike-grade edge check (spike 0.3h): it lives in `spikes/`, tunes nothing, and nothing outside `spikes/` may import it or its data.
- **Backtests follow README §15 "Backtest validity":** point-in-time universe, raw-price filters, the fill model, the 2016–2022 tune / 2023+ holdout, and source defaults unless clearly beaten on both periods.
- **Time:** store UTC; compute in `America/New_York`; timezone-aware datetimes only. **Count trading sessions, never calendar days** (use the calendar helpers, never `timedelta(days=...)` for market windows).
- **Money and prices:** round order prices to the valid tick ($0.01 for stocks ≥ $1). Share counts are integers, floored.
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
uv run python scripts/backfill.py postgres   # the 2-year window's earnings, fundamentals, VIX into Postgres (after an ingest run)
uv run python scripts/smoke_real_data.py  # the early modules over the whole history -> docs/backtests/smoke.md
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
| **Pivot** | The breakout price level (for a VCP, the high of the final contraction). |
| **R** | The initial risk per share (entry − initial stop). "+2R" = a gain of twice that risk. |
| **ORH** | Opening-range high: the high of the first 5 minutes; an entry trigger. |
| **ADR / ATR** | Average daily range (%) / average true range; they bound stop width. |
| **VCP** | Volatility contraction pattern: successively shallower pullbacks with drying volume. |
| **EP** | Episodic pivot: a ≥ 10% gap on earnings or news. |
| **Base count** | Which base since the stage-2 start (IBD rules); 3rd → half risk, 4th+ → no entry. |
| **Distribution day / FTD** | IBD market signals: an index down ≥ 0.2% on higher volume / day 4+ of a rally with a ≥ 1.25% gain on higher volume. |
| **Tranche** | Part of a position; two-tranche entries buy 50% at the trigger and 50% on confirmation. |
| **Position plan** | The exit plan written at entry; code carries it out; tighten-only. |
| **Hard stop / soft rule** | The broker-side GTC stop / a close-based rule checked by code at the EOD review. |
| **Baseline book** | A simulated rule-only portfolio used as the benchmark for the LLM. |
| **Breakout tracker** | Every scan trigger, ungated, used for the leadership-health regime pillar. |
| **Regime** | `CONFIRMED_UPTREND` / `UNDER_PRESSURE` / `EARLY_UPTREND` (after a follow-through day) / `CORRECTION` (IBD's pre-FTD "rally attempt" is a sub-state of it). "More conservative" means lower on the ladder `CORRECTION` < `EARLY_UPTREND` < `UNDER_PRESSURE` < `CONFIRMED_UPTREND`. |
| **Heat** | Total open risk: Σ max(0, entry − hard stop) × shares across positions, plus pending entries. Capped at 6% of equity. |
| **Decision price** | The 15:40 price on which "close-based" soft rules decide (12:40 on half days): the last IEX trade if ≤ 2 min old, else the IEX quote midpoint if fresh, else none (#120). |
| **Verdict scorecard** | Every Analyst verdict simulated forward under the default plan: the main evidence for whether the LLM adds value. |
