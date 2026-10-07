# AuraTrade

An autonomous, event-driven swing-trading system for US equities. It is modelled as closely as possible on the documented methods of successful growth/momentum swing traders: Mark Minervini (**SEPA / VCP**), William O'Neil and IBD (**CAN SLIM**: market direction, bases and sell rules) and Kristjan Kullamägi (**breakouts and episodic pivots**). A **market-regime layer** switches strategy and risk as conditions change. The sources behind each rule are listed in [§18](#18-methodology-sources).

An LLM (Claude) makes the trading and capital-allocation decisions. Python computes the facts the LLM reasons over, carries out each position's exit plan, and enforces hard risk limits the LLM cannot override. The system runs serverless on AWS, on a budget of **under $150/month** (about $15–25/month expected).

> **Status:** design complete, build not started. The step-by-step build plan and progress tracker are in [docs/execution-plan.md](docs/execution-plan.md); the builder's working rules are in [CLAUDE.md](CLAUDE.md).
>
> **Disclaimer:** this is experimental software that will trade real money once live. Nothing here is financial advice. It runs on an Alpaca **paper** account first. Never risk capital you cannot afford to lose.

---

## Contents

1. [Design principles](#1-design-principles)
2. [System architecture](#2-system-architecture)
3. [Daily schedule](#3-daily-schedule)
4. [Market calendar and data integrity](#4-market-calendar-and-data-integrity)
5. [Components](#5-components)
6. [Market regimes and strategies](#6-market-regimes-and-strategies)
7. [Entries, position plans and exits](#7-entries-position-plans-and-exits)
8. [Risk management and guardrails](#8-risk-management-and-guardrails)
9. [LLM design and token budget](#9-llm-design-and-token-budget)
10. [Data model](#10-data-model)
11. [Tech stack](#11-tech-stack)
12. [Infrastructure and cost](#12-infrastructure-and-cost)
13. [Repository layout](#13-repository-layout)
14. [Configuration](#14-configuration)
15. [Validation and go-live criteria](#15-validation-and-go-live-criteria)
16. [Decision log](#16-decision-log)
17. [Known limitations](#17-known-limitations)
18. [Methodology sources](#18-methodology-sources)

---

## 1. Design principles

1. **Trade like the practitioners, not like a model.** Every rule maps to a documented practice of a successful trader (§18) or is a guardrail that keeps automation safe. Defaults that the sources disagree on are chosen by backtest, not by taste.
2. **The LLM decides; code enforces the envelope.** Claude chooses setups, risk and exit plans. Code rejects or clips anything outside hard limits.
3. **Python computes facts; the LLM interprets them.** All indicators, patterns, base counts and share counts are computed in code.
4. **Each position follows its entry plan, and changes can only tighten it.** The plan is written at entry and carried out by code. Later LLM decisions may only make it more conservative.
5. **Losses small, winners big.** Tight initial stops, quick exits from breakouts that don't work, partial sales into strength, and letting the remainder run on a trailing stop.
6. **Progressive exposure.** Start small, size up when trades are working, size down when they aren't, in both the market and the account.
7. **The broker is the source of truth; fail closed for entries, fail safe for exits.** Hard stops always rest at the broker.
8. **Everything is logged.** LLM decisions can't be honestly backtested, so the log is the validation record.
9. **Only spend tokens when a decision is needed.**

---

## 2. System architecture

```mermaid
flowchart TD
    subgraph EVE["Evening pipeline (after close, session T)"]
        PM1[post_market<br/>reconcile · journal · report]
        ING[ingest<br/>bars · macro · earnings · fundamentals · industries]
        REG[regime<br/>5 pillars → computed regime]
        SCAN[scan<br/>all strategies · features · ranking · charts · plan levels]
        ANS[analyst_submit<br/>Batch API]
        ANC[analyst_collect]
        PM1 --> ING --> REG --> SCAN --> ANS --> ANC
    end

    subgraph MORN["Trading day (session T+1)"]
        PRE[premarket_check 07:30<br/>macro · news · earnings]
        PMG[portfolio_manager 08:40/08:45<br/>gaps + EP candidates → LLM: regime view · EP review · allocation · plans]
        GR{{guardrails}}
        EXO[execute_open 09:20<br/>OPG exits · hard stops]
        EXE[execute_entries 09:36<br/>pivot / opening-range-high triggers]
        MON[intraday_monitor ×3<br/>early-behaviour rules]
        EOD[eod_review<br/>code: soft rules · LLM: tranche 2, failures]
        PRE --> PMG --> GR --> EXO --> EXE --> MON --> EOD
    end

    ANC --> PRE

    ALPD[(Alpaca Market Data<br/>free: SIP delayed + IEX real-time)]
    ALPB[(Alpaca Broker<br/>paper → live)]
    MAC[(CBOE / FRED)]
    FUN[(SEC EDGAR + Finnhub)]
    CL[(Claude API<br/>claude-opus-5-5)]
    DB[(Neon Postgres)]
    S3[(S3)]
    TG[Telegram]

    ALPD --> ING
    MAC --> ING
    FUN --> ING
    ANS --> CL
    ANC <--> CL
    PMG <--> CL
    EOD <--> CL
    EXO <--> ALPB
    EXE <--> ALPB
    MON <--> ALPB
    EOD <--> ALPB
    PM1 <--> ALPB
    EVE --> DB
    MORN --> DB
    SCAN --> S3
    GR -. violations .-> TG
    PM1 -. daily report .-> TG
```

**Key architectural choices:**

- **One container image, many entrypoints** (one Lambda per job).
- **The Analyst runs in the evening** via the Claude **Batch API**. **Episodic pivots** (overnight earnings or news gaps) can only be seen in the morning, so they are reviewed in the Portfolio Manager's real-time call.
- **All strategy scans run every evening**; the regime decides what gets *funded*.
- **The regime is computed in code**; the LLM's view (only ever more conservative) comes in the morning call.
- **Postgres is the queue** (`signals`).
- **Entries are placed after the opening print (09:36)**, not before the open. Triggers are the pivot or the opening-range high, so a stock that gaps up and then fades isn't bought at the open.

---

## 3. Daily schedule

All times are US/Eastern (EventBridge Scheduler, `America/New_York`). **Every job starts with the market-calendar guard** (§4).

| Time (ET) | Job | Purpose |
|---|---|---|
| **Evening (session T)** | | |
| 16:20 | `post_market` | Reconcile; journal; equity snapshot; risk multiplier (§8); Telegram daily report |
| 17:00 | `ingest` | Gap-fill bars through T; universe and industries; corporate actions; macro; earnings calendar and surprises; rolling fundamentals |
| 17:15 | `regime` | 5 pillars → state machine → **computed regime** |
| 17:30 | `scan` | Trend Template → **all** strategy scans → features (base count, RS line, up/down volume, momentum rank, ADR) → ranking + cap → charts → baseline. Also updates **position-plan levels** and raises **position events** (climax signals, faltering RS, time stops, earnings cushion) |
| 17:45 | `analyst_submit` | Submit Analyst reviews as one Claude batch |
| 19:00, 21:00, 06:00 | `analyst_collect` | Collect results (idempotent). Not done by 06:00 → no new evening-pipeline entries tomorrow, plus an alert |
| **Trading day (session T+1)** | | |
| 07:30 | `premarket_check` | Macro refresh (computed regime may only **downgrade**); overnight news; earnings-date changes |
| 08:40 | `portfolio_manager` (gap step) | IEX pre-market trading starts at 08:00, so gaps are measured here, not at 07:30. Setups gapping > 5% above their pivot are invalid today. **Episodic-pivot candidates**: gap ≥ 10% on earnings/news |
| 08:45 | `portfolio_manager` (decision step) | **Only if there's something to decide.** LLM regime view, **EP review**, allocation, exit plans for new entries, tightening of existing plans → guardrails → `signals` |
| 09:20 | `execute_open` | Reconcile → OPG exits/trims (stop quantity reduced first, §5.7) → hard-stop replacements |
| 09:36 | `execute_entries` | Reconcile + stop invariant (catches any rejected OPG order) → re-check the actual open vs each pivot → place entry orders with their triggers: pivot, or the **5-minute opening-range high** when the stock opened above the pivot, and for momentum breakouts and EPs. Skip anything more than 5% above its pivot |
| 10:30, 12:30, 14:00 | `intraday_monitor` | Reconcile fills; volume pace; **early-behaviour rules** (§7); raise hard stops when a milestone was hit intraday (breakeven, round-trip); cancel invalid entries; crisis check |
| 15:40 (12:40 on half days) | `eod_review` | Code: soft-rule exits (trailing MA closes, heavy-volume reversals, climax and cushion actions). LLM, **only if needed**: tranche-2 adds that passed the gate, failed breakouts |
| **Other** | | |
| Saturday 10:00 | `weekly_review` | Performance, "your numbers" (average gain/loss, win rate), LLM vs baseline, calibration, **proposed** lessons |
| Every 15 min, 07:00–22:00 on weekdays | `watchdog` | Alert if an expected job heartbeat is missing |

---

## 4. Market calendar and data integrity

**Calendar guard.** EventBridge fires every weekday, so every job begins with:

```python
session = calendar.session_for(today)          # None if the market is closed
if session is None:
    record_job_run(job, status="SKIPPED_MARKET_CLOSED")   # heartbeat still written
    return
```

- **Calendar authority:** Alpaca's calendar API is primary; `pandas_market_calendars` is the cross-check. **If they disagree, no new entries today** (stops still managed), plus an alert.
- **`market_sessions` table:** date, open/close, `is_early_close`, minutes, data status.
- **Guard policy per job type:**

  | Job type | Runs when |
  |---|---|
  | Morning and intraday jobs | Today is a session |
  | Evening jobs (`post_market` → `analyst_submit`) | Today was a session |
  | `analyst_collect` | Any day it's scheduled (idempotent, collects the latest open batch) |
  | `weekly_review` | Saturdays, regardless of sessions |
  | `watchdog` | Checks only heartbeats that were *expected* given that day's calendar |

**Holidays:** no calls or orders; hard stops are GTC at the broker; entry orders are day orders.

**Gap-fill ingest:** fetch every session since the last stored one.

**Early-close days (13:00):** no new entries or tranche-2 adds; EOD review at 12:40; half-day **volume normalized** (× 390 / session minutes).

**Sessions, not calendar days:** every window (22 / 25 / 252 sessions, earnings windows, hysteresis, time stops, 8-week hold = 40 sessions) is counted in sessions.

**Missing data vs. closed market:**

| Situation | Detected by | Action |
|---|---|---|
| Market closed | Not in `market_sessions` | Expected; nothing fetched |
| Vendor data incomplete | Under 90% of expected symbols | Retry, then `INCOMPLETE` → no new entries next session, plus an alert |
| A single symbol has no bar | Halted, suspended or delisted | Excluded from today's watchlist; never forward-filled |

**Macro staleness:** "latest value on or before" each session; more than 2 sessions stale → volatility pillar scores 0.

**Intraday prices:** current prices and opening ranges from **real-time IEX**; volume from **15-minute-delayed SIP**.

**Long weekends:** the Portfolio Manager receives `sessions_until_next_open` and `is_long_weekend`.

---

## 5. Components

### 5.1 Ingest (`ingest`)

- **Universe:** active, tradable US common stocks (NYSE, NASDAQ, AMEX). Price ≥ $10, 50-day average dollar volume ≥ $10M, ≥ 252 sessions of history. This also screens out the thinly traded stocks IBD found perform poorly (under ~400k shares a day).
- **Bars:** Alpaca SIP daily bars, `adjustment=all`; rolling **2 years** in Postgres, full history (2016 onward) in **Parquet**.
- **Validation:** coverage ≥ 90%, OHLC sanity, split re-fetch, idempotent upserts.
- **Industries:** SEC SIC codes → about 100 industry groups (Finnhub profile as fallback).
- **Macro:** CBOE VIX/VIX3M (primary), FRED (fallback), FRED credit spread.
- **Earnings:** Finnhub calendar **and surprises** (actual vs estimate), refreshed daily.
- **Fundamentals:** SEC EDGAR company facts → quarterly EPS and revenue growth and acceleration.
- **News:** Alpaca news API, on demand.

### 5.2 Regime classifier (`regime`)

Five pillars, each **+1, 0 or −1**:

| Pillar | +1 | −1 | Source |
|---|---|---|---|
| **Index trend** | **Both** SPY and QQQ: close > 50 SMA > 200 SMA, 50 SMA rising | **Either** closes < 200 SMA with the 50 SMA falling | Bars |
| **Breadth** | > 60% of the universe above its 50 SMA, and new highs − new lows (10-session average) > 0 | < 35% above the 50 SMA, or new lows > new highs | Full universe |
| **Volatility** | VIX < 20 and VIX/VIX3M < 0.9 | VIX/VIX3M > 1.0 or VIX > 30 | CBOE / FRED |
| **Distribution** | ≤ 3 distribution days in the last 25 sessions | ≥ 6 | SPY / QQQ ([IBD](#18-methodology-sources)) |
| **Leadership health** | ≥ 55% of **all** breakouts from the last 20 sessions are above their trigger 5 sessions later | ≤ 35% | Breakout tracker (§5.12) |

**Leadership health** is how a practitioner notices that "breakouts aren't working" before the indexes show it. Kullamägi trades "fewer or no trades" when setups stop working; Minervini sizes down when he's trading poorly.
- It uses the **ungated** breakout tracker, not the regime-gated baseline book. Otherwise it would stop getting data in exactly the regimes where it's needed, creating a feedback loop.
- It scores 0 with fewer than 10 tracked breakouts in the window.

| Total score | Regime |
|---|---|
| +3 to +5 | `CONFIRMED_UPTREND` |
| 0 to +2 | `UNDER_PRESSURE` |
| ≤ −1 | `CORRECTION` |
| CORRECTION, then a follow-through day | `RALLY_ATTEMPT` |

- **Distribution day** (IBD): an index down ≥ 0.2% on volume higher than the prior session. **The count resets to zero after a follow-through day.**
- **Follow-through day** (IBD): day 4 or later of a rally attempt, an index up ≥ 1.25% on volume higher than the prior session. The best ones come on days 4–7, so later follow-throughs are flagged as weaker. The attempt is cancelled if the index undercuts the rally low.
- **Hysteresis:** downgrades are immediate; upgrades need 3 consecutive sessions. `premarket_check` may only downgrade.
- **State transitions** (a new uptrend always needs a follow-through day, as in IBD):

  | From | To | When |
  |---|---|---|
  | `CONFIRMED_UPTREND` | `UNDER_PRESSURE` | Score ≤ +2 (immediate) |
  | `CONFIRMED_UPTREND` / `UNDER_PRESSURE` | `CORRECTION` | Score ≤ −1 (immediate) |
  | `UNDER_PRESSURE` | `CONFIRMED_UPTREND` | Score ≥ +3 for 3 sessions |
  | `CORRECTION` | `RALLY_ATTEMPT` | Follow-through day (the score alone never lifts CORRECTION) |
  | `RALLY_ATTEMPT` | `CONFIRMED_UPTREND` | Score ≥ +3 for 3 sessions |
  | `RALLY_ATTEMPT` | `CORRECTION` | Index undercuts the rally low |
  | `RALLY_ATTEMPT` | `UNDER_PRESSURE` | 25 sessions without an upgrade or a failure |
- **Crisis override (code):** VIX/VIX3M > 1.0 for ≥ 2 sessions, or SPY −4% in a session → no new entries.
- **LLM view** in the Portfolio Manager call; **effective regime = the more conservative one.**
- Thresholds are tuned in the regime backtest (2016–present).

### 5.3 Scanner (`scan`)

**Trend Template** (Minervini). All 8 rules must pass for `VcpBreakout`, `MomentumBreakout` and `LeaderPullback`.
- `EmergingLeader` needs only rules 1, 5, 6, 7 and 8. Right after a correction, the moving-average stack (rules 2–4) often hasn't re-aligned yet, even for the true new leaders.
- `EpisodicPivot` is exempt; its catalyst replaces the trend.

The 8 rules:

1. Price > 150 SMA and > 200 SMA
2. 150 SMA > 200 SMA
3. 200 SMA rising for at least 22 sessions
4. 50 SMA > 150 SMA and > 200 SMA
5. Price > 50 SMA
6. Price ≥ 30% above the 52-week low
7. Price within 25% of the 52-week high
8. RS rating ≥ 70: the percentile of `0.4·R63 + 0.2·R126 + 0.2·R189 + 0.2·R252`

**Strategy scans (all run every evening):**

| Strategy | Detection | Source |
|---|---|---|
| `VcpBreakout` | 2–4 (up to 6) contractions, each shallower than the last (e.g. 25% → 12% → 6%); volume dries up in the final contraction; pivot = the high of the final contraction | Minervini |
| `MomentumBreakout` | Top ~2% of the universe by 1-, 3- or 6-month return; a prior move of 30–100%+ within the last 1–3 months; a 10–40-session consolidation with higher lows and a tightening range, riding the rising 10/20-day MA | Kullamägi |
| `LeaderPullback` | RS ≥ 80; pullback to within 1 ATR of the 10/21 EMA on declining volume; trigger = the prior session's high | Minervini / O'Neil (pullback buys in leaders) |
| `EmergingLeader` | RS ≥ 85; new 52-week high or RS line at a new high; smallest drawdown during the correction | O'Neil (the first leaders out of a correction) |
| `EpisodicPivot` | Detected **in the morning** (§5.5): a gap of ≥ 10% on earnings or news, heavy volume, strong growth and a beat; best if the stock hasn't rallied over the prior 3–6 months | Kullamägi; post-earnings drift (Bernard & Thomas) |

**Features computed for every candidate** (and shown to the Analyst):

| Feature | Definition | Source |
|---|---|---|
| **Base count** | Bases since the stage-2 start. A new base counts only after a ≥ 20% advance; the count **resets** if a base undercuts the prior base's low; a base-on-base counts as one | IBD |
| **RS line** | Price / SPY; at a new high before or at the breakout = strong, declining = weak | IBD |
| **Up/down volume ratio** | 50-session up-day volume ÷ down-day volume; ≥ 1.0 = accumulation | IBD |
| **Momentum rank** | Percentile of 1-, 3- and 6-month returns | Kullamägi |
| **ADR / ATR** | Average daily range (%) and ATR, which bound stop width | Kullamägi |
| **Industry-group RS rank** | Median RS of the group's members | O'Neil (leaders come from leading groups) |
| **Earnings catalyst** | A recent earnings gap-up, growth and acceleration, surprise | Minervini (SEPA catalyst), PEAD |
| **Extension** | % above the 50-day and 200-day | Minervini / IBD |

**Ranking and cap:** a quality score from RS, momentum rank, group rank, RS-line status, up/down ratio ≥ 1.0, tightness (stop width vs ADR), base count (earlier is better) and catalyst. Only strategies **fundable in the current regime** compete for the **top 40** Analyst slots. Stocks more than 25% above their 50-day are excluded as too extended to buy.

**Verdict cache:** re-review only if the ticker is new, its structure changed, or the verdict is more than 3 sessions old. "Structure" is the base or consolidation (for VCP and momentum, the pivot; for pullbacks, the MA being tested). A pullback's daily-moving trigger (the prior session's high) doesn't trigger a re-review.

**Charts:** daily (1 year, ~800×600, 10/21/50/150/200 MAs, volume, pivot, RS line) and weekly (2 years, ~600×400).

**Position-plan levels and events:** for every open position, compute tomorrow's hard stop, soft-rule levels, climax signals, faltering RS, time stops and earnings-cushion actions → `position_plans` / `position_events`.

### 5.4 Trade Analyst (`analyst_submit` / `analyst_collect`, LLM)

- **Batch, no tools.** Each request has compact facts (the features above), two charts, fundamentals, the earnings date and surprise, up to 5 headlines, the regime, and **the Analyst's own previous verdict and note for this ticker** (continuity across days, the way a trader follows a name while its base forms).
- **Output:** `BUY | WATCH | SKIP`, strategy, pivot, stop, conviction 1–5, up to 3 key risks, reasoning (≤ ~80 words), and a ≤ 20-word **watch note** for next time → `setups`.
  - For **pivot strategies**, `stop` is the structural initial stop.
  - For **opening-range-high strategies** (momentum), `pivot` is the consolidation high and `stop` is the **invalidation level**: below it the setup is void. The actual initial stop is the low of the day, set at entry (§7.1).
- **The shared prefix is cached** across the batch.

### 5.5 Pre-market check (`premarket_check`)

- **07:30:** macro refresh (downgrade-only), overnight news, earnings-date changes.
- **08:40** (first step of `portfolio_manager`, once IEX pre-market quotes exist):
  - pre-market gap vs pivot: above pivot + 5% → invalid today
  - **EP candidates:** stocks that reported after yesterday's close or before today's open, or had major news, gapping ≥ 10%, with charts, the surprise and the latest filed growth numbers. EDGAR lags the earnings release, so the new quarter is represented by the surprise and the headlines.
- Pre-market gaps are estimates (thin IEX pre-market liquidity), so `execute_entries` re-checks against the actual open.

### 5.6 Portfolio Manager (`portfolio_manager`, LLM, real-time)

- **Runs only if** there are fundable setups, EP candidates or position events.
- **Inputs:**
  - account state (equity, buying power, exposure, PDT status, **risk multiplier**, "your numbers")
  - computed regime and pillars
  - fundable setups with flags
  - **EP candidates**
  - **positions with events only** (plus a one-line summary of the rest)
  - sector concentration, long-weekend info, approved lessons
- **Tools:** `get_news`, `get_chart`, `get_fundamentals`, `get_position_history`.
- **Decides:** its regime view (more conservative only); **which EPs to take**; which setups to fund and the risk %; **the exit plan for each entry** (bounded menu, §7); tightening actions.

### 5.7 Execution (`execute_open` / `execute_entries`)

1. **Reconcile.** **Invariant (every job that touches orders):** every position has a broker stop that is **GTC**, covers its **full quantity**, and sits at or above its plan's hard-stop level. Otherwise repair immediately, alert and block entries.
   - OTO child legs may inherit the parent's `day` time-in-force, so after a fill the stop is converted to a standalone GTC stop if needed (verified in spike 0.3b).
2. **Sell sequencing.** A full-quantity stop reserves all the shares, so the broker would reject any other sell order. Every partial sale, trim or exit therefore:
   1. replaces the stop with the remaining quantity (or cancels it for a full exit)
   2. submits the sell
   3. on rejection or no fill, restores the stop at once
3. **09:20 (`execute_open`):** OPG exits and trims (using the sell sequence); hard-stop replacements (only upward, full quantity).
4. **09:36 (`execute_entries`):** compute each entry's trigger and limit (§7.1), size it in code on the **worst fill (the limit price)**, clip by the limits, and submit an **OTO buy-stop-limit** (day) with the stop leg.
5. **Intraday stop raises:** `intraday_monitor` raises hard stops when a milestone (breakeven, round-trip) is reached intraday, never down.
6. **Idempotency:** `idempotency_key` = `client_order_id`.
7. **Adaptive account rules:** if PDT-restricted, entries are limited by the remaining day trades. EP and momentum entries, whose stop is the low of the day, are the most likely to stop out the same day, so they're the first to be withheld when the allowance is low.

### 5.8 Intraday monitor (`intraday_monitor`)

Deterministic. Reconciles fills; computes volume pace (delayed SIP); applies the **early-behaviour rules** (§7); cancels entries invalidated by gaps, earnings, the crisis override or a regime downgrade.

### 5.9 EOD review (`eod_review`)

**Code, always** (IEX real-time price, delayed-SIP volume):
- trailing-MA close exits
- heavy-volume reversal exits
- climax partial sales
- 8-week-hold bookkeeping
- earnings-cushion actions

**LLM, only if needed:**
- **tranche-2 adds** that passed the confirmation gate
- **failed breakouts**: day 1–3 positions back below the pivot on light volume (hold, or exit early)

### 5.10 Post-market and journal (`post_market`)

`trade_journal` (R-multiple, holding sessions, MAE/MFE, tranches, exit reason, regimes, linked decisions); `account_snapshots` (equity, drawdown, **risk multiplier**); Telegram daily report.

### 5.11 Weekly review (`weekly_review`)

- **"Your numbers"** (Minervini's discipline): win rate, average gain, average loss, gain/loss ratio and expectancy, overall and per strategy and regime. Feeds the guardrail that keeps stops small relative to the average gain (§8).
- Comparison with the baseline; conviction calibration; Analyst BUY rate.
- **Proposed lessons** → take effect only after your approval (at most 10 active).

### 5.12 Shadow baseline and breakout tracker

**Shadow baseline book** (the benchmark the LLM book is measured against):
- **Same inputs:** the same capped top-40 list, regime gating, sizing, guardrails, entry policies and default exit plans. EPs come from the same morning candidate list (rule: gap ≥ 10%, volume, surprise > 0).
- **Rules instead of the LLM:** it takes setups in **rank order** until the position and exposure limits are full, and makes tranche-2 adds purely on the gate.
- **Simulated each evening** from that day's SIP minute bars for its symbols only (no longer delayed after the close): 09:36 triggers, opening-range highs, low-of-day stops, the EOD gate and soft rules. It therefore needs no extra intraday jobs.

**Breakout tracker** (feeds the leadership-health pillar):
- Records **every** trigger from the full, uncapped scan, regardless of regime or caps, and its result 5 sessions later.
- Because it's ungated, it keeps measuring whether breakouts work even in `CORRECTION`, which is when a recovery has to be spotted.

### 5.13 Observability and safety

- **Kill switch** (`TRADING_ENABLED`).
- **Watchdog** heartbeats.
- **Telegram alerts:** failures, guardrail events, halts, mismatches, refusals, incomplete data, calendar disagreements.
- **Logs:** structlog → CloudWatch; LLM payloads → S3.

---

## 6. Market regimes and strategies

| Regime | Fundable strategies | Base risk / trade | Max exposure | Max positions\* |
|---|---|---|---|---|
| `CONFIRMED_UPTREND` | `VcpBreakout`, `MomentumBreakout`, `EpisodicPivot` | 1.0% | 100% | 6 (8 if equity ≥ $100k) |
| `UNDER_PRESSURE` | `LeaderPullback`, highest-conviction `VcpBreakout`, `EpisodicPivot` (conviction ≥ 4) | 0.5% | 50% | 4 |
| `RALLY_ATTEMPT` | `EmergingLeader`, `EpisodicPivot` (starter positions) | 0.25–0.5% | 25% → 50% | 3 |
| `CORRECTION` | None (cash) | — | Existing positions only | — |
| Crisis override | No new entries | — | — | — |
| *Later phase* | `FailedBreakoutShort`, parabolic short | 0.5% | 25% | 2–3 |

\* Minervini holds about 4–8 positions in smaller portfolios. Base risk is multiplied by the **progressive-exposure multiplier** (§8).

Regime changes never force liquidation. A downgrade tightens existing plans (code) and raises a position event for each one.

**Strategy registry:** each strategy implements `scan()`, `build_llm_context()`, `system_prompt`, `entry_policy`, `exit_policy` and `default_risk`. `config/risk.yaml` maps each regime to its strategies, risk, entry rules and exit-plan menus.

---

## 7. Entries, position plans and exits

### 7.1 Entries

| Strategy | Trigger (from 09:36) | Initial hard stop | Tranches |
|---|---|---|---|
| `VcpBreakout` | Pivot. If the stock opened above the pivot (by ≤ 5%), the 5-min opening-range high instead | Below the final contraction's low; ≤ 8% below the pivot | 2 |
| `LeaderPullback` | Prior session's high | Below the pullback low / MA; ≤ 1.5 × ATR | 2 |
| `EmergingLeader` | Pivot / new-high level | Structural; ≤ 8% | 2 (starter) |
| `MomentumBreakout` | max(consolidation high, 5-min opening-range high) | Low of the day at 09:36; ≤ 1 × ADR below the trigger | 1 |
| `EpisodicPivot` | 5-min opening-range high on the gap day | Low of the day at 09:36; ≤ 1 × ADR below the trigger | 1 |

**Limit prices (they define the worst fill used for sizing):**
- Pivot strategies: limit = pivot × 1.05 (the max chase).
- Opening-range-high strategies: limit = trigger × (1 + min(2%, ½ × ADR%)).

Stop width is measured from the pivot/trigger; risk is sized on the limit, so slippage up to the limit can't exceed the planned risk.

- Kullamägi enters momentum breakouts and EPs at the opening-range high with the stop at the low of the day, never wider than the ADR. Minervini buys VCPs at the pivot with the stop under the last contraction.
- **Max chase:** no entry more than 5% above a pivot. Minervini doesn't buy stocks more than 25% above their 50-day. EPs are exempt from the 50-day rule, since their gap is the signal.
- **Exposure is reserved for the full planned position** when tranche 1 is placed, so tranche 2 can never be blocked by, or push past, the exposure and position limits.
- **Two-tranche entries** (Minervini's "pilot buy, then add on confirmation"):
  - tranche 1 = 50% at the trigger
  - tranche 2 = 50% at the EOD review on days 1–3, but only if the **confirmation gate** passes: price between the pivot and pivot × 1.05, in the upper half of the day's range, projected volume ≥ 1.4× the 50-day average (IBD looks for ≥ 40–50% above average), RS line at or near a high
  - tranche 2 is re-sized on the actual price so total risk ≤ plan, and the hard stop is replaced to cover the full quantity

### 7.2 Early-behaviour rules (first 10 sessions)

| Situation | Action | Why |
|---|---|---|
| Hard stop hit | Out (broker) | Minervini: "When the stock price hits my stop, I'm out — period." |
| **Heavy-volume reversal**: closes below the pivot on ≥ 1.5× average volume, the heaviest down volume since the breakout | Exit (code, EOD) | The breakout has been rejected by sellers |
| Closes below the pivot on **light** volume | Hold; no tranche 2; LLM may exit early | IBD: nearly half of breakouts that became 50%+ winners dipped to or below the buy point first, so a light-volume dip alone isn't a sell |
| No progress: high < +1R by session 10 | Halve | Leaders tend to work quickly; dead money ties up risk |
| No progress by session 20 | Exit | Same reason |

### 7.3 Position plan (written at entry; a bounded menu; defaults chosen by backtest)

| Component | Menu options | Source |
|---|---|---|
| **Strength partial** | (a) sell ⅓–½ after 3–5 sessions if ≥ +1.5R; (b) sell ¼–½ at +20–25%; (c) sell ⅓–½ at +2–3R; (d) none | (a) Kullamägi, (b) O'Neil, (c) Minervini |
| **Breakeven** | Stop → breakeven after the first partial **or** at +3R, whichever comes first | Minervini |
| **Round-trip rule** (default on; tested in backtest) | Once a gain has reached +10%, the stop is at least breakeven | IBD: don't let a double-digit gain become a loss |
| **8-week hold** (chosen at entry: on/off) | If +20% within 15 sessions of the breakout: suspend profit-target partials and replace the trail with the 50-day for 40 sessions from the breakout; exit only on the hard stop (≥ breakeven), a close below the 50-day, or climax signals | IBD |
| **Trail** | First close below the 10-day / 21 EMA / 50 SMA | Kullamägi (10/20-day), Minervini / O'Neil (50-day) |
| **Climax sale** | ≥ 2 signals → sell ½; ≥ 3 → sell all. Signals: largest daily gain since the move began; exhaustion gap; heaviest-volume up day since the breakout; ≥ 7 up days in 8 or ≥ 8 in 10; ≥ 70% above the 200-day; ≥ 25% gain in ≤ 15 sessions after an extended run | IBD |
| **Late-stage base** | New entries: base count 3 → risk × 0.5; ≥ 4 → not fundable. Held positions: a breakout from a 4th-stage base → sell ½ into strength | O'Neil: about 80% of fourth-stage bases fail |
| **Faltering RS** | RS line drops to a 50-session low → position event (Portfolio Manager review) | IBD |
| **Time stop** | §7.2 | — |

**Execution split:**
- **Hard stop** at the broker (GTC, full quantity): initial → breakeven → round-trip level.
- **Soft rules** carried out by code at the EOD review.

**Tighten-only:** later decisions may raise stops, choose a tighter trail, sell earlier or exit. Any loosening is rejected.
- **Transitions committed at entry are not loosening.** The 8-week hold widening the trail to the 50-day is allowed only because the plan opted into it at entry. Neither the LLM nor anything else can add it later.

**Rule precedence** (when several fire on the same day, the first applicable wins; sales add up but never exceed the position):
1. Hard stop
2. Earnings cushion
3. Early-behaviour exits
4. Climax sales
5. Regime-downgrade tightening
6. 8-week-hold constraints
7. Strength partials
8. Trailing-MA exit
9. Time stop

### 7.4 Earnings cushion (open positions)

At the EOD review of the last session before a report. For a **before-open** report, that's the prior session. For an **after-close** report, it's the report day itself. `earnings_events` stores the timing.

| Unrealized gain | Action |
|---|---|
| < 1R | Exit |
| 1R to 3R | Trim to half; stop at least breakeven |
| ≥ 3R | May hold full; stop at least breakeven |

---

## 8. Risk management and guardrails

Guardrails are pure Python functions (`auratrade/guardrails/`) with 100% branch coverage. They run on **every** LLM action. Violations are rejected or clipped, logged to `guardrail_events`, and alerted.

**Progressive-exposure multiplier** (Minervini: smaller when trading poorly, larger when trading well; start with pilot positions):

| State | Multiplier |
|---|---|
| **Start**, or after any halt is reset ("prove it first") | 0.5 until 2 winning trades |
| Normal | 1.0 |
| **Trading well**: equity at a new high and the last 10 trades have positive expectancy | 1.25 |
| 3 consecutive losses, or drawdown ≥ 5% from peak | 0.5 |
| Drawdown ≥ 10% from peak | 0.25 |
| Return to normal | After 2 consecutive winners or a new equity high |

Effective risk = regime base risk × multiplier × late-stage factor, **hard cap 1.5%**. For reference, Minervini caps risk at 1.25–2.5% of equity per trade and Kullamägi usually risks 0.25–1%.

| Guardrail | Default | Behaviour |
|---|---|---|
| Max risk per trade | Effective risk, **cap 1.5%** | Clip |
| Max position size | 25% of equity (Minervini ~25%; Kullamägi never > 30% overnight) | Clip |
| Max total exposure / positions | Regime-dependent (§6); a two-tranche entry reserves its **full planned size** | Reject |
| Max per industry group | 2 positions | Reject |
| Stop on every entry | Always | Reject |
| Max stop width | VCP / emerging: ≤ 8% below the pivot; pullback: ≤ 1.5 × ATR; momentum / EP: ≤ 1 × ADR | Reject |
| **Keep losses small vs gains** | **Per strategy**, after ≥ 20 closed trades of that strategy: initial stop ≤ ½ × that strategy's rolling average gain (%), never tighter than its default rule | Reject |
| Max chase | ≤ 5% above the pivot; never > 25% above the 50-day (EPs exempt) | Reject |
| Base count | 3 → risk × 0.5; ≥ 4 → reject | Clip / Reject |
| Hard stop: GTC, full quantity, ≥ plan level | Invariant check in every order-touching job | Auto-repair + alert |
| Sell sequencing | Stop reduced before any sell; restored on failure | Enforced in the order layer |
| Plans are tighten-only | Including the round-trip rule | Reject |
| Tranche 2 | Only via the gate, ≤ 3 sessions after tranche 1, total risk ≤ plan | Reject |
| Earnings blackout (new entries, except EPs) | The **next** report is within 5 sessions | Reject |
| Earnings cushion | §7.4 | Code |
| Early-close days | No new entries or adds | Reject |
| Tradable symbols | Today's setups / approved EPs (entries); open positions (exits) | Reject |
| Order types | Entries always limit-protected | Reject |
| PDT allowance | If restricted: entries limited by remaining day trades | Reject |
| Daily / weekly loss halt | −2% / −5% | No new entries |
| Drawdown halt | −15% from peak | All trading stops; manual re-enable |
| Crisis override | VIX backwardation for 2+ sessions or SPY −4% | No new entries |
| Regime ceiling | LLM view ≤ computed regime | Use the more conservative one |
| Data integrity | `INCOMPLETE` session or calendar disagreement | No new entries |
| LLM failure | Error, timeout, refusal or invalid schema | No action (fail closed) |

---

## 9. LLM design and token budget

| Role | Model | Mode | Effort | Calls / day |
|---|---|---|---|---|
| Trade Analyst | `claude-opus-5-5` | **Batch** (evening), shared prefix cached | `medium` or `high`, chosen by test in Phase 4 | ≤ 40 (fewer with the verdict cache) |
| Portfolio Manager (+ regime view + EP review) | `claude-opus-5-5` | Real-time, with tools | `high` | 0–1 |
| EOD review | `claude-opus-5-5` | Real-time | `high` | 0–1 |
| Weekly review | `claude-opus-5-5` | Real-time | `high` | 1 per week |

**Token-saving measures:**

| Measure | Effect |
|---|---|
| Batch API for the Analyst | −50% on the largest cost |
| Prompt caching inside the batch | Shared input billed at about 10% |
| Verdict cache | Unchanged setups aren't re-reviewed |
| Skip logic | No real-time calls on quiet days |
| No separate regime call | Folded into the Portfolio Manager |
| Event-driven position context | Only positions with events are sent in full |
| Code carries out exit plans | No LLM needed for routine exits |
| Output caps | Reasoning ≤ ~80 words, ≤ 3 risks, watch note ≤ 20 words |
| Compact facts, small charts | Image tokens ≈ w × h / 750 |
| *Optional, not adopted:* two-stage Analyst | Revisit only if costs exceed budget |

**Expected LLM cost: about $12–20/month** (EP reviews add a little during earnings season). Measured in Phase 0 and tracked daily.

**API usage:**
- structured outputs into Pydantic models
- chart PNGs as image blocks
- `strict: true` tools with `tool_choice: auto`
- check `stop_reason`; a refusal → no action
- server-side fallbacks enabled
- effort set explicitly per role (Opus 5.5 always thinks; its default effort is `medium`)
- versioned prompts in `prompt_versions`

**Output schemas (abridged):**

```jsonc
// Trade Analyst → setups
{ "decision": "BUY" | "WATCH" | "SKIP", "strategy": "VCP_BREAKOUT",
  "pivot": 142.50, "stop": 134.80, "conviction": 4,
  "key_risks": ["3rd-stage base"], "reasoning": "≤ 80 words", "watch_note": "≤ 20 words" }

// Portfolio Manager → guardrails → signals
{ "regime_view": { "label": "UNDER_PRESSURE", "reasoning": "..." },
  "entries": [{ "setup_id": "…" | null, "ep_symbol": "…" | null, "risk_pct": 1.0,
                "plan": { "thesis": "...", "partial": "KULLAMAGI_3_5D", "trail": "10D",
                          "time_stop": true } }],
  "position_actions": [{ "symbol": "ANET", "action": "TIGHTEN" | "TRIM" | "EXIT",
                         "changes": { "trail": "10D" }, "trim_pct": null, "reasoning": "..." }] }
```

---

## 10. Data model

PostgreSQL on Neon. SQLAlchemy 2.0, Alembic.

| Table | Purpose |
|---|---|
| `tickers` | Universe metadata: int `id`, symbol, exchange, type, SIC, industry group, active, delisted_at |
| `industry_groups` | Group, SIC ranges, daily group RS rank |
| `market_sessions` | Calendar: open/close, early close, minutes, data status |
| `daily_bars` | Adjusted OHLCV `(ticker_id, date)`, rolling 2 years |
| `macro_series` | VIX, VIX3M, credit spread, with `fetched_at` |
| `fundamentals` | Quarterly EPS and revenue, growth and acceleration |
| `earnings_events` | Date, timing, actual vs estimate (surprise), source |
| `bases` | Detected bases per symbol: start/end, depth, contractions, pivot, **stage number** |
| `market_regime` | Per session: 5 pillars, features, computed / LLM / effective regime, crisis flag |
| `daily_watchlist` | Scanner output: strategy, rank and features (JSONB), chart keys |
| `setups` | Analyst verdicts, `valid_until`, **watch note** |
| `ep_candidates` | Morning gap list: gap %, catalyst, surprise, decision |
| `llm_batches`, `prompt_versions`, `llm_decisions` | LLM bookkeeping |
| `signals` | **The queue**: idempotency_key, action, strategy, trigger type, tranche, prices, risk %, shares, status |
| `guardrail_events` | Rule, proposed vs enforced value |
| `orders`, `positions` | Broker mirror; strategy tag; regime at entry |
| `position_plans` | Versioned, tighten-only plan per position, with current levels and the 8-week-hold state |
| `position_events` | Milestones, climax signals, faltering RS, time stops, cushion, flags |
| `trade_journal` | One row per trade: R-multiple, MAE/MFE, tranches, exit reason, regimes, links |
| `baseline_signals` | Shadow book trades |
| `breakout_tracker` | Every scan trigger (ungated) and its 5-session result → leadership health |
| `account_snapshots` | Equity, drawdown, **risk multiplier**, rolling "your numbers" |
| `lessons` | Proposed / approved / retired, with evidence |
| `job_runs`, `system_flags` | Heartbeats; kill switch and halts |

**Retention (to stay within the 0.5 GB free tier):** `daily_bars` 2 years; `daily_watchlist`, `breakout_tracker` and `llm_decisions` output 90 days in Postgres, then archived to Parquet in S3. Journal, plans, signals, orders, regime and lessons are kept forever (small). Size is checked in the daily report, with an alert at 70%.

---

## 11. Tech stack

| Layer | Choice |
|---|---|
| Language / packaging | Python 3.13, **uv** |
| Quality | ruff, mypy, pytest, hypothesis, recorded HTTP fixtures |
| Data processing | pandas, numpy, pyarrow |
| Charts | mplfinance |
| Calendar | Alpaca calendar API + pandas_market_calendars |
| Market data | **Alpaca free tier**: SIP historical (15-min delay), IEX real-time quotes/bars, news |
| Macro data | CBOE, FRED |
| Fundamentals / earnings / industries | SEC EDGAR, Finnhub free tier |
| Broker | **Alpaca** (`alpaca-py`): paper → live |
| LLM | Anthropic Python SDK, `claude-opus-5-5`, Batch API, prompt caching |
| Validation / config | Pydantic v2, pydantic-settings, YAML |
| Database | **Neon Postgres (free tier)**, SQLAlchemy 2.0, psycopg 3, Alembic |
| HTTP / logging | httpx, tenacity; structlog → CloudWatch |
| Alerts | **Telegram bot** |
| Compute / scheduling | AWS Lambda (container image) + EventBridge Scheduler |
| Secrets / storage | SSM Parameter Store; S3 |
| Infrastructure as code / CI/CD | AWS CDK (Python); GitHub Actions + OIDC |

---

## 12. Infrastructure and cost

- **Region** `us-east-1`. One ECR image; one Lambda per job. Neon over TLS, so no VPC or NAT.
- **Least-privilege IAM** per function; secrets in SSM. Environments: `dev`, `paper`, later `live`.
- **AWS budget alarms** at $10 and $25.

| Item | Monthly estimate |
|---|---|
| Claude API | ~$12–20 |
| AWS | ~$1–5 |
| Data, database, alerts | $0 (free tiers) |
| **Total** | **~$15–25** (budget: < $150) |

---

## 13. Repository layout

```text
AuraTrade/
├── README.md
├── docs/                         # execution plan, spikes, backtests, incidents
├── pyproject.toml / uv.lock
├── Dockerfile
├── docker-compose.yml
├── alembic/
├── config/risk.yaml              # regimes, strategies, entry rules, exit menus, guardrails, multiplier
├── src/auratrade/
│   ├── config.py
│   ├── calendar/
│   ├── db/
│   ├── data/                     # alpaca_data, macro, edgar, finnhub, news
│   ├── indicators/               # MAs, ATR/ADR, RS + RS line, group RS, U/D volume, momentum rank,
│   │                             # contractions, bases + base count, climax signals, volume pace, breadth
│   ├── regime/
│   ├── strategies/               # vcp_breakout, momentum_breakout, leader_pullback,
│   │                             # emerging_leader, episodic_pivot
│   ├── plans/                    # plans, early-behaviour rules, soft rules, 8-week hold, cushion, events
│   ├── llm/
│   ├── guardrails/
│   ├── execution/                # account, sizing, triggers (pivot / opening-range high), tranches, orders, reconciliation
│   ├── journal/                  # journal, your-numbers, baseline, multiplier, reports, lessons
│   ├── storage/
│   ├── notify/
│   └── jobs/
├── backtest/                     # regime, baseline, entry and exit-policy comparisons
├── spikes/
├── infra/
└── tests/
```

---

## 14. Configuration

- **`TRADING_MODE`:** `dry_run` | `paper` | `live` (live needs an explicit flag and separate credentials).
- **`TRADING_ENABLED`:** runtime kill switch.
- **Models / effort:** `ANALYST_MODEL`, `PM_MODEL`, `*_EFFORT`; `ANALYST_MAX_CANDIDATES=40`.
- **`config/risk.yaml`:** regime thresholds, fundable strategies, entry rules, exit-plan menus and defaults, guardrails, multiplier rules, early-behaviour rules, the cushion table. Its hash is logged with every run.
- **Paper account:** set the Alpaca paper equity to the intended live capital.

---

## 15. Validation and go-live criteria

The paper trial runs until **the later of 3 months or 30 completed trades**. 30 trades can't statistically prove an edge, so go-live judges **process quality and relative performance**, and evidence keeps accumulating during small live trading.

Required before going live:

- **Operations:** every job ran or was correctly skipped; no position was ever without a full-quantity broker stop; zero duplicate orders; zero unresolved mismatches.
- **Guardrails:** none bypassed; every intervention logged and alerted.
- **Relative performance:** the LLM book is **not worse than the shadow baseline** on expectancy and max drawdown.
- **"Your numbers":** average loss well below average gain (target gain/loss ≥ 2); no single loss beyond 2R except documented gaps.
- **Calibration:** higher conviction does not perform worse than lower conviction.
- **Human review:** a sample of decisions and reasoning has been read and judged sound.

Go-live starts at the 0.5 multiplier ("prove it first") with reduced capital.

---

## 16. Decision log

| # | Decision | Choice |
|---|---|---|
| 1 | Decision-maker | LLM inside code-enforced guardrails (a deterministic-first approach was rejected) |
| 2 | Regime layer | Score + FTD state machine; LLM view only more conservative |
| 3 | LLM | `claude-opus-5-5` everywhere; Analyst ≤ 40/day via batch |
| 4 | Analyst timing | Evening batch; EPs reviewed in the morning |
| 5 | Budget | < $150/month; expected ~$15–25 |
| 6 | Market data | Alpaca free tier (delayed SIP + real-time IEX) |
| 7 | Broker | Alpaca, paper first |
| 8 | Database | Neon free; rolling 2 years of bars + Parquet |
| 9 | Compute | Lambda + EventBridge Scheduler |
| 10 | Queue | Postgres `signals` |
| 11 | Alerts | Telegram |
| 12 | Account rules | Adaptive from the Alpaca account (PDT, margin, shorting) |
| 13 | Early-close days | No new entries or adds |
| 14 | Max chase | 5% above pivot; never > 25% above the 50-day |
| 15 | Universe | Price ≥ $10, 50-day avg dollar volume ≥ $10M |
| 16 | Exits | No fixed take-profit; entry-time plans carried out by code, tighten-only |
| 17 | Shorting | Later phase |
| 18 | Secrets | SSM Parameter Store |
| 19 | Stop width | Per strategy: ≤ 8% below pivot (VCP), ≤ 1.5 ATR (pullback), ≤ 1 ADR (momentum / EP) |
| 20 | Entries | Two tranches for VCP / pullback / emerging; one for momentum / EP |
| 21 | Stops | Hard broker stop + soft close-based rules at the EOD review |
| 22 | Intraday data | IEX real-time for price and opening range; delayed SIP for volume |
| 23 | Earnings | Blackout for new non-EP entries + cushion table for open positions |
| 24 | Risk scaling | Progressive exposure: 0.25 / 0.5 / 1.0 / 1.25, start at 0.5 |
| 25 | Industries | SEC SIC → groups; group RS in ranking; max 2 per group |
| 26 | Regime LLM view | Folded into the Portfolio Manager call; all scans run every evening |
| 27 | Token policy | Skip, event-driven, caps, caching |
| 28 | Lessons | LLM proposes, human approves; max 10 active |
| 29 | Baseline | Same everything except LLM choices (leadership health uses the separate tracker, #46) |
| 30 | Go-live bar | Later of 3 months / 30 trades; process + baseline + numbers |
| 31 | Idle cash | Cash in v1 |
| 32 | New strategies | `MomentumBreakout` and `EpisodicPivot` (Kullamägi), EP backed by post-earnings drift research |
| 33 | Entry timing | Orders placed at 09:36; opening-range-high triggers for gap opens, momentum and EPs |
| 34 | Early behaviour | Heavy-volume reversal → exit; light-volume dip below pivot → hold (IBD evidence); time stops at 10/20 sessions |
| 35 | Exit menu | Kullamägi / O'Neil / Minervini partial options; defaults chosen by backtest per strategy |
| 36 | Breakeven | After the first partial or at +3R; round-trip rule at +10% always on |
| 37 | 8-week hold | IBD rule for +20% within 15 sessions |
| 38 | Climax sales | IBD climax signals; ≥ 2 → sell ½, ≥ 3 → sell all |
| 39 | Base count | IBD counting; 3rd stage → half risk, 4th+ → not fundable |
| 40 | New features | RS line, up/down volume ratio, momentum rank, ADR, extension |
| 41 | Regime | 5th pillar "leadership health" from the baseline book; distribution count resets after an FTD |
| 42 | Keep losses small | After 20 trades: initial stop ≤ ½ × rolling average gain |
| 43 | Analyst continuity | Previous verdict + watch note included in the next review |
| 44 | Gap timing | Pre-market gap and EP checks at 08:40 (IEX pre-market opens 08:00), re-checked at the open |
| 45 | Order mechanics | Sell sequencing (stop reduced first); stop invariant = GTC + full quantity + plan level, in every order job |
| 46 | Leadership health | Ungated breakout tracker, separate from the gated baseline book |
| 47 | Regime transitions | Explicit table; a new uptrend always needs a follow-through day |
| 48 | Rule precedence | Fixed order; plan transitions committed at entry are not loosening |
| 49 | Baseline mechanics | Same top-40 list; rank-order fill; simulated each evening from minute bars |
| 50 | Retention | 90-day hot window for large tables; archive to Parquet |

---

## 17. Known limitations

- **LLM decisions can't be backtested** without hindsight bias; forward paper trading is the honest test, and 30 trades is a weak sample.
- **The traders this design follows are outliers.** Their published rules are necessary but not sufficient; their results came with discretion and experience the system only approximates.
- **LLM output is non-deterministic.** Tighten-only plans, logging and guardrails contain it.
- **Paper fills are optimistic.**
- **Free-tier data limits:** IEX-only opening ranges and pre-market gaps are approximations of the consolidated tape; intraday volume is 15 minutes delayed; delisted coverage is limited; history starts in 2016; EP backtests use a gap + volume proxy for "news"; EDGAR fundamentals lag earnings releases; IBD measures distribution and follow-through on index exchange volume, approximated here with SPY/QQQ volume.
- **Gap risk:** stops don't protect against overnight gaps.
- **Regime lag:** trend-based detection reacts after the turn.
- **Breakout strategies** expect roughly a 40–50% win rate; returns depend on a few large winners.
- **Vendor outages** fail closed: no new entries; hard stops stay.

---

## 18. Methodology sources

Primary sources are preferred: the traders' own writing or interviews, IBD's own educational articles (syndicated on Yahoo Finance), and peer-reviewed research. Book-derived numbers taken from summaries are marked † and should be checked against the books.

**Kristjan Kullamägi**
- [My 3 timeless setups that have made me tens of millions](https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/) (own blog): breakout and EP criteria, opening-range-high entries, low-of-day stops ≤ ADR, sell ⅓–½ after 3–5 days, breakeven, 10/20-day trail, 0.25–1% risk, 10–20% positions, ≤ 30% overnight per stock, fewer trades in bad markets.

**Mark Minervini**
- *Trade Like a Stock Market Wizard* (2013) and *Think & Trade Like a Champion* (2017): Trend Template, VCP, SEPA, sell rules†, position sizing†.
- [Minervini on progressive exposure and partial positions](https://x.com/markminervini/status/1318562658052984834) (own post).
- [MarketWatch interview (expanded), Michael Sincere](https://michaelsincere.com/articles/my-marketwatch-interview-with-stock-market-wizard-mark-minervini): uptrends only, small single-digit losses, protecting profits.
- † [Shortform summary of *Think & Trade Like a Champion*](https://www.shortform.com/pdf/think-trade-like-a-champion-pdf-mark-minervini): risk 1.25–2.5% per trade, breakeven at ~3× risk, 4–8 positions in smaller accounts.

**William O'Neil / Investor's Business Daily**
- [What factors help determine if a breakout works or fails?](https://finance.yahoo.com/news/factors-help-determine-breakout-works-220900693.html): breakout volume, thin stocks underperform, nearly half of 50%+ winners dipped to or below the buy point.
- [Know a stock's base count](https://finance.yahoo.com/news/know-stocks-count-market-gets-215200480.html) and [Why late-stage base patterns often lead to a timely exit](https://finance.yahoo.com/news/why-stage-patterns-often-lead-215800864.html): base counting rules; late-stage failure rates.
- [Beware of climax-run signals](https://finance.yahoo.com/news/beware-climax-run-signals-long-225300520.html): climax-top signals.
- [Know when to invoke the 8-week hold rule](https://finance.yahoo.com/news/know-invoke-8-week-hold-215800238.html): 20% in 3 weeks → hold 8 weeks; round-trip rule.
- [Use up/down volume ratio to gauge demand](https://finance.yahoo.com/news/down-volume-ratio-gauge-demand-212800062.html): 50-day up/down volume ≥ 1.0.
- [Use the RS line to identify emerging leadership](https://finance.yahoo.com/news/relative-strength-line-identify-emerging-210700981.html): RS line new highs before or at the breakout.
- [Watch for distribution days](https://finance.yahoo.com/news/watch-distribution-days-spot-peaks-220700524.html) and [Follow-through day](https://finance.yahoo.com/news/day-tells-time-buy-stocks-215900253.html): market-timing rules.

**Academic research**
- George & Hwang (2004), "The 52-Week High and Momentum Investing", *Journal of Finance* ([summary](https://www.cxoadvisory.com/1284/technical-trading/the-52-week-high-as-a-momentum-indicator-for-individual-stocks/)): nearness to the 52-week high predicts returns. Supports Trend Template rule 7 and new-high breakouts.
- Bernard & Thomas (1989), "Post-Earnings-Announcement Drift", *Journal of Accounting Research* ([overview](https://en.wikipedia.org/wiki/Post%E2%80%93earnings-announcement_drift)): prices under-react to earnings news. Supports `EpisodicPivot` and the catalyst feature.
