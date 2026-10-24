# What's left: status at 2026-10-24 (task 3.8: the baseline gate failed; the strategy decision is next, see [strategy-decision.md](strategy-decision.md); `AuraTradePaper` keeps running the 1.8 acceptance)

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Build:** Phase 0 is complete. Phase 1 tasks 1.1–1.6; Phase 2 tasks 2.1–2.4; Phase 3 tasks 3.1, 3.2a (#136), 3.2b (#137), 3.3 (#138), 3.4 (#139), 3.5a and 3.5b (#140); 3.6 (#141); 3.7 (#142). Tasks 1.7 and 1.8 are **built, merged and deployed** (PRs #30, #31); their acceptance runs on AWS (below). On `main` 1,267 tests pass, coverage 97%, ruff and mypy clean.
- **Decisions:** README §16 runs to **#143** (#143: the EP proxy at 09:36, from the 3.8 diagnostics, approved; #142: the 3.7 watchlist, baseline, tracker and scorecard readings, approved; #141: the 3.6 simulator readings, approved; #140: the 3.5b guardrail, sizing and multiplier readings, approved; #139: the chart readings; #138: the 3.3 ranking choices; #137: the 3.2b scan choices; #136: the strategy-scan readings from 3.2a, approved; #135: the regime thresholds from 2.4; #132: 1.7's live acceptance moved to AWS; #133: the 1.8 build readings, approved 2026-10-22; #134: the public site, simple now and a designed showcase at 5.7). The design is frozen (#105).
- **Accounts:** both Alpaca paper accounts are active and flat (no positions, no open orders, checked 2026-10-22 14:40 ET): `dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper` ($5,000). Neon `dev` (endpoint `ep-autumn-sea-…`) is migrated to head (**0006**), holds the last 2 years of data (about 151 MB of 512), its regime rows are seeded from 2026-09-01, and its kill switch is **off**. Neon `paper` (endpoint `ep-summer-math-…`) is a full copy of `dev` made 2026-10-22, also at 0006.
- **Env files (2026-10-22):** `.env` and `.env.paper` have the same keys in the layout of `.env.example` (including `ENVIRONMENT`); every key in both passes `scripts/check_keys.py` (`--env-file .env.paper` for `paper`). Any local command runs against `paper` with `uv run --env-file .env.paper ...` (checked: it resolves to the `paper` database).
- **AWS:** account 391732005061; CLI profile `auratrade` (us-east-1) is an IAM user with `AdministratorAccess`, signed in with `aws login` (not Identity Center; equivalent for this). Budgets `auratrade-monthly-10` and `-25`. `cdk bootstrap` done; the 10 secrets are SecureStrings under `/auratrade/paper/`; **`AuraTradePaper` deployed 2026-10-22 15:48 ET** with `trading_mode=paper` (11 functions, 18 schedules in the group `auratrade-paper`). The `paper` branch was reset just before: no trading rows, **kill switch on**.
- **Network note (2026-10-22):** the Wi-Fi the PC moved to cuts the Postgres protocol to Neon (port 5432 opens, then the handshake is dropped; direct TLS too). Neon itself is fine and answers on its HTTPS SQL endpoint (`POST https://<host>/sql` with a `Neon-Connection-String` header): the reset ran that way (the script's own statements, one transaction), and `job_runs` is read that way from this network. The Lambdas are unaffected. Local commands that need the database (`auratrade ...`, `reset_trading_state`, `alembic`, the tests' Neon use) work only from another network (the hotspot) while on this one.
- **Time zones in Git Bash:** `TZ=America/New_York date` prints UTC (Git Bash ignores `TZ`); get ET from PowerShell: `[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow,'Eastern Standard Time')`.
- **healthchecks.io:** check `auratrade-watchdog`, cron `*/15 7-21 * * 1-5`, `America/New_York`, grace 30 minutes; its ping URL is in `.env.paper`.
- **Known data notes:** Alpaca's TEVA history starts on 2026-09-14 (a vendor gap, not ingest). FRED answered with invalid JSON on the evening of 2026-10-21; on 2026-10-22 it answers again (`VIXCLS` 2026-10-21 = 16.68).

## Change of plan (2026-10-21, approved by the user)

The PC is on a phone hotspot, so it can't be relied on to stay connected for the sessions. **The rest of 1.7's live acceptance (3 sessions + the stop hand check) moves to AWS**: it runs as part of task 1.8, on the separate `paper` Alpaca account. It is recorded as decision **#132** and in the plan (1.7 and 1.8 acceptance).

**The local scheduler is stopped.** It ran from the `AuraTrade-live` worktree (a separate copy of `main` at `C:\Users\dimat\Documents\GitHub\AuraTrade-live`, log `AuraTrade-live\var\local-session.log`) and was healthy: its last run, `stop_guard` at 14:30 ET, was on time. It was stopped at about 14:45 ET on a misreading of the clock (the Git Bash note above). Nothing was exposed: the `dev` account was flat; `dev` simply has no close or evening run on 10-22. AWS has taken over; the worktree is removed.

## Task 1.8: acceptance in progress (code merged: PR #31, `main` = what is deployed)

The code is merged so `main` matches AWS; 1.7 and 1.8 stay **unticked** until the acceptance passes. Fixes found during it go in as small PRs from `main`, then the user redeploys.

**Done on the branch (tested; ruff and mypy clean):**
- `jobs/schedule.py`: the whole weekday schedule as `Slot`s, the single source for the AWS stack, the watchdog and `scripts/local_session.py`; the evening chain's fallbacks fire every 10 minutes up to their deadline.
- `jobs/watchdog.py`: missing heartbeats, stuck RUNNING rows, disabled schedules; each finding alerted once per session; pings `HEALTHCHECKS_PING_URL`.
- `eod_review` at 12:40 and 15:40, running only within 30 minutes of that day's close (half days work).
- `parameters.py`: secrets from SSM at function start. `scripts/put_parameters.py`: plans by default, `--apply` writes.
- `scripts/reset_trading_state.py`: clears the trading tables a Neon branch copied from `dev` carries, resets the flags (kill switch on); plans by default, refuses the `dev` database and localhost.
- README §3, §5.13, §12, §13, §14 and decisions #132, #133 written; CLAUDE.md commands and decision count updated.
- `Dockerfile` + `.dockerignore` (allow-list; checked: the staged context holds only `Dockerfile`, `pyproject.toml`, `uv.lock`, `config/`, `src/`).
- `infra/stacks/trading.py` (`TradingStack`), wired into `infra/app.py` as `AuraTradePaper`; `npx aws-cdk synth` passes for both stacks.
- Tests: schedule, watchdog (against the test Postgres), the `eod_review` window, SSM loading, `put_parameters`, the stack synthesized offline (handlers, `dry_run` default, `live` refused, retries, grants, schedules = slots).

**Checked 2026-10-22 (plans only, nothing written):**
- `put_parameters --env paper --env-file .env.paper`: all 10 keys planned, nothing refused, nothing taken from `.env`.
- `reset_trading_state --env-file .env.paper`: would clear 6 orders, 20 signals, 16 job runs, 2 account snapshots and 4 flags (no positions, plans, mismatches or journal rows); kill switch on afterwards.

**Not yet verified:** the Docker image has never been built (it builds during `cdk deploy`, about 600 MB of download and upload, better on Wi-Fi), and nothing has run in AWS.

**Left to do, in order:**
1. ~~Budgets, reset, bootstrap, SSM secrets, deploy~~: all done 2026-10-22 (the image was first built and checked locally: every handler imports, settings and `risk.yaml` load, read-only filesystem with `/tmp` writable). `cdk deploy` is blocked for the agent by Claude Code's auto-mode classifier, so the user runs deploys.
2. ~~First runs on AWS~~: the 15:55 `stop_guard` and 16:00 `watchdog` were denied `ssm:GetParametersByPath` (the grant lacked the path's trailing slash) and failed closed; fixed (`f20d501`, the test checks the exact ARN), redeployed 16:22 ET. The first `ingest` (started by hand at 16:33, its 16:20 slot having hit the grant) ran out of memory at 2,048 MB in the fundamentals step, which held its whole batch of EDGAR company facts; fixed (`552388f`, at most 8 held, a test proves it) and `ingest` given 3,008 MB (this new account's Lambda maximum), redeployed 16:53. The 17:00 fallback then ran the chain end to end: `ingest` 267 s, peak **759 MB**, no failed steps, bar coverage 99.96%; `simulate`, `regime` (`CORRECTION`, 37 sessions), `scan` (10 signals for 10-23: MRNA, IOVA, CDNA, KOD, AGL, PLSE, SLN, PAYS, TEAM, RNG), `analyst_submit` all succeeded by 17:05. The watchdog pinged healthchecks.io on every run and alerted the morning slots that passed before the stack existed (first-day effect) and the crashed `ingest` row as stuck (that row was then marked FAILED by hand).
3. ~~Kill switch~~: turned **off** on `paper` 2026-10-22 ~17:20 ET with the user's OK (the CLI's update, sent over the HTTPS endpoint); no halts set. Friday 10-23 is the first paper session with entries.
4. ~~Public site~~: `AuraTradePublicSite` deployed 2026-10-22 16:13 ET at **https://dk5a9tokky27z.cloudfront.net** (status strip updated, #134); checked: CSP, HSTS, `X-Frame-Options: DENY`, `nosniff`, HTTP → HTTPS, 404 page.
5. The acceptance: the 3 sessions and the stop hand check (1.7, #132), 10 unattended sessions, the watchdog test (disable a schedule) and the dead-man's-switch test (disable the watchdog's), the cost and Neon compute checks (the public site is already deployed).
   - **The stop hand check, live:** once a position has filled, cancel its stop leg on the `paper` account (what a job killed right after reducing the stop leaves; the chaos test does the same against the fake broker) and confirm the next `stop_guard` run (every 5 minutes, 09:35–16:00) places a full-quantity GTC stop at the plan level. Needs the user's OK at the time.

### Session log

| Session | Morning | Close and evening |
|---|---|---|
| Fri 10-23 | Overnight: every `watchdog` run succeeded (every 15 minutes to 22:00 ET, again from 07:00). `execute_open` 09:20 (market closed: nothing to do) and 09:31, `execute_entries` 09:36: 10 signals → **2 OTO entries** (IOVA 10 sh, trigger 15.37, stop 14.42; SLN 7 sh, trigger 19.69, stop 18.48), each stop leg covering the full quantity. Skipped: MRNA and TEAM sized to 0 shares (0.25% of $5,000 is $12.50, less than one share's risk), RNG gapped above its limit (90.70 > 85.39), and **5 with "no decision price"** (CDNA, KOD, AGL, PLSE, PAYS; below). No fills by 10:00. | **Close:** neither entry filled (IOVA never reached 15.37, SLN never 19.69). `eod_review` 12:40 skipped itself as designed ("not within 30 min of the close"); at 15:40 it cancelled both unfilled OTO parents ("no entry rests overnight"), legs with them: the account ends the day flat, no positions, no open orders. `watchdog` succeeded all 37 runs, 07:00–16:00. `stop_guard` had nothing to guard (it writes no `job_runs` rows). So the stop hand check still waits for a first fill. Evening chain (post_market 16:20 → ingest 17:00 → …): not yet recorded. |

**Finding (10-23): the IEX spread cap skips half the scan.** #120's premise holds: IEX quotes are fresh (ages 0–90 s at 09:57). But for small and mid caps the IEX quote itself is wide, since IEX is not the NBBO: at 09:57 CDNA 12.5%, PLSE 14.0%, PAYS 12.4%, AGL 29.2%, RNG 8.6%, against IOVA 0.14% and SLN 0.05%. Their last IEX trades were 1–8 minutes old (AGL's from the previous day). So the provisional `max_spread` of 2% (#131) leaves them without a decision price, and the entry is skipped: failing closed, as designed. Nothing changed; to decide with the user (it sets which names can be entered at all, so it matters from 3.x, not for the skeleton).

## 1. Other things only you can do (not urgent)

- **Anthropic Console:** confirm a monthly spend limit is set (about $20 now, about $40 from 4.6).
- **Domain** for the public site: optional.

## 2. The build continues in plan order

Next tasks: **1.8** early AWS deploy of the skeleton (acceptance in progress, above) → ~~2.4~~, ~~3.2a~~, ~~3.2b~~, ~~3.3~~, ~~3.4~~, ~~3.5b~~, ~~3.6~~, ~~3.7~~ (done, below) → **3.8** baseline backtest (stopped at the gate; the strategy decision is next).

### Task 3.8: stopped at the gate; the strategy decision is next (2026-10-24)

**Start with [strategy-decision.md](strategy-decision.md):** what was found, the outside evidence, the options, the recommended plan and a prompt for the next session. Phase 4 doesn't start until the user decides.

The user chose (2026-10-24): **3.8 before deploying 3.7**: `paper` stays on the 1.8 build until the acceptance's 10 sessions are in (early November), then `alembic upgrade head` on `paper` and `cdk deploy`. The **base count** is computed inside 3.8 from the whole Parquet history; keeping the live `bases` table current is a follow-up task.

Done on `phase-3/baseline-backtest`, in order:
- **Pre-registration** (`docs/backtests/baseline-preregistration.md`), before any 3.8 code ran on real data.
- **Regime re-run on 5 pillars** (`uv run python -m backtest.baseline regime`, about 6 min; `docs/backtests/regime-5-pillar.md`): the pre-registered rule chose `cu+2_corr-2_c2_dist8`; 3 of 5 targets. **Not frozen in `risk.yaml`** (that happens only once a pool passes).
- **Data fixes** (7e3fc62): dead ETNs/ETFs typed `COMMON` are retyped (`scripts/backfill.py funds`); securities without EDGAR report dates aren't rejected by the blackout in the pool (the live rule is kept as the "survivors only" figure).
- **The pool, the gate and the comparisons** (`uv run python -m backtest.baseline pool`, about 30-40 min on cached minute bars; `docs/backtests/baseline.md`): **the gate fails.** No comparison turns it positive.
- **Diagnostics** (pre-registered in `docs/backtests/baseline-diagnostics-preregistration.md`; `uv run python -m backtest.diagnostics`; `docs/backtests/baseline-diagnostics.md`): costs +0.13R a trade, early failures, tight 09:36 stops; the non-EP setups are found correctly and lose -0.17R; the EP proxy looked ahead.
- **The EP fix (#143, approved):** the pool trades the proxy's 09:36 view; re-run in `docs/backtests/baseline.md`.
- **Fixed on the way:** the pullback gate's tested-EMA bug (c5856aa, live 3.7 code).
- **The holdout (2023 on) is still closed.**

### Task 3.7: done (2026-10-24, #142)

Migration 0007 (`daily_watchlist`, `baseline_signals`, `breakout_tracker`, `verdict_outcomes`). `scan` now also runs the full scan (`strategies/watchlist.py`, `strategies/store.py`) and writes every setup, the capped list ranked. `simulate` (`journal/evening.py`) records T's triggers of T-1's scan and the completed 5-session results, decides the baseline book's entries from T-1's list through the guardrails and re-plays every open baseline trade from its entry session (minute bars fetched for its symbols only and cached in the object store, `sim/minutes.py`; sessions from `sim/sessions.py`), and runs the scorecard on the verdicts it is given (none until Phase 4). `regime` reads the tracker for leadership health. Pure parts: `journal/breakout_tracker.py`, `scorecard.py` (units, the in-force window, the session-block bootstrap), `baseline.py`, `replay.py` (sim R and default-plan R). **Real data:** Neon `dev` is migrated to **0007**; a rolled-back run on 10-20/10-21 (with 10-20 forced to `CONFIRMED_UPTREND`, since `dev` reads `CORRECTION`) scanned 2,741 symbols in 40 s, found 80 setups, triggered 50 breakouts, and the baseline took 6 of 39, simulated them from Alpaca minute bars; it caught four bugs, now fixed and tested (EPs in the book, report distances and the cushion past T, the correlation window one close short). The user approved #142 on 2026-10-24. Open item: nothing keeps the `bases` table current, so the base count stays neutral in the watchlist. **AWS:** nothing changes there until you redeploy; the next deploy brings the full scan, `simulate` and leadership health to `paper` (more Lambda time, a few minute-bar requests an evening, S3 for the cache), and needs `alembic upgrade head` on the `paper` branch first (0007, additive).

### Task 3.6: done (2026-10-24, #141)

`sim/fills.py`: the README §5.12 fill model, one bar at a time (minute or daily): buy-stop-limits at max(trigger, open) plus slippage within the limit, else a resting limit filled only by a later bar (a miss otherwise); stops at min(stop, open) less slippage; soft exits at the decision price less slippage. `sim/trade.py`: `simulate_trade` plays one approved entry forward: the live 09:36 order check (`guardrails.orders`), the plan written at entry, `plans.resolve` at each session's decision price, tranche 2 through its gate, guardrail and re-sizing; it returns the fills, the exit rule, R and the mark at each session's close (the scorecard's R after 5/10/20). The fake broker's `play` runs the same functions over its open orders, for the Phase 5 replay tests. `risk.yaml` `sim:`: slippage tiered by dollar volume (0.05% to 0.30%). Tests on hand-built minute-bar days with known fills: gap-through stops, untriggered orders, a breakout past its limit that never comes back (a miss), one that comes back (filled at the limit), a bar reaching both the stop and +2R (stopped out), tranche 2, the +2R partial then breakeven, a trail exit, the 09:51 delayed-SIP range. The user approved #141 on 2026-10-24.

### Task 3.5b: done (2026-10-23, #140)

`guardrails/`: `evaluate(actions, context, risk)` runs the Portfolio Manager's and the EOD review's actions (regime view, entries, tranche 2, tightenings, exits) through every README §8 rule, in order, each approval counting against the next; `orders.check_order` re-checks an approved entry at 09:36 on the real trigger, limit and stop and sizes it in whole shares. `execution/triggers.py` (opening range, triggers, max chase, limit, order type, low-of-day stop), `execution/sizing.py` (planned size, tranches, tranche-2 re-sizing), `journal/risk_multiplier.py` (the README §8 table as a daily state machine), `journal/numbers.py`. Hypothesis tests feed adversarial entries (NaN, inverted stops, 50% risk, unknown symbols, pivots off the code pivot). CI now fails if `guardrails/` drops below 100% branch coverage. Nothing live calls these yet: Phase 5 wires them into the jobs; the skeleton on AWS keeps its own sizing.

### Task 3.4: done (2026-10-23, #139)

`charts.py`: the daily (1 year, 800×600) and weekly (2 years, 600×400) PNGs, stored under `charts/<session>/`. `mplfinance` is now a main dependency (the Lambda image grows; `MPLCONFIGDIR=/tmp/matplotlib` in the Dockerfile); it reaches AWS with the next deploy, and nothing there draws charts until Phase 4.

### Task 3.3: done (2026-10-23, #138)

`strategies/ranking.py` (features, the eight components, the score, `select`) and `verdict_cache.py`. The user chose: equal weights; one slot per ticker; the fundable strategies take turns for the 40 slots (a single ranking gave pullbacks 81% of them). The scan job doesn't use it yet: Phase 4 wires the Analyst. Open for 3.8: the backtest must supply the group RS rank and the base count as of each session (neutral in the real-data run).

### Task 3.2b: done (2026-10-23, #137)

`leader_pullback.py`, `emerging_leader.py`, registered with the other three. `SymbolData` now also carries the RS ratings, SPY's closes and the regime's `CORRECTION` spells; `scripts/scan_real_data.py` replays the regime for them. The user chose: an emerging leader's drawdown at most 1.5x SPY's over the correction; a pullback at least 2 x ATR deep (45 a session instead of 80); the 5-session low as the emerging leader's structural stop (46% still wider than 8%; the Analyst can tighten).

### Task 3.2a: done (2026-10-23, #136)

`strategies/base.py` (the interface: `scan`, `build_llm_context`, `system_prompt`, `entry_policy`, `exit_policy`, `default_risk`; `SymbolData` with the series cached per symbol), `registry.py`, `vcp_breakout.py`, `momentum_breakout.py`, `episodic_pivot.py` (the backtest proxy), `risk.yaml` `strategies:`. Pure, 100% branch coverage. `scripts/scan_real_data.py` → `docs/backtests/scans.md`: 2017-2022, 0 exceptions. The user approved #136 and chose: a 5% floor on a VCP's first contraction (merger-arbitrage flatliners), no momentum depth cap until 3.8. Nothing live uses the scans yet: the skeleton's scan-lite stays until 3.3 and Phase 5.

### Task 2.4: done (2026-10-23, #135)

`backtest/regime.py` and `docs/backtests/regime.md`. IBD's published thresholds failed 4 of 5 pre-registered checks on 4 pillars; the user chose option A: provisional thresholds (`CONFIRMED_UPTREND` from +2, `CORRECTION` at ≤ −2, distribution −1 from 8 days), failed-FTD round trips counted apart from the switch limit. They pass 3 checks; the two misses (uptrend coverage in 2019 / H2 2020-21, ~17 switches a year) go to the 3.8 re-run. Nothing live depends on the regime yet (the skeleton ignores it, #131); the next AWS deploy carries the new thresholds into the evening `regime` job.

## How to resume in a new chat

- **The strategy decision:** paste the prompt in [strategy-decision.md §8](strategy-decision.md#8-a-prompt-for-the-next-session). Build nothing until the user approves a plan.
- **The 1.8 acceptance** keeps running on AWS (`paper`): check each session's `job_runs` (Neon SQL Editor or the HTTPS endpoint), Alpaca orders and stops, Telegram, and record them in the "Session log" above. The 10-23 evening chain and the sessions from 10-26 are not recorded yet. The stop hand check still waits for a first fill (needs the user's OK at the time).
- **First steps for AWS:** `aws sts get-caller-identity --profile auratrade` (if it fails: `aws login --profile auratrade`), then the CloudWatch logs (`aws logs filter-log-events --log-group-name /aws/lambda/auratrade-paper-<job>`; in Git Bash prefix `MSYS_NO_PATHCONV=1` so paths starting with `/` aren't rewritten).
- The test database for local runs is the Docker container `auratrade-test-pg` on port 5434 (another project's Postgres holds 5432). If it is gone: `docker run -d --name auratrade-test-pg -e POSTGRES_USER=auratrade -e POSTGRES_PASSWORD=auratrade -e POSTGRES_DB=auratrade -p 5434:5432 postgres:16`. Run the tests with `TEST_DATABASE_URL=postgresql+psycopg://auratrade:auratrade@localhost:5434/auratrade AURATRADE_REQUIRE_DB=1 uv run pytest`. Stop it with `docker stop auratrade-test-pg` when not testing.
- **The backtest runs** need the local Parquet history (`var/storage`) and the cached minute bars; they load the calendar from Neon `dev` (port 5432 must be reachable from the network).
