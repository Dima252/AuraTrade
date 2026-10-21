# What's left: status at 2026-10-21 (task 1.7: live acceptance running)

A handoff for the next session. The [progress tracker](execution-plan.md#progress-tracker) remains the source of truth for task order; this page says what blocks what right now.

## Where things stand

- **Build:** Phase 0 is complete. Phase 1 tasks 1.1–1.6; Phase 2 tasks 2.1–2.3; Phase 3 tasks 3.1 and 3.5a. Task 1.7 is **built and merged** (PR #30); only its live acceptance is left (below). 1,177 tests pass, coverage 97%, ruff and mypy clean.
- **Decisions:** README §16 runs to **#131** (the 1.7 build readings, approved 2026-10-21). The design is frozen (#105).
- **Accounts:** both Alpaca paper accounts are active (`dev` ...JG3C in `.env`, `paper` ...CDFD in `.env.paper`). Neon `dev` is migrated to head (**0006**), holds the last 2 years of data (about 151 MB of 512), its regime rows are seeded from 2026-09-01, and its kill switch is **off** (entries allowed on `dev`).
- **Known data notes:** Alpaca's TEVA history starts on 2026-09-14 (a vendor gap, not ingest). On 2026-10-21 FRED answered with invalid JSON for VIX, VIX3M and the credit spread in the evening run; ingest carried on (non-critical step) and the regime still ran. Check that it is back on 10-22 (`job_runs` → `ingest` → `details.macro.errors`).

## Task 1.7: live acceptance (2026-10-22 to 10-26)

**What is running:** `TRADING_MODE=paper uv run python scripts/local_session.py --until 2026-10-26`, in the background on this PC, log `var/local-session.log`. It runs every skeleton job at its time on the `dev` paper account. **The PC must stay on and awake through Monday 10-26 evening.** If it stopped, restart it with the same command (runs already past are skipped).

10-21 was a shakedown session (entries placed late, at 12:55 ET); none filled, and the 15:40 review cancelled all three as designed. The evening chain wrote 10 signals for 10-22.

### The checks still to do

Times in ET, and in Tel Aviv time (ET+7 until Israel's clocks change on Sun 10-25, ET+6 on Mon 10-26).

| When (ET) | Tel Aviv | Session | What runs | What to check |
|---|---|---|---|---|
| Thu 10-22 09:20–16:20 | 16:20–23:20 | counted session 1 | `execute_open` 09:20 + 09:31, `execute_entries` 09:36, `stop_guard` every 5 min, `eod_review` 15:40, `post_market --chain` 16:20 | see "Each session" below |
| Fri 10-23 09:20–16:20 | 16:20–23:20 | counted session 2 | the same | the same |
| Mon 10-26 09:20–16:20 | 15:20–22:20 | counted session 3 | the same; the scheduler ends after it | the same, then the final review |
| The first session where an entry has filled | after 09:40 ET, before 15:35 ET | any | the stop hand check | see "Stop hand check" below |

**Each session** (after its 16:20 run, about 16:30 ET):
1. `var/local-session.log`: every run `exit 0`; on a holiday each job would log a calendar skip instead (none fall in this window).
2. `job_runs` for the session: `execute_open` ×2, `execute_entries`, `eod_review`, `post_market`, the chain (`ingest`, `simulate`, `regime`, `scan`, `analyst_submit`) all `SUCCEEDED`; `stop_guard` rows appear only if it repaired something.
3. At the broker (Alpaca `dev`): every position's GTC sell stops cover its full quantity, each at or above its plan level; no partly filled entry left unprotected; no client order id used twice; no entry order resting after 15:40.
4. The Telegram daily report arrived and matches the broker (equity, positions, today's closes).

**Stop hand check** (accept item "a job killed right after reducing a stop is repaired by `stop_guard` within one run"): needs an open position. During market hours, shrink or cancel that position's stop by hand in the Alpaca `dev` dashboard (what a job dying between "reduce the stop" and "sell" leaves behind), note the time, and confirm the next `stop_guard` run (≤ 5 minutes) restores a full-quantity GTC stop at the plan level, with a `stop_guard` row in `job_runs` and a Telegram alert. If no entry has filled by Monday, keep the scheduler running past 10-26 until one does, rather than forcing a trade.

**Already accepted:** the concurrent-run lock check (two `execute_open` at once on 10-21: the second waited for the first), all 7 automated chaos scenarios (in CI), and the calendar guards (in tests; the live check is item 1 above).

**Final review (after Mon 10-26, about 16:30 ET / 22:30 Tel Aviv):** if every check passed, tick 1.7 in the tracker with a one-line summary, update this page, and commit on a short `docs/` branch. Then decide what happens to the `dev` positions still open: either keep the scheduler running (their stops rest at the broker either way, but the 21-EMA exit and `stop_guard` only run while it does), or flatten the `dev` account. 1.8 trades on the separate `paper` account.

## 1. Things only you can do (before task 1.8, not urgent)

- **AWS account:** root MFA, an admin user via IAM Identity Center, budget alarms at $10 / $25.
- **AWS CLI** installed on this PC (Node and the GitHub CLI are already there).
- **healthchecks.io** free account, for the external dead-man's switch.
- **Anthropic Console:** confirm a monthly spend limit is set (about $20 now, about $40 from 4.6).
- **Domain** for the public site: optional.

## 2. The build continues in plan order

Next tasks: **1.8** early AWS deploy of the skeleton (needs your OK and the account steps above: it creates AWS resources; a cost estimate comes first) → **2.4** regime backtest (the regime has read `CORRECTION` since September, the same kind of miss as 2017 in the smoke run) → **3.2a** onward.

## How to resume in a new chat

- "check the session" runs the per-session checks above for the latest session.
- "continue" after 10-26: the final review above, then task 1.8.
- The test database for local runs is the Docker container `auratrade-test-pg` on port 5434 (another project's Postgres holds 5432); stop it with `docker stop auratrade-test-pg` when not testing.
