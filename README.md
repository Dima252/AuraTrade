# AuraTrade

An autonomous **momentum book** for US equities. Once a month it ranks the 1,000 most liquid US stocks by their 12-1 month return (the past year, skipping the last month) and holds 8 of the strongest, each with a protective stop at the broker. Cross-sectional momentum is the best-documented anomaly in the research, and the rules follow that research and the practitioners who trade it (Jegadeesh & Titman, Asness et al., Gray & Vogel, Clenow). A **market-regime layer** blocks new buys in corrections. The sources behind each rule are listed in [§18](#18-methodology-sources).

An LLM (Claude) makes the trading and capital-allocation decisions. Python computes the ranking and every price level, carries out each position's rule exits, and enforces hard risk limits the LLM cannot override. The system runs serverless on AWS, on a budget of **under $150/month** (about $6–15/month expected, §12).

> **Strategy change (#146-#149, 2026-10-25):** the chart-pattern setups (Minervini's VCP, O'Neil's CAN SLIM, Kullamägi's breakouts and episodic pivots) failed three tests, and the system became the momentum book this README describes ([docs/strategy-change-momentum.md](docs/strategy-change-momentum.md), [docs/momentum-plan.md](docs/momentum-plan.md)). The setup system's spec is archived in [docs/archive/README-setup-system.md](docs/archive/README-setup-system.md); its code stays in the repo, unwired, and its scans now serve as facts for the LLM.
>
> **Status:** the momentum book's rules and its backtest pre-registration ([docs/backtests/momentum-preregistration.md](docs/backtests/momentum-preregistration.md)) are approved; the backtest is being built. Sections that describe components not built yet are the spec they will be built to. The step-by-step build plan and progress tracker are in [docs/execution-plan.md](docs/execution-plan.md); the builder's working rules are in [CLAUDE.md](CLAUDE.md).
>
> **Disclaimer:** this is experimental software that will trade real money once live. Nothing here is financial advice. It runs on an Alpaca **paper** account first. Never risk capital you cannot afford to lose.

---

## Contents

1. [Design principles](#1-design-principles)
2. [System architecture](#2-system-architecture)
3. [Daily schedule](#3-daily-schedule)
4. [Market calendar and data integrity](#4-market-calendar-and-data-integrity)
5. [Components](#5-components)
6. [Market regime overlay](#6-market-regime-overlay)
7. [Entries, stops and exits](#7-entries-stops-and-exits)
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

1. **Follow the evidence, then the practitioners.** The strategy is cross-sectional momentum, the best-documented anomaly in the research (§18), run with the discipline of the practitioners who trade it (Gray & Vogel, Clenow). Every rule has a source or is a guardrail that keeps automation safe. Where sources disagree, a pre-registered backtest chooses, and the default stays unless it is clearly beaten (§15).
2. **The LLM decides; code enforces the envelope.** Claude chooses which ranked names the book holds, how large, and when to step aside early. Code rejects or clips anything outside hard limits.
3. **Python computes facts; the LLM interprets them.** The ranking, share counts and **every price level** are computed in code. The LLM may raise a stop; it never supplies a price (§8, "Price levels").
4. **Each position follows its plan, and changes can only tighten it.** The plan (hold while in the top decile, the catastrophe stop) is set at entry and carried out by code. Later LLM decisions may only make it more conservative: exit earlier, raise the stop.
5. **Hold the strongest and let them run.** A holding stays while it remains in the top decile of the ranking. It leaves at a rebalance once it drops out, or at its stop. There are no profit targets and no trims.
6. **Start small.** Half slots at the start of the paper trial and of live trading.
7. **The broker is the source of truth; fail closed for entries, fail safe for exits.** Hard stops always rest at the broker. No halt, kill switch or error path ever cancels a protective stop or blocks a rule exit.
8. **Everything is logged.** LLM decisions can't be honestly backtested, so the log is the validation record.
9. **Only spend tokens when a decision is needed:** one review a month, plus the sessions with position events.

---

## 2. System architecture

```mermaid
flowchart TD
    subgraph EVE["Evening pipeline (after close, session T)"]
        PM1[post_market<br/>reconcile · journal · report]
        ING[ingest<br/>bars · macro · earnings · fundamentals · industries]
        SIM[simulate<br/>rule book · breakout tracker · scorecard]
        REG[regime<br/>5 pillars → computed regime]
        RNK[rank<br/>momentum ranking · hold zone · events<br/>on ranking sessions: the candidate list, facts, charts]
        BRS[book_review_submit<br/>Batch API, ranking sessions only]
        BRC[book_review_collect]
        PM1 --> ING --> SIM --> REG --> RNK --> BRS --> BRC
    end

    subgraph DAY["Trading day (session T+1)"]
        PRE[premarket_check 07:30<br/>macro · news · earnings]
        PMG[portfolio_manager 08:45<br/>execution sessions and event days<br/>→ LLM: picks · sizes · early exits · regime view]
        GR{{guardrails}}
        EXO[execute_open 09:20 + 09:31 check<br/>split stops · stop raises · unfilled sales]
        EXC[execute_close 15:40<br/>rule sells · LLM exits · buys with GTC stop legs]
        SG[stop_guard every 5 min]
        PRE --> PMG --> GR --> EXO --> EXC
        EXO --- SG
    end

    BRC --> PRE

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
    BRS --> CL
    BRC <--> CL
    PMG <--> CL
    EXO <--> ALPB
    EXC <--> ALPB
    SG <--> ALPB
    PM1 <--> ALPB
    EVE --> DB
    DAY --> DB
    RNK --> S3
    GR -. violations .-> TG
    PM1 -. daily report .-> TG
```

**Key architectural choices:**

- **One container image, many entrypoints** (one Lambda per job).
- **Monthly decisions, daily protection.** The book is ranked every evening, but buys and rule sells happen only on the **execution session E**: the first session of each month, after the **ranking session R** (the last session of the month). Between rebalances only the stops, position events and the LLM's early exits act.
- **The 12-1 signal skips the last month,** so the list is known the evening before and needs no intraday data. The Book Analyst reviews it overnight through the **Batch API**. The Portfolio Manager decides in real time the next morning, and the trades go in at the 15:40 decision price.
- **The regime is computed in code**; the LLM's view (only ever more conservative) comes in the morning call. It decides whether the book may buy, never whether it must sell.
- **Postgres is the queue** (`signals`).
- **The evening pipeline is chained, not timed.** Each evening job starts only after its predecessor succeeded for the same session (checked in `job_runs`), and on success it invokes the next one. The scheduled times in §3 are fallback starts. A job whose predecessor hasn't succeeded waits and retries up to 30 minutes past its fallback time. It then fails closed (no buys next session) and alerts. A slow `ingest` therefore can't leave `regime` or `rank` reading stale data.
- **Every job fits in Lambda's 15-minute limit.** Each job logs its duration, and anything over 10 minutes raises an alert.
- **Trades go in at 15:40, not at the open.** The close has the day's best liquidity. Sells go first, through the sell sequence; then buys, as OTO marketable limits carrying their GTC stop legs (§5.7).

---

## 3. Daily schedule

All times are US/Eastern (EventBridge Scheduler, `America/New_York`). **Every job starts with the market-calendar guard** (§4).

| Time (ET) | Job | Purpose |
|---|---|---|
| **Evening (session T)** | | |
| 16:20 | `post_market` | Reconcile; cancel any unfilled buy parent (backstop); journal; equity snapshot against SPY, MTUM and the rule book; Telegram daily report |
| 17:00 | `ingest` | Gap-fill bars through T, and re-fetch T−1's bar (its volume grows after the evening, #118); universe and industries; corporate actions; macro (the evening VIX from CBOE's delayed quotes, #117); earnings calendar and surprises; rolling fundamentals |
| 17:15 (fallback) | `simulate` | Simulates session T for the **rule book** (§5.12), records session T's triggers in the **breakout tracker** (the regime's leadership-health input), and **scores past book reviews** once their period ends |
| 17:25 (fallback) | `regime` | 5 pillars → state machine → **computed regime** |
| 17:35 (fallback) | `rank` | The **momentum ranking** of the ranked set (§5.3) and each holding's percentile; **position events** (a report within 5 sessions, a one-session fall of ≥ 10%, corporate actions). **On a ranking session R:** the candidate list (the top 40 eligible names and every holding) with its facts and charts |
| 17:50 (fallback) | `book_review_submit` | **Ranking sessions only:** submit the Book Analyst reviews as one Claude batch |
| 19:00, 21:00, 06:00 | `book_review_collect` | Collect results (idempotent). Not done by 06:00 → no buys on E (rule sells still run), plus an alert |
| **Trading day (session T+1)** | | |
| 07:30 | `premarket_check` | Macro refresh from settled closes (computed regime may only **downgrade**, #117); overnight news; earnings-date changes |
| 08:45 | `portfolio_manager` | **Only on an execution session (or its retry) or with position events.** LLM regime view, slot picks from the top 16, sizes, early exits, stop raises → guardrails → `signals`. **Hard deadline 12:00:** no valid decision by then → no buys today (rule sells and stops still run) |
| 09:20 | `execute_open` | Reconcile → stops re-placed for splits with an ex-date today (§5.7) → stop raises (upward only). **No sales:** before the open a stop can't be reduced to free shares (spike 0.3b, #124) |
| 09:31 | `execute_open` (check run) | Reconcile + stop invariant → sales left unfilled at the last close, and cash-acquisition exits, through the sell sequence (marketable limit, §5.7) |
| Every 5 min, 09:35–16:00 | `stop_guard` | Invariant-only check (§5.7): every position's GTC stops cover its full quantity, and a partly filled buy counts as unprotected (#115). It asks the broker first and opens a database connection and takes the lock only to repair |
| 15:40 | `execute_close` | Reconcile + stop invariant. **On E:** rule sells (out of the hold zone or the ranked set) and the Portfolio Manager's exits through the sell sequence, then buys (OTO marketable limit at the decision price × 1.005, with its GTC stop leg), then stop raises. **Other sessions:** the Portfolio Manager's event exits only. No trades on half days: E moves to the next full session |
| **Other** | | |
| Saturday 10:00 | `weekly_review` | The book against the rule book, SPY and MTUM; the scorecard; **proposed** lessons |
| Every 15 min, 07:00–22:00 on weekdays | `watchdog` | Alert if an expected job heartbeat is missing, a run is stuck or a schedule is disabled. Each run also pings an external dead-man's switch (§5.13) |

One module, `jobs/schedule.py`, holds this schedule; the AWS schedules, the watchdog's expectations and the local stand-in are all built from it (#133). Until Phase 5 replaces them, the walking skeleton's jobs (1.7: `execute_entries`, `eod_review` and the scan-lite) keep running on `paper` for the 1.8 acceptance.
- **Fallbacks.** A chained job's time above is a fallback: it normally starts when its predecessor succeeds. Its schedule fires every 10 minutes from that time to its deadline (the time + `jobs.chain_wait_minutes`, 30), so a late predecessor still starts it within 10 minutes. A run that finds the predecessor not done records `WAITING_PREDECESSOR` and exits; a run at the deadline without one fails closed and alerts (§4).
- **Retries.** Buys blocked on E by a loss halt or a missing price are tried once more on the next session, with the same ranking (`book.buy_retry_sessions`); the Portfolio Manager runs again that morning for the empty slots.

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
- **`market_sessions` table:** date, open/close, `is_early_close`, minutes, data status, and the cross-check result per session.
- **Sync:** `python -m auratrade.calendar.sync` writes Alpaca's sessions (2016 through the end of next year), re-run daily. It is idempotent, removes a session Alpaca drops (an unscheduled closure), and refuses to drop more than 3 at once, so a vendor glitch can't empty the calendar. **A date outside the synced range is unknown, not closed:** jobs fail closed and alert instead of skipping as "market closed".
- **Guard policy per job type:**

  | Job type | Runs when |
  |---|---|
  | Morning and intraday jobs | Today is a session |
  | Evening jobs (`post_market` → `analyst_submit`) | Today was a session, and the predecessor job succeeded for it (§2) |
  | `analyst_collect` | Any day it's scheduled (idempotent, collects the latest open batch) |
  | `weekly_review` | Saturdays, regardless of sessions |
  | `watchdog` | Checks only heartbeats that were *expected* given that day's calendar |

**Holidays:** no calls or orders; hard stops are GTC at the broker; no entry order rests overnight (entry parents are GTC for their stop leg's sake, and `post_market` cancels any unfilled one, §5.7).

**Gap-fill ingest:** fetch every session since the last stored one.

**Early-close days (13:00):** no trades; a rebalance moves to the next full session (§7.1); half-day **volume normalized** (× 390 / session minutes).

**Sessions, not calendar days:** every window (the 21- and 252-session signal, 50-session dollar volume, earnings windows, hysteresis) is counted in sessions. The monthly rebalance is anchored on the calendar's last session of each month.

**Missing data vs. closed market:**

| Situation | Detected by | Action |
|---|---|---|
| Market closed | Not in `market_sessions` | Expected; nothing fetched |
| Vendor data incomplete | Under 90% of expected symbols | Retry, then `INCOMPLETE` → no new entries next session, plus an alert |
| A single symbol has no bar | Halted, suspended or delisted | Excluded from today's watchlist; never forward-filled |

**Macro staleness:** "latest value on or before" each session; more than 2 sessions stale → volatility pillar scores 0.

**Intraday prices:** current prices and opening ranges from **real-time IEX**; volume from **15-minute-delayed SIP**. Two exceptions (spike 0.3a): pre-market gaps come from delayed SIP (#110), and when IEX printed nothing in the first 5 minutes, the opening range comes from delayed SIP at 09:51 (#109).

**Long weekends:** the Portfolio Manager receives the calendar days until the next open (`days_until_next_open`: Fri → Mon = 3; counted in sessions the next open is always 1 away) and `is_long_weekend` (more than 3).

---

## 5. Components

### 5.1 Ingest (`ingest`)

- **Universe:** active, tradable US common stocks (NYSE, NASDAQ, AMEX), **ADRs included** (#111). Price ≥ $10, 50-day average dollar volume ≥ $10M, ≥ 252 sessions of history. This also screens out the thinly traded stocks IBD found perform poorly (under ~400k shares a day).
  - The price and dollar-volume filters use **raw (unadjusted) prices as they traded that day**. Split-adjusted history would misstate past prices: a $50 stock before a 10:1 split shows as $5.
  - **Classification** (Alpaca's asset API has no type flag; #111): the Nasdaq Trader symbol directory's ETF flag → symbol suffix (preferreds, units, warrants, rights; single-letter class shares such as `BRK.B` stay) → name rules (funds, notes, SPAC-style names; "Common Stock / Ordinary Shares / Depositary Shares" kept) → EDGAR SIC 6770 (blank checks). EDGAR fund filings (N-CSR, N-PORT) also mark a closed-end fund named like a stock ("… Term Trust … Common Shares") as a fund, and EDGAR's verdict stands over the name rules (#129).
  - **Point in time:** backtests rebuild the universe for each historical session from the symbols that were listed then, **including later-delisted ones**, as far as the vendor covers them. Coverage is measured in spike 0.3a and its gap is reported in every backtest (§15).
    - Alpaca's asset list forgets most delistings before ~2018, but its bars endpoint still serves them. The backfill adds **ticker-enumeration snapshots** (every 1–4 letter ticker that traded in a given week; ~3 minutes each) to the asset list.
    - **Zero-volume bars are dropped:** Alpaca repeats the last close with volume 0 for months after a delisting.
    - History is keyed by ticker, and tickers get reused, so securities are keyed by Alpaca asset id / CUSIP where known, and a ticker with a trading gap of more than 30 sessions is split into separate securities. Such a relisting has an unreliable base count (§5.3).
    - **A one-day close move of 4× (or ¼×) also starts a new security** (#130): it is a bankruptcy reorganization, a reverse split the vendor didn't adjust, or a spin-off far more often than a real move, and the new piece starts without history, the conservative side.
    - **Renamed-ticker duplicates are dropped** (#130): the vendor keeps serving a renamed company's bars under the old ticker until the rename (PKI and RVTY, TASR and AXON). Two securities with identical bars on ≥ 90% of their sessions are one company; the active one is kept (then the one with an asset id, then the longer history). The backfill dropped 860.
    - **EDGAR covers only securities still trading** (#130): SEC's ticker-to-CIK map lists today's tickers, so a delisted security has no CIK, and so no historical earnings dates or fundamentals. Backtests report that gap with the delisted-coverage gap (§15).
  - **Listing date** = the first *public trading* date, not the exchange listing date: an uplisting from OTC (e.g. CELH in 2017) is not an IPO. The base count anchors on it (§5.3, #126). Ingest sets it to the first stored bar when that falls inside the 2-year window, unless EDGAR shows a 10-K, 10-Q, 20-F or 40-F filed before it; then it stays unknown and the count is unreliable (#129).
- **Bars:** Alpaca SIP daily bars, `adjustment=split`, plus the raw close (for the universe filter); rolling **2 years** in Postgres, full history (2016 onward) in **Parquet**.
  - **Tracked set** (#129): bars are stored for universe-type tickers that pass **half** the universe's price and dollar-volume filters, plus the index ETFs. A ticker that starts passing gets its whole 2-year window, so this saves storage (about 140 MB per database for 2,761 tickers) without changing who is in the universe; the filters themselves are applied point in time when the bars are read.
  - **Gap-fill:** each tracked ticker is fetched from its last stored session, which also re-fetches T−1. If that re-fetched bar's split-adjusted close moved by more than 2%, the vendor re-adjusted the history (a split) and the whole window is re-fetched.
  - **Session T−1's bar is re-fetched** each evening: the volume of bars fetched at 16:58 grew later for 187 of 195 symbols, by up to 62% (spike 0.3a, #118).
  - **Outlier filter:** SIP daily bars carry bad ticks that pass a plain OHLC check (NVDA's high of 195.95 on 2024-06-10, true ≈ 122). A high or low far outside both the bar's open–close range and the neighbouring bars is flagged and not used, and the base count never trusts a single-bar extreme (#126). "Far" is more than max(15%, 3 × the stock's 20-session average daily range), so a volatile stock's real intraday spike is kept; the bar keeps its body's edge and the vendor's value goes to `bar_corrections` (#129).
  - **Split-only adjustment, not dividend adjustment:** pivots and stops computed from the bars become broker orders, so they must be in the prices the stock actually traded at. Dividend-adjusted history sits below real prices, and it changes all earlier bars on every ex-date, which gap-fill ingest would never re-fetch. RS and momentum use price returns, as IBD's RS rating does.
- **Corporate actions:** splits, reverse splits, mergers and symbol changes for held symbols and open orders (§5.7).
- **Validation:** coverage ≥ 90%, OHLC sanity, split re-fetch, idempotent upserts. Coverage counts the tracked tickers that traded at T−1 and the index ETFs; a missing index alone makes the session `INCOMPLETE`. Ingest refuses session T's bars before T's close plus the 15-minute SIP delay (#129).
- **Failure policy** (#129): the calendar re-sync, the universe refresh and the bars fail the job, so the evening chain stops (no new entries next session); so does a drop of more than 50 securities from Alpaca's asset list in one evening (a vendor glitch). Corporate actions, macro, company data, earnings, fundamentals and retention degrade: the stored data stays as it was and an alert goes out.
- **Industries:** SEC SIC codes → about 100 industry groups (Finnhub profile as fallback). A code rolls up to its 3- and then 2-digit prefix until its group has 15 tracked members (114 groups on the live data). Groups are named by their prefix ("SIC 367"), so a group keeps its row across the weekly regrouping, and a ticker belongs to the most specific group its code matches (#129).
- **Run time** (#129): the job must fit Lambda's 15 minutes (alert at 10). Requests to Alpaca and EDGAR are bound by the server's response time, so up to 4 run at once per vendor, under its rate limit. A nightly run took 219 s in the live check, most of it Finnhub's 55 requests a minute.
- **Macro** (spike 0.3d, #117):
  - **Evening VIX/VIX3M** come from CBOE's delayed-quote JSON, accepted only if its `last_trade_time` is ≥ 16:15 on session T (it was final at 16:32 ET); on a half day, 15 minutes after its close (#129). A value is never replaced by a lower-ranked source (history file > delayed quote > FRED). CBOE's history files update only at about 18:30–21:30 ET, after the 17:25 `regime` job, and FRED lags at least a full session.
  - **Fallbacks, in order:** the CBOE history file, if its last row's date is T (never its `Last-Modified` header); then FRED (T−1); then the staleness rule (§4). The next evening checks the JSON value against the history row and logs any difference.
  - **Backtests** use the CBOE history files, filtered to `market_sessions` dates (the VIX file has rows on holidays).
  - **Credit spread** (FRED `BAMLH0A0HYM2`, only 3 years of history): stored for the Portfolio Manager's context only; no code rule uses it.
- **Earnings:** Finnhub calendar **and surprises** (actual vs estimate), refreshed daily.
  - **History (for backtests):** report dates from SEC EDGAR 8-K filings with Item 2.02 (results of operations). The timing is the **earlier of the 8-K acceptance and the first earnings headline**: before 09:30 → before the open, after 16:00 → after the close. Many "during the session" 8-Ks are morning releases filed later, and the headline catches them; with no headline (9%), the 8-K alone. Finnhub's free calendar has no past dates (spike 0.3e, #119).
  - **Upcoming timing:** Finnhub leaves before/after-market blank for 61% of upcoming reports. Then the company's usual timing from its last 8 reports applies; with no history, the report counts as before the open (the cushion runs one session earlier). A tie in the usual timing goes to before the open, and a release on a closed day counts as before the next session's open (#129).
  - **Each evening:** the calendar is read day by day 63 sessions ahead, and a moved date replaces the old one only when every day was fetched. Reports of the last 3 sessions get their surprise (at most 150 requests a run) and their EDGAR 8-K event, the history the usual timing reads (#129).
- **Fundamentals:** SEC EDGAR company facts → quarterly EPS and revenue growth and acceleration. Q4 is the 10-K's annual value minus the three quarters; growth is year over year and only from a positive base. A quarter first filed more than 120 days after its period end (a comparative figure in a later report) or before it (a mis-tagged fact) is skipped. Each tracked ticker is refreshed every 20 sessions and after each report, at most 200 company-facts downloads a run; the last 12 quarters stay in Postgres (#129).
  - **Point in time:** every fact is keyed on its **`filed` date** (when the market could see it), never the fiscal period end. Backtests read a quarter only from the session after its filing. The period end is 4–6 weeks earlier, and using it would look ahead.
- **News:** Alpaca news API, on demand.

### 5.2 Regime classifier (`regime`)

Five pillars, each **+1, 0 or −1**:

| Pillar | +1 | −1 | Source |
|---|---|---|---|
| **Index trend** | **Both** SPY and QQQ: close > 50 SMA > 200 SMA, 50 SMA rising (above its value 10 sessions earlier) | **Either** closes < 200 SMA with the 50 SMA falling | Bars |
| **Breadth** | > 60% of the universe above its 50 SMA, and new highs − new lows (10-session average) > 0 | < 35% above the 50 SMA, or new lows > new highs (same 10-session average) | Full universe |
| **Volatility** | VIX < 20 and VIX/VIX3M < 0.9 | VIX/VIX3M > 1.0 or VIX > 30 | CBOE / FRED |
| **Distribution** | ≤ 3 distribution days in the last 25 sessions | ≥ 8 (IBD: 6; #135) | SPY and QQQ counted separately; the **higher** count is used ([IBD](#18-methodology-sources)) |
| **Leadership health** | ≥ 55% of **all** breakouts from the 20-session window are above their trigger 5 sessions later | ≤ 35% | Breakout tracker (§5.12) |

**Leadership health** is how a practitioner notices that "breakouts aren't working" before the indexes show it. Kullamägi trades "fewer or no trades" when setups stop working; Minervini sizes down when he's trading poorly.
- It uses the **ungated** breakout tracker, not the regime-gated baseline book. Otherwise it would stop getting data in exactly the regimes where it's needed, creating a feedback loop.
- It scores 0 with fewer than 10 tracked breakouts in the window.
- **The window is the 20 sessions whose 5-session result is complete:** breakouts triggered from T−24 through T−5. Breakouts from the last 5 sessions have no result yet. Counting them would mix partial results into the live score and look ahead in backtests.

| Total score | Regime |
|---|---|
| +2 to +5 | `CONFIRMED_UPTREND` |
| −1 to +1 | `UNDER_PRESSURE` |
| ≤ −2 | `CORRECTION` |
| CORRECTION, then a follow-through day | `EARLY_UPTREND` |

**These are provisional 4-pillar thresholds** from the regime backtest (2.4, #135; `docs/backtests/regime.md`). IBD's published values (`CONFIRMED_UPTREND` from +3, `CORRECTION` at ≤ −1, distribution −1 from 6 days) sat in `CORRECTION` 38% of 2017–2022 with leadership health absent (it scored 0 there; live, it reads the breakout tracker from task 3.7, #142) and ETF volume overcounting distribution days. All of them are set again on 5 pillars in the 3.8 re-run. Every score below is the configured one: `risk.yaml` holds the values.

- **Naming.** IBD's "rally attempt" is the stretch *before* a follow-through day. Here it is tracked as a sub-state inside `CORRECTION` (rally day count, rally low). `EARLY_UPTREND` is the period *after* the FTD, when only starter positions are allowed. Every prompt defines the four labels this way, so the LLM doesn't map them onto IBD's wording.
- **Distribution day** (IBD): an index down ≥ 0.2% on volume higher than the prior session, counted per index (SPY, QQQ). **Both counts reset to zero after a follow-through day.** ETF volume stands in for exchange volume. Sessions with quarterly option expirations or index rebalances inflate it, so distribution days on those sessions are flagged, and the regime backtest decides whether to exclude them. The flagged sessions are the third Friday of March, June, September and December (the session before, if that's a holiday) and the S&P 500 and Nasdaq-100 rebalance dates; they count by default (#127).
- **Follow-through day** (IBD): day 4 or later of a rally attempt, **either** index up ≥ 1.25% on volume higher than the prior session.
  - **Correction low:** each index's lowest intraday low since the session the regime entered `CORRECTION`. This also covers a `CORRECTION` entered on the score while the index is near its high: the low is then that session's low, and the rally attempt can start right away.
  - Day 1 is the first up close after that index's correction low. A new lower low restarts the count.
  - **Rally low:** the correction low that the current rally attempt started from. The `EARLY_UPTREND` it leads to keeps that rally low.
  - The best FTDs come on days 4–7, so later ones are flagged as weaker.
  - The attempt is cancelled if **either** index undercuts its rally low.
- **Hysteresis:**
  - Downgrades to `CORRECTION` from `CONFIRMED_UPTREND` or `UNDER_PRESSURE` are immediate (`correction_sessions: 1`; the backtest compares 2 sessions, with the index-trend pillar at −1 always immediate).
  - `CONFIRMED_UPTREND` → `UNDER_PRESSURE` happens at once on a score ≤ 0, or after 2 consecutive sessions at +1, so that one pillar flickering across a threshold doesn't flip the regime.
  - Upgrades need 3 consecutive sessions, counted only while in the current state. Leaving `EARLY_UPTREND` upward also needs at least 10 sessions in it.
  - `premarket_check` may only downgrade.
- **State transitions** (a new uptrend needs a follow-through day, as in IBD, with one safety fallback):

  | From | To | When |
  |---|---|---|
  | `CONFIRMED_UPTREND` | `UNDER_PRESSURE` | Score ≤ 0 (immediate), or score +1 for 2 consecutive sessions |
  | `CONFIRMED_UPTREND` / `UNDER_PRESSURE` | `CORRECTION` | Score ≤ −2 (immediate) |
  | `UNDER_PRESSURE` | `CONFIRMED_UPTREND` | Score ≥ +2 for 3 sessions |
  | `CORRECTION` | `EARLY_UPTREND` | Follow-through day |
  | `CORRECTION` | `EARLY_UPTREND` (flagged `no_ftd`) | **Fallback:** score ≥ +2 for 10 consecutive sessions without a qualifying FTD. ETF volume can miss a real FTD, and without this the system could sit in cash through a whole recovery. With no FTD, each index's **rally low** is its lowest intraday low over those 10 sessions |
  | `EARLY_UPTREND` | `CONFIRMED_UPTREND` | Score ≥ +2 for 3 sessions, after at least 10 sessions in `EARLY_UPTREND` |
  | `EARLY_UPTREND` | `CORRECTION` | Either index undercuts its rally low, or ≥ 6 distribution days since entering `EARLY_UPTREND` (higher index count; IBD's count, kept when the pillar's −1 moved to 8, #135) |

  **The score doesn't end `EARLY_UPTREND`.** A follow-through day comes near the low, when the index-trend, breadth and volatility pillars are usually still negative, so the score is often ≤ −1 on the FTD itself. An earlier draft sent `EARLY_UPTREND` back to `CORRECTION` on a score ≤ −1. That would have cancelled most FTDs at the next session, and the regime would have flapped on every later +1.25% day. As in IBD, a new uptrend fails on a rally-low undercut or heavy distribution after the FTD.

  **No timeout out of `EARLY_UPTREND`.** A stalled early uptrend keeps its small limits until it confirms or fails. An earlier draft moved it to `UNDER_PRESSURE` after 25 sessions, but that state allows more risk (§6), so stalling would have been rewarded. For the same reason, the step-up from starter limits after 10 sessions (§6) applies only on sessions with a score ≥ +1.

  **The 10-session minimum** keeps the starter limits (§6) in force for their full 10 sessions on both paths. Without it, the `no_ftd` path, which enters at a score ≥ +2, would reach `CONFIRMED_UPTREND` within 3 sessions.
- **Crisis override (code):** VIX/VIX3M > 1.0 for ≥ 2 sessions (for as long as it lasts), or SPY −4% in a session (for the rest of that session and the next one) → no new entries.
  - VIX/VIX3M is the latest value on or before the session, from any source in the order above (§5.1). The override is also set, with an alert, when every source is more than 2 sessions stale. With one stale day, the 2-session check can read the same observation twice; that is accepted (#127).
- **Starting state:** `CORRECTION` (fail closed). A replay starts at least 252 sessions before the first label it needs.
- **Conservatism ladder** (by the risk each state allows, §6): `CORRECTION` < `EARLY_UPTREND` < `UNDER_PRESSURE` < `CONFIRMED_UPTREND`. "More conservative" always means lower on this ladder.
- **LLM view** in the Portfolio Manager call; **effective regime = the lower of the computed regime and the LLM view.** The LLM may return the computed label or any label below it, except `EARLY_UPTREND`: only the state machine assigns that, because it requires a follow-through day. `premarket_check` downgrades along the same ladder.
- **A morning downgrade blocks that day's buys** (§6); an upgrade never forces anything. The setup system's rule about unreviewed strategies is archived with it.
- Thresholds are tuned in the regime backtest: tuned on 2016–2022 and checked once, untouched, on 2023–present (§15).
- **The regime is judged in both directions.** Turning defensive early in a decline isn't enough. It also has to stay invested in strong uptrends.
  - Several pillars score 0 in many healthy markets (VIX ≥ 20, 4–5 distribution days, under 10 tracked breakouts), so +2 could become the normal bull-market state. The 2.4 backtest found exactly that on 4 pillars, and `CONFIRMED_UPTREND` now starts at +2 (#135).
  - One session at the `CORRECTION` score drops to `CORRECTION`, and getting out needs a follow-through day.
  - So the backtest reports each pillar's score distribution, time in each regime during known uptrends, and every **false `CORRECTION`**: a `CORRECTION` spell lasting more than 15 sessions in which SPY never closes 8% or more below its prior 52-week high.
  - It also compares an immediate downgrade to `CORRECTION` against one that needs 2 sessions at that score (with the index-trend pillar at −1 still immediate).

### 5.3 Momentum ranking (`rank`)

The ranking replaces the setup scanner as the candidate list (#147). The five setup scans, the quality ranking and the verdict cache (archived spec: `docs/archive/README-setup-system.md` §5.3) stay in the code as **facts for the Book Analyst**, not as the pool.

**Every evening** (`book:` in `risk.yaml`; the exact rules are fixed in `docs/backtests/momentum-preregistration.md` §3):
1. **The ranked set:** the point-in-time universe (§5.1) with ≥ 253 bars, narrowed to the **1,000 with the highest 50-session average raw dollar volume** (`book.universe_size`). Dollar volume stands in for market cap, which has no free point-in-time history. Momentum doesn't weaken with size (Israel & Moskowitz 2013), and small, illiquid names lose most of its profit to costs (Lesmond, Schill & Zhou 2004; Korajczyk & Sadka 2004).
2. **The signal, 12-1:** close 21 bars back ÷ close 252 bars back − 1, on split-adjusted closes: the past year, skipping the last month (Jegadeesh & Titman 1993; Asness, Moskowitz & Pedersen 2013).
3. **Percentile** within the ranked set (`indicators.relative_strength.percentile_ranks`). Rank order is by the signal, ties broken alphabetically.
4. **Each holding's percentile**, and whether it's still in the ranked set: the hold-zone status the rebalance acts on (§7).
5. **Position events** for holdings: a report within `book.event_report_sessions` (5); a one-session fall of ≥ `book.event_drop` (10%); a corporate action (§5.7).

**On a ranking session R** (the last session of each month), it also builds:
- **The candidates:** the first `book.candidates` (16) names in rank order that are not held, have a price, fit at least 1 share in a slot (#122), and aren't linked by the cluster rule (§8) to 2 holdings. Only these can be bought.
- **The review list:** the first `book.review_candidates` (40) eligible names plus every holding, each with its facts:
  - the 12-1 return and percentile, distance from the 52-week high, smoothness (share of up days over the formation period, Da, Gurun & Warachka 2014), volatility (ATR %);
  - industry group and its RS rank, RS rating and RS line, the Trend Template result;
  - any current setup from the old scans (strategy and pivot, as a fact);
  - EPS and revenue growth (EDGAR, point in time), the next earnings date and the last surprise;
  - daily and weekly charts (`charts.py`, as before, without a pivot line).

### 5.4 Book Analyst (`book_review_submit` / `book_review_collect`, LLM)

- **Batch, no tools, ranking sessions only.** One request per name on the review list: the facts above, two charts, up to 5 headlines, the regime, and **the Analyst's previous verdict and note for the name** (continuity across months).
- **Output:** for a candidate `TAKE | PASS`; for a holding `KEEP | EXIT`; conviction 1–5, up to 3 key risks, reasoning (≤ ~80 words), a ≤ 20-word note for next time → `book_reviews`.
- Its verdicts are inputs; only the Portfolio Manager acts. **Every review is scored** (§5.12), which is why it reviews 40 names when only 16 can be bought.
- **The previous verdict is context, not a default.** The prompt says to judge from today's facts. The weekly review tracks how often verdicts flip.
- **The shared prefix is cached** across the batch.

### 5.5 Pre-market check (`premarket_check`)

- **07:30:** macro refresh (downgrade-only), overnight news for holdings and candidates, earnings-date changes.
  - The macro refresh uses settled closes only. If the evening read missed session T's VIX, it fetches T's close (the delayed-quote JSON, then the history file) and re-scores. CBOE's overnight-session VIX values are ignored, because they can't be backtested (#117).
- A news item or earnings change on a holding raises a position event for the Portfolio Manager.

### 5.6 Portfolio Manager (`portfolio_manager`, LLM, real-time)

- **Runs only** on an execution session (or its retry) or when a holding has a position event.
- **Inputs:**
  - account state (equity, cash, slot value, drawdown, the halts in force)
  - computed regime and pillars
  - the Book Analyst's verdicts on the candidates and holdings
  - **positions with events only** (plus a one-line summary of the rest)
  - clusters, approved lessons
- **Tools:** `get_news`, `get_chart`, `get_fundamentals`, `get_position_history`.
- **Decides:**
  - its regime view (more conservative only);
  - **which candidates fill the empty slots** (from the 16 only), each at 100% or 50% of a slot, or leaving a slot in cash;
  - **early exits** (for example stepping aside before a report or on bad news) and stop raises;
  - for each event, `KEEP / EXIT / RAISE_STOP`.

  It can't buy outside the candidates, size above a slot, hold a name past its rule exit, or lower a stop (§8).

### 5.7 Execution and the order layer (`execute_open` / `execute_close` / `stop_guard`)

1. **Reconcile.** **Invariant (every job that touches orders):** every position's resting broker sell stops are **GTC**, together cover its **full quantity**, and each sits at or above its plan's hard-stop level. Usually that is one stop; after a two-tranche entry or an add it can be one stop leg per buy (below). Otherwise repair immediately, alert and block entries.
   - **Between scheduled jobs**, the `stop_guard` (every 5 minutes, 09:35–16:00) checks existence, quantity and GTC from the broker alone. It repairs under the lock only when something is missing, using the plan level from the database. A job that dies after reducing a stop and before selling therefore leaves the shares unprotected for minutes, not until the next scheduled order job. `stop_guard` doesn't wait for the lock: while another job holds it, a sell sequence may legitimately be in progress, so it skips to its next run.
   - Order jobs are also invoked with Lambda's async retries on (they are idempotent through `client_order_id`), so a crashed job re-runs, reconciling first, within a few minutes.
   - **OTO stop legs** (spike 0.3b, #113). A leg takes its parent's time-in-force, and after the fill neither its time-in-force nor its quantity can be changed, only its stop price. So every entry parent is **GTC**, which makes the leg GTC from the start; the EOD review cancels any parent still unfilled (`post_market` as a backstop), so no entry rests overnight.
     - A leg stays in place, and stop raises replace its price, until a sale has to cut the quantity. Then, under the lock, the position's legs are cancelled and one standalone GTC stop is placed for the shares that remain: about 1 second with no stop, which `stop_guard` covers. From then on every change is an atomic replace. If the new stop is rejected because the price is already through its level, the shares are sold at once (step 2).
     - Alpaca sets leg `client_order_id`s itself, so reconciliation finds legs through their parent's `legs`, not by our prefix.
   - **The `fill_watcher` contingency isn't needed:** 0.3b showed that a stop-limit entry carries an attached stop leg, so the per-minute job that would have attached stops to plain entries isn't built.
2. **Sell sequencing.** A full-quantity stop reserves all the shares, so the broker would reject any other sell order. Every partial sale, trim or exit therefore:
   1. replaces the stop with the remaining quantity (or cancels it for a full exit); an OTO leg is first swapped for a standalone stop (step 1)
   2. submits the sell
   3. on rejection or no fill, restores the stop at once

   **Price already through the stop level.** Sometimes the market is at or below the level a stop would go at: when restoring a stop after a failed sell, raising one, or repairing the invariant after a gap down. Then no stop is placed. The shares are sold at once, by the same run, still holding the lock:
   - during the session: a marketable limit order. If it hasn't filled after a short wait (about 30 seconds, configured), the run re-prices it once, then replaces it with a market order. Leaving the retry to "the next order-touching job" could take hours, and after a 15:40 exit the next job runs after the close.
   - before the open: nothing is sold; the 09:31 check run sells (a stop couldn't trigger before 09:30 either)
   - a sale still unfilled at the close is retried by the next morning's 09:31 check run, with an alert. No OPG orders are used (#124)

   That is what the stop would have done. Alpaca rejects a sell stop above the market (`422 stop price must be less than current price`, spike 0.3b), so this path is required.
3. **09:20 (`execute_open`):** stop raises (only upward) and stops for splits (step 10). **No sales before the open:** on a closed market a stop replace stays `pending_replace` and keeps every share reserved, so an OPG sale is rejected and the stop can't be restored (spike 0.3b).
   **09:31 check run:** reconciles and enforces the invariant, then carries out cash-acquisition exits and any sale left unfilled at the last close, each through the sell sequence with a marketable limit. The cost is about one minute of exposure to the open, which the resting stop covers (#124).
4. **15:40 (`execute_close`, §5.9):** sells first, then each buy sized in code on the **worst fill (the limit price)**, clipped by the limits, and submitted as an **OTO marketable limit** (GTC parent, step 1) with its stop leg: never a market buy.
   - The book never buys more of a holding (§7.3). If a buy in a symbol with a resting sell stop is ever needed, it must be an OTO: Alpaca rejects a plain buy there as a "potential wash trade" (spike 0.3b, #114).
5. **Stop raises** (at a rebalance, or the Portfolio Manager's) replace the stop's price atomically, never down.
6. **Idempotency:** `idempotency_key` = `client_order_id`.
7. **Single writer.**
   - Every section of code that changes orders holds one account-wide Postgres advisory lock. It is held only while orders are being changed, never during an LLM call, and other jobs wait for it.
   - EventBridge and async Lambda invocations are at-least-once, and the `stop_guard` runs alongside other jobs. Without the lock, one job's invariant repair could re-place a full-quantity stop in the middle of another job's sell sequence.
   - The lock is taken on a direct (unpooled) Neon connection, because session-level advisory locks don't work through a transaction-mode pooler. The job releases it in a `finally` block. A Lambda that times out or is frozen may not close its connection at once, so the lock connection also has a server-side idle timeout, set longer than any order section, as a backstop.
   - A job that waits more than 2 minutes alerts, and after 8 minutes it fails (so Lambda retries it). The `stop_guard` doesn't wait; it skips to its next run. The backstop idle timeout is 10 minutes; if the lock stays taken for longer, `stop_guard` alerts (#131).
8. **Partial fills.**
   - The filled quantity is the position. The stop covers what actually filled, and the invariant always checks actual broker quantities.
   - **A partly filled buy counts as unprotected.** Its stop leg stays `held` (inactive) until the parent fills completely (spike 0.3b). The next order job or `stop_guard` (within about 5 minutes) cancels the unfilled rest, re-reads the orders, keeps the leg if it has become an active GTC stop for the filled shares, and otherwise places a standalone GTC stop for them (#115). The position is smaller than planned, which only lowers risk, and the rest's reserved exposure is released.
   - When a sell fills only partly, the stop is restored on the remaining shares at once, and the rest of the sale is retried by the next order-touching job (at the latest, the next morning's 09:31 check run).
9. **No margin, no PDT rule.** FINRA's pattern-day-trader rule (4 day trades in 5 sessions, the $25,000 minimum) was replaced by an intraday margin standard from 2026-06-04 (Rule 4210 amendment, SEC Release 34-105226). Spike 0.3b ran six same-day round trips on a $4,875 paper account with none blocked, and the account API has no PDT fields (#123). So entries are never limited by day trades. Instead, entries never use margin: total exposure, with pending entries at their limit price, stays ≤ 100% of equity (§6, §8), so an entry can't create an intraday margin deficit. Alpaca's live-account behaviour is confirmed before Phase 8, because paper may not model intraday margin calls.
10. **Corporate actions on held positions** (from ingest's corporate-action feed, checked again by reconciliation):

    | Event | Handling |
    |---|---|
    | Split / reverse split | On the ex-date Alpaca **rejects resting stops at 04:00 ET** (spike 0.3b). At 09:20, `execute_open` writes a new plan version with every level and quantity scaled by the ratio (not a loosening), then places a new stop at the scaled level for the broker's actual quantity; if the broker refuses it before the open, the 09:31 check run places it. **Every invariant repair checks the corporate-actions feed first,** so a position not yet adjusted, with an unscaled plan level, isn't read as "price through the stop" and sold. **Paper never applies splits to positions** (DXJ stayed 1 share at the old cost until it was closed), so reconciliation compares quantities scaled by the feed's ratio and alerts, instead of failing closed for the rest of the trial (#116) |
    | Definitive cash acquisition | Position event + alert; code exits at the next session's 09:31 check run. The stock now trades on the deal spread, not momentum |
    | Stock-for-stock merger, spin-off, symbol change | Position event + alert; no new entries in the symbol; a `mismatches` row is resolved by hand (`auratrade resolve`) before entries resume |
    | Trading halt (LULD / news) | Alert. The stop rests unchanged and runs when trading resumes |
    | Cash dividend | Under FINRA Rule 5330 a broker may lower resting sell stops by the dividend on the ex-date. Alpaca paper doesn't (QDTE's stop was unchanged on its 2026-10-15 ex-date, spike 0.3b). If a live broker does, the invariant catches the lowered stop and replaces it at the plan level (#72) |

### 5.8 Intraday monitor (retired)

The setup system's intraday monitor (early-behaviour rules, milestone stop raises, volume pace) has no role in the book. Between scheduled jobs, protection comes from `stop_guard` (§5.7).

### 5.9 Close execution (`execute_close`)

At 15:40 on every full session, under the lock, after reconciling and enforcing the invariant:
1. **On an execution session E (or its retry):**
   - **Rule sells** (§7.3): holdings below the hold zone or out of the ranked set.
   - **The Portfolio Manager's exits.** Both go through the sell sequence (§5.7), as marketable limits on the decision price.
   - **Buys** for the approved picks: the slot sized in code on the limit (the decision price × 1.005, rounded up to the tick), whole shares, as an **OTO marketable limit with its GTC stop leg** at the plan stop (§7.2).
   - **Stop raises** for the holdings kept, by atomic replace.
2. **On other sessions:** the Portfolio Manager's event exits only.
3. **No decision price** (#120): sells wait for the next session's 15:40 (the stop rests). A buy falls back to the delayed SIP last trade as its reference (#144): the limit bounds the fill. A buy that doesn't go in is retried once, the next session.

An unfilled buy parent is cancelled by `post_market` (no entry rests overnight; the parent is GTC only for its stop leg's sake, #113).

### 5.10 Post-market and journal (`post_market`)

- **`book_journal`:** one row per position: entry and exit, return %, holding sessions, MAE/MFE, the exit reason (rebalance, stop, LLM exit, corporate action), the regime at entry, linked decisions. It also records the **sim return**: the position replayed through `sim/book.py` as decided, so live and simulated books compare on the same fill model.
- **`account_snapshots`:** equity, drawdown, and SPY, MTUM and the rule book on the same day.
- The Telegram daily report.

### 5.11 Weekly review (`weekly_review`)

- The book's return and drawdown against **the rule book, SPY and MTUM**, since the start and per month (on the sim return, paper fills alongside).
- The scorecard (§5.12); the Analyst's TAKE rate and verdict flip rate; fallback-model answers reported separately.
- **Proposed lessons** take effect only after your approval (at most 10 active).
  - Each lesson cites its evidence, with at least 40 scored reviews or 10 closed positions, so a couple of unlucky months can't become a rule.
  - A lesson expires after 60 sessions unless you re-approve it.

### 5.12 Rule book, scorecard and breakout tracker

**The rule book** (the benchmark the LLM book is measured against; it replaces the setup system's shadow baseline):
- **Same inputs:** the same ranking, candidates, limits, overlay and halts, on its own $5,000 account (`baseline.start_equity`).
- **Rules instead of the LLM:** it fills empty slots from the candidates in rank order at full slots, and never exits early.
- **Simulated each evening** by `simulate`, through `sim/book.py` on daily bars, the same simulation the backtest uses (§15).
- **Like for like.** The LLM book trades on Alpaca paper fills, which are optimistic (§17). So every comparison uses the LLM book's decisions replayed through `sim/book.py` (the sim return, §5.10), and paper fills are reported alongside.

**The scorecard** (the main evidence for whether the LLM adds value):
- **Every review is scored.** Each name on a review list gets its return from E's close to the next E's close, minus the average of the top 40 → `book_scorecard`.
- **The headline statistic** is the monthly rank correlation between the Analyst's conviction and that relative return (the information coefficient). It is averaged over months, with its t-statistic and a bootstrap over months. TAKE vs PASS relative returns are shown beside it.
- **The Portfolio Manager** is judged on its picks against the rule book's picks over the same slots, and on its early exits against holding to the rule exit.
- Each review is tagged with its prompt version and the answering model (§9).
- **It starts early:** the evening pipeline runs in `dry_run` from the end of Phase 4 (task 4.7), so months of scored reviews exist before the paper trial. About 40 names a month means a meaningful information coefficient takes a year or more (§15).

**Breakout tracker** (kept only for the regime): it records every trigger of the old scans and its 5-session result, and feeds the leadership-health pillar (§5.2).

**One simulator.** The rule book, the backtests and the sim returns all use **one** simulation (`sim/`): `sim/fills.py` for every fill, `sim/book.py` for the portfolio. Both call the same pure ranking, sizing and guardrail functions as live trading.
- **Fills:** rebalance trades at the decision price (the close in backtests) plus the liquidity-tier slippage (#141), rounded to the tick against the trader, and within the limit.
- **Stops** fill at min(stop, the bar's open) **minus slippage**, so gaps go through and stop-outs pay the spread.

### 5.13 Observability and safety

- **Kill switch, halts and flatten:** one meaning everywhere.

  | Control | Blocks | Never touches |
  |---|---|---|
  | Kill switch (`system_flags.trading_enabled = false`) | New buys. `auratrade kill on` cancels resting entry orders at once (under the lock); every job also checks the flag and cancels any that remain | Protective stops, rule exits, stop raises, reconciliation |
  | Daily / weekly loss halt | New buys until the next day / week | Same |
  | Drawdown halt (−30%, `book.drawdown_halt`, #148) | New buys until you re-enable by hand | Same |
  | `auratrade flatten --confirm` (manual only) | Sells every position with the sell sequence | Nothing automatic ever calls it |
- **The kill switch is a database flag, not an environment variable.** Every job reads `system_flags` at start, so flipping it needs no redeploy.
- **Operator commands** (CLI; every "by hand" step in this README has one; Telegram commands come later, Phase 9):

  | Command | Does |
  |---|---|
  | `auratrade kill on` / `off` | Kill switch |
  | `auratrade halt status` / `reset <halt>` | Show or reset the loss and drawdown halts |
  | `auratrade mismatches` / `resolve <id> --note "…"` | List reconciliation and corporate-action mismatches; mark one resolved after fixing it at the broker |
  | `auratrade lessons approve` / `reject <id>` | Lessons (§5.11) |
  | `auratrade flatten --confirm` | Manual flatten (above) |

  `docs/runbook.md` says what to do for each alert type. It is written before the paper trial.
- **Watchdog** heartbeats. AWS can't watch itself: if the scheduler or the account is down, the watchdog is down too. So each watchdog run also pings a free external dead-man's switch (e.g. healthchecks.io), which alerts by email and Telegram when the pings stop. Each run (#133):
  - on a session day, reports a scheduled job with no run `jobs.watchdog_grace_minutes` (15) after its slot (a run started at or after the slot), or, for a chained job, no run for the session by its deadline plus the grace;
  - reports a run still RUNNING after `jobs.stale_running_minutes` (20): its process died;
  - in AWS, reports any schedule of its environment that isn't enabled. That is how `stop_guard` is watched, since it writes no heartbeat unless it acts;
  - alerts each finding once per session, and pings the dead-man's switch whether or not it found anything. A failed ping is only logged; the switch alerts when pings stop.
- **Telegram alerts:** failures, guardrail events, halts, mismatches, refusals, incomplete data, calendar disagreements.
- **Logs:** structlog → CloudWatch; LLM payloads → S3.

---

## 6. Market regime overlay

The regime classifier (§5.2) decides **whether the book may buy**, never whether it must sell (#147):

| Effective regime | New buys | Holdings |
|---|---|---|
| `CONFIRMED_UPTREND`, `UNDER_PRESSURE`, `EARLY_UPTREND` | Allowed (the Portfolio Manager may still use half slots or hold cash) | Kept by the rules (§7) |
| `CORRECTION` | **None** | Kept by the rules; rule sells and stops continue |
| Crisis override | **None** | Same |

- **Effective regime** = the lower of the computed regime and the LLM's view (§5.2). A morning downgrade to `CORRECTION` blocks that day's buys.
- **Why buys only:** Clenow buys only with the index above its 200-day, and holds what he has. Daniel & Moskowitz (2016) find momentum crashes come in panic states, when beaten-down stocks rebound; a long-only book suffers less than the long-short factor. The pre-registered backtest compares no overlay (C3) and Clenow's SPY 200-day filter (C4).
- **Thresholds:** live trading runs the provisional thresholds in `risk.yaml` (#135) until task 3.16 freezes the 5-pillar variant the backtest uses (`cu+2_corr-2_c2_dist8`, `docs/backtests/regime-5-pillar.md`). The leadership-health pillar keeps reading the breakout tracker (§5.12).

The setup system's regime-to-strategy table is archived (`docs/archive/README-setup-system.md` §6).

---

## 7. Entries, stops and exits

The book's rules (#147), all in `risk.yaml` `book:`; the backtest's exact versions are in `docs/backtests/momentum-preregistration.md` §3. The setup system's entries, plan menus, early-behaviour rules and earnings cushion are archived (`docs/archive/README-setup-system.md` §7).

### 7.1 The rebalance

| Step | Rule | Source |
|---|---|---|
| **When** | Ranked on the close of the **last session of each month** (R); traded at **15:40 on the next session** (E). A half day moves E to the next full session | Monthly is the academic standard (Jegadeesh & Titman 1993) |
| **Holdings** | **8** (`book.holdings`), in equal slots | The 4-8 positions of smaller practitioner books; at $5,000 a slot is about $625 |
| **Slot value** | Equity at E (after the sells) × `book.max_exposure` (100%) ÷ 8. The Portfolio Manager may take a half slot | Jegadeesh & Titman; Gray & Vogel (equal weight) |
| **What can be bought** | Only the **candidates**: the first 16 eligible names in rank order (§5.3) | — |
| **Who chooses** | Live: the Portfolio Manager, from the candidates. The rule book (and the backtest): the candidates in rank order, under the cluster rule | #2: the LLM decides within code's list |
| **Shares** | ⌊min(slot, cash left) ÷ limit⌋ on the raw price, whole shares (#122); 0 → the next pick | — |
| **Order** | OTO marketable limit at the decision price × 1.005 (rounded up to the tick), with its GTC stop leg (§5.7). Never a market buy | The #144 / tranche-2 order path |
| **No resizing** | Positions aren't trimmed or topped up between entries; winners can grow | Clenow re-sizes; not adopted, to avoid partial sales |
| **Start small** | `book.start_slot_fraction` (half slots) for the start of the paper trial and of live trading (§15) | Principle 6 |

### 7.2 The stop

- **One GTC stop at the broker** covering the full quantity (CLAUDE.md §3.5), at **80% of the decision price** at entry (`book.stop_fraction` 0.20), rounded down to the tick.
- **Raised at each later rebalance** to 80% of that session's decision price if that is higher. Never lowered. The Portfolio Manager may raise it further at any time.
- **After a stop-out** the slot stays in cash until the next rebalance (Han, Zhou & Zhu 2016).
- **Why so wide:** the momentum practitioners (Gray & Vogel, Clenow, Antonacci) use no stop at all. The broker stop is a safety rule, so it is set wide enough to rarely change the strategy, as a guard against a single-name collapse. Han, Zhou & Zhu's 10% stop is the pre-registered comparison C2.
- **Gaps go through it.** An overnight collapse fills at the open (§17).

### 7.3 Exits

In order of precedence, when several apply on the same day:
1. **The stop** (the broker).
2. **Corporate actions** (§5.7): a definitive cash acquisition exits at the next 09:31 check run.
3. **The Portfolio Manager's early exit** (tightening): executed at 15:40 the same day.
4. **The rule sell at a rebalance:** a holding whose percentile on R is below **90** (`book.hold_percentile`, the top decile), or which has left the ranked set, is sold at 15:40 on E (Novy-Marx & Velikov 2016's buy/hold spread cuts turnover; Clenow sells below the top 20%).

**Tighten-only:** later decisions may raise the stop or exit earlier. Holding a name past its rule exit, lowering a stop or buying more of a holding is rejected.

### 7.4 Earnings

**The book holds through reports and buys through them** (#147). A large share of momentum's return comes around earnings announcements (Jegadeesh & Titman 1993; Chan, Jegadeesh & Lakonishok 1996; Novy-Marx 2015), so stepping aside by rule would give it up.
- A report within 5 sessions raises a **position event**. The Portfolio Manager may step aside, which only tightens: it exits at 15:40 on the last session before the report (the report day itself for an after-close report).
- The setup system's earnings blackout and cushion are the pre-registered comparison C5.

---

## 8. Risk management and guardrails

Guardrails are pure Python functions (`auratrade/guardrails/`, the book's in `guardrails/book.py`) with 100% branch coverage. They run on **every** LLM action. Violations are rejected or clipped, logged to `guardrail_events`, and alerted.

**The risk posture changed with the strategy (#148).** At 8 slots with a 20% stop, each position risks about 2.5% of equity to its stop, and a full book about 20%. The setup system's R-based limits (1.5% per trade, 6% heat, the progressive-exposure multiplier) don't apply to the book; they are archived with it (`docs/archive/README-setup-system.md` §8).

| Guardrail | Default | Behaviour |
|---|---|---|
| Tradable symbols | Buys: today's candidates (§5.3). Exits: open positions | Reject |
| Max positions | 8 (`book.holdings`), counting pending buys | Reject |
| Position size at entry | ≤ 1 slot (equity × max exposure ÷ 8), or ½ slot when the Portfolio Manager says so; whole shares, floored; 0 shares → skipped | Clip / Reject |
| No margin | Total exposure, with pending buys at their limit, ≤ 100% of equity (§5.7) | Reject |
| **Max per correlated cluster** | 2 positions. A cluster is the same industry group **or** a 60-session daily-return correlation ≥ 0.7 with a held position; a correlation that can't be computed counts as linked (#140) | Reject |
| **Price levels** | The LLM never supplies a price. Limits and stops come from code (§7); the LLM may only raise a stop | Reject |
| Stop on every buy | Always: the OTO's GTC leg at the plan stop | Reject |
| Hard stop: GTC, covering the full quantity, ≥ plan level | Invariant check in every order-touching job and `stop_guard`; a partly filled buy counts as unprotected (§5.7) | Auto-repair + alert |
| Sell sequencing | Stop reduced before any sell (an OTO leg is first swapped for a standalone stop); restored on failure, or the shares sold at once if the price is already through the stop level (§5.7) | Enforced in the order layer |
| Single writer | One account-wide advisory lock around every order change (§5.7) | Enforced in the order layer |
| Tighten-only | No loosening: no lower stop, no holding past a rule exit, no adding to a holding | Reject |
| Order types | Buys always limit-protected | Reject |
| Regime / crisis | Effective regime `CORRECTION` or the crisis override (§6) | No buys |
| Daily / weekly loss halt | −2% / −5% (`halts:`) | No buys until the next day / week (one retry, §3); stops and exits continue (§5.13) |
| **Drawdown halt** | −30% from the peak (`book.drawdown_halt`) | No buys until re-enabled by hand; stops and exits continue (§5.13). A momentum book often falls 25-40%; −15% would have stopped it in most bad years (#148) |
| Early-close days | No trades | Reject |
| Data integrity | `INCOMPLETE` session or calendar disagreement | No buys |
| LLM failure | Error, timeout, refusal or invalid schema | No buys (fail closed); rule sells and stops continue |

---

## 9. LLM design and token budget

| Role | Model | Mode | Effort | Calls |
|---|---|---|---|---|
| Book Analyst | `claude-opus-5-5` | **Batch** (ranking evenings), shared prefix cached | `medium` or `high`, chosen on quality in task 4.6 | ≤ 48 a month (40 candidates + up to 8 holdings) |
| Portfolio Manager (+ regime view + events) | `claude-opus-5-5` | Real-time, with tools | `high` | 1–2 per rebalance, plus event days |
| Weekly review | `claude-opus-5-5` | Real-time | `high` | 1 per week |

**Expected LLM cost: about $3–8/month** (#148), from the spike 0.3g measurements (#121):
- **Book Analyst:** about $0.016 a request in the batch, about $0.80 a month.
- **Portfolio Manager:** $0.10–0.30 a call; a few a month.
- **Weekly:** about $2 a month.

The range allows for prompts growing. It is re-measured in 4.7. **Cost alerts:** above $1.00 for a batch, and above $0.50 for a real-time call.

**Token-saving measures:** the Batch API for the Analyst (−50%); prompt caching inside the batch (best-effort, so the estimate assumes none); skip logic (no real-time call on a session without a rebalance or an event); event-driven position context; code carries out every rule exit; output caps (reasoning ≤ ~80 words, ≤ 3 risks, note ≤ 20 words); compact facts and small charts (image tokens ≈ w × h / 750).

**API usage:**
- structured outputs into Pydantic models
- chart PNGs as image blocks
- `strict: true` tools with `tool_choice: auto`
- check `stop_reason`; a refusal → no action
- every real-time call has a **hard deadline** (Portfolio Manager 12:00), enforced by the job, not by the model: a late answer is discarded (fail closed)
- server-side fallbacks enabled. A fallback can answer with a different model, so the answering model (`response.model`) is stored on every `llm_decisions` row, and the scorecard and the weekly review report fallback answers separately
- effort set explicitly per role (Opus 5.5 always thinks; its default effort is `medium`)
- versioned prompts in `prompt_versions`
- **untrusted text is data.** Headlines, news bodies (`get_news`) and filing text come from third parties and could carry instructions. They are passed in clearly delimited data blocks, and every system prompt says to treat them as information about the stock, never as instructions. The guardrails bound what a manipulated answer could do (it can only pick among the candidates within the limits), but a planted headline could still bias a choice.

**Output schemas (abridged):**

```jsonc
// Book Analyst → book_reviews (one per name on the review list)
{ "symbol": "ANET", "verdict": "TAKE" | "PASS" | "KEEP" | "EXIT", "conviction": 4,
  "key_risks": ["report in 4 sessions"], "reasoning": "≤ 80 words", "note": "≤ 20 words" }

// Portfolio Manager → guardrails → signals
{ "regime_view": { "label": "UNDER_PRESSURE", "reasoning": "..." },
  "buys": [{ "symbol": "ANET", "slot_fraction": 1.0 | 0.5, "thesis": "..." }],
  "exits": [{ "symbol": "SMCI", "reasoning": "..." }],
  "stop_raises": [{ "symbol": "NVDA", "stop": 118.40, "reasoning": "..." }] }
```

A `stop_raises` price is the one place the LLM names a number. Code accepts it only above the current stop and below the last price; otherwise it is rejected.

---

## 10. Data model

PostgreSQL on Neon. SQLAlchemy 2.0, Alembic.

| Table | Purpose |
|---|---|
| `tickers` | Universe metadata: int `id`, symbol, exchange, type, SIC, industry group, active, delisted_at |
| `industry_groups` | Group, SIC ranges, daily group RS rank |
| `market_sessions` | Calendar: open/close, early close, minutes, data status and coverage, calendar cross-check result |
| `daily_bars` | Split-adjusted OHLCV `(ticker_id, date)` + raw close (for point-in-time filters), rolling 2 years |
| `bar_corrections` | Highs and lows the outlier filter replaced, with the vendor's value (#126, #129) |
| `corporate_actions` | Splits, reverse splits, mergers, symbol changes: ex-date, ratio / terms, handled flag |
| `macro_series` | VIX, VIX3M, credit spread, with `fetched_at` |
| `fundamentals` | Quarterly EPS and revenue, growth and acceleration |
| `earnings_events` | Date, timing (and whether it was inferred), fiscal period, actual vs estimate (surprise), source; EDGAR rows keep the 8-K acceptance and headline times |
| `bases` | Detected bases per symbol: start/end, depth, contractions, pivot, **stage number**, whether the count is reliable |
| `market_regime` | Per session: 5 pillars, features, computed / LLM / effective regime, crisis flag |
| `book_rankings` | Per ranking session: each ranked security's 12-1 signal, percentile and eligibility; each evening, the holdings' percentiles (about 12,000 rows a year; kept) |
| `book_reviews` | Book Analyst verdicts per ranking session: verdict, conviction, risks, reasoning, note, prompt version, answering model |
| `book_scorecard` | One row per reviewed name per rebalance: its return to the next E and relative to the top-40 average (§5.12). Small, kept forever |
| `rule_book_positions` | The rule book's simulated holdings and trades (§5.12) |
| `book_journal` | One row per position: entry, exit, return %, sim return, holding sessions, MAE/MFE, exit reason, regime, links (§5.10) |
| `daily_watchlist` | The old scans' full output (facts for the Analyst): strategy, rank and features (JSONB), chart keys |
| `setups`, `ep_candidates`, `verdict_outcomes`, `baseline_signals`, `trade_journal` | The setup system's tables (archived spec); kept, no longer written once Phase 4-5 replace their jobs |
| `llm_batches`, `prompt_versions`, `llm_decisions` | LLM bookkeeping; each decision stores its prompt version, requested model and answering model |
| `signals` | **The queue**: idempotency_key, action, strategy, **source**, trigger type, tranche, prices, risk %, shares, client_order_id, status. `source = SKELETON` marks the walking skeleton's rule-only trades (task 1.7), which "your numbers" and the go-live statistics leave out (#131) |
| `guardrail_events` | Rule, proposed vs enforced value |
| `orders`, `positions` | Broker mirror; strategy tag and source; regime at entry; a sale still owed (`exit_pending`) |
| `position_plans` | Versioned, tighten-only plan per position: its stop level and hold-zone rule |
| `position_events` | Reports ahead, one-session falls, corporate actions, flags |
| `breakout_tracker` | Every scan trigger (ungated) and its 5-session result → leadership health |
| `account_snapshots` | Equity, drawdown, and SPY, MTUM and the rule book on the same day |
| `lessons` | Proposed / approved / retired, with evidence |
| `job_runs`, `system_flags` | Heartbeats; kill switch (`trading_enabled`) and halts |
| `mismatches` | Reconciliation and corporate-action mismatches: detected, details, resolved_at, resolution note. An open row blocks entries in its symbol (or all entries, if account-level) |

**Retention (to stay within the 0.5 GB free tier):** `daily_bars` 2 years (older bars move to Parquet, one file per session keyed by symbol and Alpaca asset id, since ticker ids differ per database). The session files are the backfill's own (`parquet/daily_bars/year=YYYY/YYYY-MM-DD.parquet`): retention **merges** a leaving session into the backfill's file, so the securities Postgres never held (delisted before the database existed) stay in it (#130); `daily_watchlist`, `breakout_tracker` and `llm_decisions` output 90 days in Postgres, then archived to Parquet in S3. Journal, plans, signals, orders, regime, lessons and `bases` are kept forever (small). The base count carries forward from `bases` rows, so they must outlive the 2-year bar window (#126). Size is checked in the daily report, with an alert at 70%. Two years of bars for the tracked set take about 140 MB per database branch (live check, 2026-10-15), so `dev` and `paper` together use about 55% of the tier.

**Backup:** the free tier's restore window is short, and the kept-forever tables are the validation record (principle 8). So `post_market` exports them to S3 every night (Parquet, one file per table per day; S3 lifecycle keeps 90 days of dailies plus month-ends).

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
- **The trading stack** (`AuraTradePaper`, `infra/stacks/trading.py`, #133):
  - **Image:** one x86_64 container image (the AWS Lambda Python 3.13 base plus the locked dependencies, `Dockerfile`), built by `cdk deploy`; each job's function runs it with its own handler. A `.dockerignore` allow-list keeps `.env` and `var/` out of the build.
  - **Functions:** order jobs (`execute_open`, `execute_close`, `post_market`; the skeleton's `execute_entries` and `eod_review` until Phase 5) get 15 minutes and Lambda's 2 async retries (idempotent through `client_order_id`); the other jobs don't retry, since the next slot or fallback covers them. Events older than an hour are dropped. The scheduler retries only a failed start, within 5 minutes. Logs are kept 30 days.
  - **Secrets:** SecureString parameters under `/auratrade/<env>/`, put there by `scripts/put_parameters.py` and read once per container at start (`auratrade.parameters`); none appear in the template. A variable already set wins; an empty path fails the job (fail closed). `TRADING_MODE` is never a parameter: it is a deploy-time choice (`-c trading_mode=paper`), `dry_run` by default, and the stack refuses `live`.
  - **Grants:** each evening job may invoke only its successor; only the watchdog may list schedules; one private, encrypted, TLS-only bucket per environment (kept if the stack is deleted) is reachable only by `post_market` (backups), `ingest` (the retention archive) and `simulate` (its cached minute bars).
  - **Each environment has its own Alpaca account and Neon branch.** If two systems share one paper account, each one's reconciliation sees the other's positions as mismatches and "repairs" stops it doesn't own. `dev` (local work and Phase 5 end-to-end tests) and `paper` (the deployed skeleton, then the trial) therefore use separate Alpaca paper accounts.
  - **Env files:** locally each environment has its own git-ignored file with the same keys and layout as `.env.example`: `.env` for `dev`, `.env.paper` for `paper`. `scripts/check_keys.py --env-file .env.paper` checks one with free calls, and `put_parameters.py` copies `.env.paper` into SSM (an empty shared key falls back to `.env`).
- **AWS budget alarms** at $10 and $25.
- **Public project site** (`AuraTradePublicSite`, decision #108): static files from `web/` in a **private** S3 bucket, served by **CloudFront** with origin access control, so the bucket is readable only through CloudFront. It is HTTPS only, has security headers with a strict content-security policy, and has no backend: nothing on the internet reaches Neon, Alpaca or SSM. The optional custom domain uses an ACM certificate (us-east-1) and Route 53 records for the apex and `www`. Data panels read one allow-listed file, `data/snapshot.json`, written nightly by `post_market` (task 5.7). It publishes closed trades only, never open positions, stops or IDs, and site deploys never prune it.
- **Neon free-tier compute:** the watchdog keeps the database awake for much of the day. Compute hours are checked against the free allowance in 1.8. The `stop_guard` asks the broker first and opens a database connection only when there is something to do.

| Item | Monthly estimate |
|---|---|
| Claude API | ~$3–8 (§9) |
| AWS | ~$1–5 |
| Public site domain (optional) | ~$1.50 (about $13/year for a `.com`, plus $0.50 for the Route 53 zone); CloudFront and S3 stay in the free tier |
| Data, database, alerts | $0 (free tiers) |
| **Total** | **~$6–15** (budget: < $150) |

---

## 13. Repository layout

```text
AuraTrade/
├── README.md
├── .github/workflows/            # CI: lint → type-check → tests (deploy added in 6.2)
├── docs/                         # execution plan, spikes, backtests, incidents, runbook
├── web/                          # public project site: static HTML/CSS (§12)
├── pyproject.toml / uv.lock
├── Dockerfile
├── docker-compose.yml
├── alembic/
├── config/risk.yaml              # regimes, strategies, entry rules, exit menus, guardrails, multiplier
├── src/auratrade/
│   ├── config.py
│   ├── cli.py                    # operator commands (§5.13): kill, mismatches, resolve so far
│   ├── calendar/
│   ├── db/
│   ├── data/                     # alpaca_data, macro, edgar, finnhub, news, nasdaq (symbol directory)
│   ├── ingest/                   # the evening ingest: classify, universe, bars, corporate actions,
│   │                             # macro, companies, earnings, fundamentals, retention
│   ├── history/                  # the Parquet history since 2016 (#130): discover, bars, securities,
│   │                             # build, news, edgar, macro, load (into Postgres), read, store
│   ├── indicators/               # MAs, ATR/ADR, RS + RS line, group RS, U/D volume, momentum rank,
│   │                             # contractions, bases + base count, climax signals, volume pace, breadth
│   ├── regime/
│   ├── charts.py                 # the Analyst's daily and weekly chart PNGs (mplfinance)
│   ├── strategies/               # momentum_book (the ranking, candidates, rule book's choice; #147);
│   │                             # the setup system, unwired (facts for the Analyst): base, registry, trend_template, vcp_breakout,
│   │                             # momentum_breakout, episodic_pivot, leader_pullback, emerging_leader
│   │                             # ranking, verdict_cache, watchlist (the evening's full scan), store
│   ├── plans/                    # plan versioning (tighten-only; the book's plans use it from Phase 5); the setup system's menus,
│   │                             # early-behaviour and soft rules, 8-week hold, cushion, events (unwired)
│   ├── llm/
│   ├── guardrails/               # book (the momentum book's limits, #148); the setup system's rules, unwired
│   ├── execution/                # account, orders, reconciliation (used by the book); the setup system's sizing,
│   │                             # triggers (pivot / opening-range high) and tranches (unwired);
│   │                             # from 1.7: broker interface + Alpaca adapter, the stop invariant, the order desk
│   │                             # (sell sequence, sell at once), the single-writer lock, signals, the skeleton's rules
│   ├── journal/                  # rule_book (the momentum book's shadow, 4.5), the book scorecard; journal, reports, lessons;
│   │                             # the setup system's: your-numbers, baseline, verdict scorecard, multiplier
│   │                             # (3.7: baseline, scorecard, breakout_tracker, replay (sim R, default-plan R),
│   │                             #  records, evening: the `simulate` job's steps)
│   ├── sim/                      # the one fill simulator + fake broker: backtests, rule book, replay tests
│   │                             # (book.py: the momentum book's portfolio simulation, 3.12)
│   │                             # (fake_broker.py from 1.7: the 0.3b behaviours + fault injection, `play` since 3.6;
│   │                             #  fills.py: the fill model; trade.py: one approved entry played forward;
│   │                             #  sessions.py: a symbol's sessions; minutes.py: the minute-bar cache)
│   ├── storage/
│   ├── notify/
│   └── jobs/
├── backtest/                     # momentum (the book's pre-registered backtest, 3.13); regime; the setup system's baseline
├── scripts/                      # one-off operator tools: check_keys.py, backfill.py, smoke_real_data.py,
│                                 # local_session.py (the jobs on their schedule on this PC),
│                                 # put_parameters.py (an environment's secrets into SSM),
│                                 # reset_trading_state.py (clear trading state copied from another branch),
│                                 # scan_real_data.py (every strategy scan over the tuning period)
├── spikes/
├── infra/                        # CDK app: app.py, stacks/ (public_site, trading: AuraTradePaper from 1.8)
└── tests/
```

---

## 14. Configuration

- **`ENVIRONMENT`:** `dev` | `paper`; names the Alpaca account and Neon branch a process belongs to, and prefixes its alerts. Set in each env file (§12) and by the stack in AWS.
- **`TRADING_MODE`:** `dry_run` | `paper` | `live` (live needs an explicit flag and separate credentials, added with the Phase 8 live stack; until then the settings refuse `live` outright). A job's `--dry-run` flag forces `dry_run` for that run; nothing can raise the mode.
- **Kill switch:** `system_flags.trading_enabled` in the database (§5.13), flipped with `auratrade kill on|off`. It is deliberately not an environment variable, so it works without a redeploy. A new database starts with it on (entries disabled; a missing flag row reads the same way), so a fresh environment trades only after someone runs `auratrade kill off`.
- **Models / effort:** `ANALYST_MODEL` (the Book Analyst), `PM_MODEL`, `WEEKLY_MODEL`, `*_EFFORT`; `ANALYST_MAX_CANDIDATES=40` (the review list, `book.review_candidates`). `EOD_*` belong to the setup system's EOD role; Phase 4 removes them.
- **`config/risk.yaml`:** the momentum book (`book:`), regime thresholds, halts, the cluster rule, the fill model, the universe filters and the ingest settings; and the setup system's sections (fundable strategies, entry rules, exit menus, multiplier, early-behaviour rules, cushion), marked "archived README" and unwired. Its hash is logged with every run.
- **Paper account:** set the Alpaca paper equity to the intended live capital ($5,000, #122).

---

## 15. Validation and go-live criteria

### Backtest validity (the momentum book)

The book's backtest decides whether capital is used, so it follows rules that keep it from flattering the strategy. The full text, fixed before any book code read real data, is `docs/backtests/momentum-preregistration.md` (#148). In short:
- **Point-in-time universe** including delisted securities as far as the vendor covers them; filters and sizing on raw prices as traded. Each report states the delisted-coverage gap and which way it biases the result.
- **One simulation** (`sim/book.py` on `sim/fills.py`): $5,000 in whole shares, the liquidity-tier slippage (#141) on every fill, gaps through stops, cash earning nothing. A held security that ends exits at the conservative price (the pre-registration's §3.8). The simulator's and the harness's readings of the gaps in the pre-registration are #150 and #151.
- **Benchmarks:** SPY and MTUM on total return; the book on price only, which biases the comparison against it.
- **Tuning period 2017–2022** (2016 is the warm-up for the 12-month signal). **The 2023+ holdout is opened once, for the frozen configuration only.** It has never been opened: the setup system's tests failed before reaching it.
- **Per-year results** in every table; no conclusion may rest on a single year. 2023–25 was unusually kind to momentum.
- **Comparisons:** seven, fixed in advance (C1–C7), each changing one thing. One is adopted only if its daily-return difference against the primary has a 90% block-bootstrap interval wholly in its favour on the tuning period, with no deeper drawdown. A safety rule (the overlay, the cluster cap) stays unless removing it passes that test; the setup system's earnings rules (C5) are adopted if they give a shallower drawdown without a return cost beyond noise. **The holdout is never used to choose.** That departs from the setup system's "beats on both periods" rule, which spent the holdout on selection (#148).
- **The power problem.** The book's tracking error against SPY is likely 15–25% a year, so 6 years can't tell a realistic 2–5% edge from zero. **The gate therefore rests on point estimates and robustness:**
  - **G1:** excess CAGR over SPY > 0.
  - **G2:** excess CAGR over MTUM ≥ 0.
  - **G3:** positive excess over SPY in ≥ 3 of the 6 years, and still positive with the best year left out.
  - **G4:** CAGR ÷ max drawdown ≥ SPY's, and max drawdown ≤ 45%.

  The intervals and the minimum detectable effect are reported beside it.
- **The holdout gate:** excess CAGR over SPY > 0 and over MTUM ≥ 0.
- **What the gate blocks:** capital, meaning LLM buys placed with the broker (Phase 5's live path) and the paper trial. **On a tuning fail** the holdout stays closed and nothing more is tried on 2017–2022; it comes back to you. **On a holdout fail** the holdout is spent; it comes back to you.
- **The LLM isn't backtested.** The model has read what happened to these stocks. The gate tests the rule book, which takes the same candidates in rank order; the LLM's value is measured forward (§5.12).

The setup system's backtest rules and its baseline gate (which failed, #143-#144) are archived in `docs/archive/README-setup-system.md` §15.

### Paper trial and go-live

The paper trial runs until **the later of 6 rebalances or 3 months** (#149). Six monthly decisions can't prove an edge, so go-live judges **process quality and relative performance**. Most of the statistical weight comes from the **scorecard** (reviews scored from the end of Phase 4, months before the trial, §5.12). The paper account is reset before the trial starts, so skeleton-phase trades don't mix in. The trial starts at half slots (`book.start_slot_fraction`).

Required before going live:
- **Operations:** every rebalance executed or correctly blocked; every job ran or was correctly skipped; no position was ever without a full-quantity broker stop; zero duplicate orders; zero unresolved mismatches.
- **Guardrails:** none bypassed; every intervention logged and alerted.
- **Relative performance:** the LLM book (its decisions replayed through `sim/book.py`) is **not worse than the rule book** by more than the margin δ set in advance (below), on return and max drawdown. Paper fills are reported alongside.
- **The LLM's value (scorecard):** the information coefficient of conviction is positive. If its interval still includes zero, the inconclusive-result rule applies (below).
- **Operations readiness:** `docs/runbook.md` exists, and the operator commands (§5.13) and the external dead-man's switch have each been exercised once.
- **Human review:** a sample of decisions and reasoning has been read and judged sound.

Go-live starts at half slots with reduced capital; full slots follow after one clean quarter.

### Pre-registered statistics

**Before the paper trial starts** (task 7.0), `docs/backtests/power.md` records, from the 4.7 scorecard data:
- the monthly information coefficient's standard deviation, and the months needed to detect a given mean;
- the **non-inferiority margin δ** for "not worse than the rule book" (default 1% a year of return, and a max-drawdown margin).

**Inconclusive-result rule.** If at the trial's end the information coefficient's interval still includes zero:
- go-live (at half slots, as always) is allowed only if its point estimate is > 0 and the LLM book is within δ of the rule book;
- the step to full slots additionally waits until the interval excludes zero on the continuing scorecard.

If the point estimate is ≤ 0, the LLM isn't adding value: don't go live, and bring it to the user.

---

## 16. Decision log

| # | Decision | Choice |
|---|---|---|
| 1 | Decision-maker | LLM inside code-enforced guardrails (a deterministic-first approach was rejected) |
| 2 | Regime layer | Score + FTD state machine; LLM view only more conservative |
| 3 | LLM | `claude-opus-5-5` everywhere; Analyst ≤ 40/day via batch |
| 4 | Analyst timing | Evening batch; EPs reviewed in the morning |
| 5 | Budget | < $150/month; expected ~$40–70 (revised in #65 and #78) |
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
| 41 | Regime | 5th pillar "leadership health" from the ungated breakout tracker (#46); distribution count resets after an FTD |
| 42 | Keep losses small | After 20 trades: initial stop ≤ ½ × rolling average gain (made precise in #59) |
| 43 | Analyst continuity | Previous verdict + watch note included in the next review |
| 44 | Gap timing | Pre-market gap and EP checks at 08:40 (IEX pre-market opens 08:00), re-checked at the open |
| 45 | Order mechanics | Sell sequencing (stop reduced first); stop invariant = GTC + full quantity + plan level, in every order job |
| 46 | Leadership health | Ungated breakout tracker, separate from the gated baseline book |
| 47 | Regime transitions | Explicit table; a new uptrend needs a follow-through day (fallback and debounce added in #55) |
| 48 | Rule precedence | Fixed order; plan transitions committed at entry are not loosening |
| 49 | Baseline mechanics | Same top-40 list; rank-order fill; simulated each evening from minute bars |
| 50 | Retention | 90-day hot window for large tables; archive to Parquet |
| | *Design review, 2026-10-07* | |
| 51 | LLM price levels | Pivot and stop come from code. The LLM may nudge the pivot ±3% (with a reason) or tighten the stop, never invent a level; the trigger must be above the market at order time. *Why:* an LLM-written pivot below the market turns a buy-stop into a market buy, and the max-chase check would measure from the wrong level |
| 52 | Halt semantics | Kill switch and every halt block entries and adds only; stops and plan exits always run; flattening is a manual command. *Why:* "all trading stops" could be built as also stopping exits |
| 53 | Portfolio risk | Open-risk ("heat") cap of 6% of equity; clusters = industry group **or** 60-session correlation ≥ 0.7, max 2 each. *Why:* per-trade limits don't bound a correlated gap-down; SIC groups miss real correlation |
| 54 | Corporate actions | Splits scale plan levels and quantities (a new plan version, not a loosening); cash acquisitions exit at the next open; other events → event + manual resolution |
| 55 | Regime fixes | `RALLY_ATTEMPT` renamed `EARLY_UPTREND` (IBD's "rally attempt" is the pre-FTD sub-state inside CORRECTION); fallback CORRECTION → EARLY_UPTREND after 10 sessions at ≥ +3 without an FTD; the CU → UP downgrade needs ≤ +1 or 2 sessions at +2; distribution counted per index, higher count used; FTD on either index. *Why:* name collision with IBD, the risk of being stuck in cash because ETF volume misses an FTD, and regime flapping |
| 56 | Soft-rule timing | "Close-based" = the 15:40 decision price, exit executed at once; the same price in live trading, the baseline and the scorecard; backtests measure the gap to the true close |
| 57 | LLM deadlines | Portfolio Manager by 09:10, EOD by 15:50 (12:50); late answers discarded (fail closed) |
| 58 | Multiplier definitions | Win ≥ +0.5R, loss ≤ −0.5R, otherwise a scratch; the first matching state wins, most conservative first; drawdown tiers apply while the drawdown lasts |
| 59 | Keep losses small (precise) | Max width = min(default, ½ × rolling average gain); below 1 × ATR → reject instead of squeezing the stop |
| 60 | Unknown earnings date | Estimate = last report + 63 sessions, ±10-session blackout; no history → reject (fail closed) |
| 61 | Backtest validity | Point-in-time universe incl. delisted, raw-price filters, fill and slippage model, 2016–2022 tune / 2023+ holdout, ≥ 30 trades per tuned cell, source default kept unless clearly beaten on both periods |
| 62 | LLM evaluation | Analyst verdict scorecard (every verdict simulated forward) + LLM-vs-baseline on disagreements carry the go-live evidence. *Why:* 30 trades can't separate skill from luck |
| 63 | One simulator | One fill simulator and fake broker (`sim/`) for backtests, baseline, scorecard and replay tests, calling the live plan and guardrail code |
| 64 | Build order | 0.3b (order mechanics) runs first, with a pre-chosen `fill_watcher` fallback; a thin **walking skeleton** (rule-only, paper, deployed) follows the data layer, so integration risk shows up early; strategy build split into core (VCP, momentum, EP) then pullback / emerging |
| 65 | LLM cost estimate | ~$30–50/month: thinking bills as output and cache hits inside a batch are best-effort. Still far under budget; Phase 4 acceptance uses the 0.3g measurement, not a fixed figure |
| | *Design review 2, 2026-10-08* | |
| 66 | Stop-width noise floor | Keep-losses-small floor = 1 × ATR for VCP / emerging / pullback, 0.5 × ATR for momentum / EP; ATR = 14 sessions, ADR = 20 sessions. Amends #59. *Why:* true range ≥ high − low, so ATR ≥ ADR, and a 1 × ATR floor under a ≤ 1 × ADR stop would have rejected every momentum and EP entry |
| 67 | Price through the stop | When the market is already at or below the level a stop would be placed at (restore, raise, repair), sell at once instead of placing a stop; a 09:31 check run catches OPG exits that didn't fill. *Why:* "restore the stop" was undefined after a gap or fast drop, and an unfilled OPG exit had no stop until 09:36 |
| 68 | Single writer | One account-wide Postgres advisory lock (direct connection) around every order change. *Why:* scheduler and Lambda invocations are at-least-once, and the `fill_watcher` runs alongside other jobs; an invariant repair could interleave with a sell sequence |
| 69 | Regime ladder | `CORRECTION` < `EARLY_UPTREND` < `UNDER_PRESSURE` < `CONFIRMED_UPTREND` for every "more conservative" comparison; only the state machine assigns `EARLY_UPTREND`; `EARLY_UPTREND` → `CORRECTION` on score ≤ −1; the 25-session timeout to `UNDER_PRESSURE` removed. Amends #47 / #55. *Why:* the old timeout raised risk on a stalled rally, and "more conservative" wasn't defined between two non-nested states |
| 70 | EP pivot and chase | An EP's pivot is its gap-day opening-range high; EPs are exempt from the 08:40 gap invalidation; a VCP whose opening-range high is above pivot × 1.05 is skipped. *Why:* every EP gaps ≥ 10%, so measuring from a prior level rejected them all, and a trigger above the limit is an invalid order |
| 71 | Split-only bars | `adjustment=split` instead of `all`; historical earnings dates from EDGAR 8-K Item 2.02. *Why:* order levels must be in traded prices, dividend adjustment rewrites history on every ex-date, and Finnhub's free history can't drive 2016+ earnings rules |
| 72 | Partial fills, PDT, dividends | The filled quantity is the position; exits are never withheld for PDT (a PDT-rejected exit keeps its stop and goes OPG next morning); the invariant re-raises stops a broker lowers for a dividend |
| 73 | Scorecard statistics | One unit per setup, labelled with the verdict in force at trigger; BUY vs SKIP on triggered setups; session-block bootstrap; prompt version and answering model tagged; a default-plan counterfactual for every LLM-book trade. Amends #62. *Why:* re-reviews and shared market moves make verdicts far from independent, and the scorecard couldn't see the Portfolio Manager's plan choices |
| 74 | Early LLM evidence | After Phase 4 the evening pipeline stays deployed in `dry_run` (task 4.6, paid, needs approval), so the scorecard runs through Phases 5–7. *Why:* whether the LLM adds value is the core bet; this answers it months sooner |
| 75 | Unproven regime | Live only: a regime with fewer than 5 closed trades (trial + live) runs at the 0.5 multiplier. *Why:* a 3-month trial may see only one regime |
| 76 | Operations | Kill switch is a `system_flags` row with a CLI; operator commands for every "by hand" step; `docs/runbook.md`; an external dead-man's switch; a nightly backup to S3; a separate Alpaca paper account and Neon branch per environment |
| 77 | Build order | Pure guardrails, sizing, triggers and the risk multiplier move to task 3.5b, because the baseline backtest (3.8) compares them on vs off; the regime holdout is opened only in the 3.8 re-run; the skeleton's local run is 3 sessions, then 10 unattended in AWS; a task's branch is merged before the next task starts |
| 78 | Smaller defaults | `UNDER_PRESSURE` VCPs need conviction ≥ 4 (baseline: top quarter by rank); `EARLY_UPTREND` steps from 0.25% / 25% to 0.5% / 50% after 10 sessions (never if `no_ftd`); the rule-precedence wording made unambiguous; tranche 2 respects the earnings blackout; the Analyst can't change the strategy; lessons need ≥ 10 trades or 20 scored setups; the LLM cost estimate corrected to ~$35–65/month (the arithmetic gives $1.45–2.25 per Analyst session) |
| | *Design review 3, 2026-10-08* | |
| 79 | Leaving `EARLY_UPTREND` | `EARLY_UPTREND` → `CORRECTION` only on a rally-low undercut or ≥ 6 distribution days since entering it, not on score ≤ −1; upgrade counters count only sessions in the current state; at least 10 sessions in `EARLY_UPTREND` before `CONFIRMED_UPTREND`. Amends #47, #55, #69. *Why:* the score is usually ≤ −1 on an FTD, so the old rule cancelled most FTDs the next session and made the regime flap; and the `no_ftd` path reached `CONFIRMED_UPTREND` within 3 sessions, skipping the starter limits |
| 80 | Opening-range trigger for pivot strategies | A pivot strategy uses the opening-range high as its trigger whenever the opening range traded above its level, not only when it opened above it. Amends #33, #70. *Why:* a stock that broke its pivot in the first 5 minutes had a trigger below the market at 09:36 and was always skipped, often the strongest breakouts |
| 81 | Limit price and sizing | Limit = min(trigger × (1 + min(2%, ½ × ADR%)), code pivot × 1.05) for every strategy; the 5% max chase is a cap and skip rule, measured from the code pivot. *Why:* sizing every pivot entry on pivot × 1.05 put on only about 50–65% of the planned risk |
| 82 | Leadership-health window | The 20 sessions whose 5-session result is complete (T−24 to T−5). Amends #41 / #46. *Why:* the last 5 sessions' breakouts have no result yet; counting them looks ahead in backtests |
| 83 | Like-for-like baseline comparison | The LLM book is compared with the baseline on its decisions replayed through `sim/` (sim R); paper fills are reported alongside; the plan counterfactual is sim R vs default-plan R. Amends #62 / #73. *Why:* paper fills are optimistic and the baseline is simulated with slippage, which favoured the LLM book |
| 84 | Regime-downgrade tightening | Defined: to `UNDER_PRESSURE` the trail tightens one step; to `CORRECTION` the trail becomes the 10-day and an 8-week hold ends; tranche-2 adds need the strategy to stay fundable, with no crisis override. *Why:* the rule was in the precedence list but never defined |
| 85 | Cushion on an unknown date | For a held position with no known next date, the cushion runs before the estimated ±10-session window. Amends #60. *Why:* the estimate protected new entries but not held positions |
| 86 | Unfilled sells | A sale that must happen (price through the stop) is escalated within the same run: marketable limit, one re-price, then market; still unfilled at the close → OPG next morning. Amends #67. *Why:* "the next order-touching job" could be hours away, or after the close |
| 87 | Heavy-volume reversal | A down day is a close below the prior close; volume is against the 50-day average. *Why:* undefined, and read as "close below the open" it would exit most EPs on their gap day |
| 88 | Leaving drawdown tiers | A drawdown tier also ends after 3 wins in it, stepping up one tier at a time; it re-applies only on a new equity low. Amends #58. *Why:* after the −15% halt, the multiplier stayed at 0.25 until about 24R of net gains |
| 89 | Baseline gate | Go / no-go in task 3.8: triggered top-40 setups under default plans must show positive expectancy after costs (tuning CI excludes zero, holdout positive); a failing strategy is dropped; a failing pool stops the build before Phase 4. *Why:* the LLM filters this pool; if the pool loses money, the filter has to be very good just to break even |
| 90 | Single-trader exit sets | Each strategy's default exits start from one trader's set (Kullamägi: momentum, EP; Minervini: VCP, pullback; O'Neil: emerging leader). Other rules are added only on evidence; the cushion and downgrade tightening are safety rules tested on vs off; the full combination and a floor (stop + trail) are run too. Amends #35, #36, #61. *Why:* nobody traded the combined rule set, and changing one rule at a time against it can't show whether the stacked early exits cut the big winners |
| 91 | Smaller fixes | `auratrade kill on` cancels resting entries at once; the advisory lock is released in `finally`, with a server-side idle timeout as a backstop; the SPY −4% crisis override lasts that session and the next; "no progress by session 20" = high < +1R; the scorecard's trigger window is the same for every verdict; a morning downgrade doesn't fund unreviewed strategies; the evening pipeline from 4.6 replaces the skeleton's `ingest` |
| | *Design review 4, 2026-10-08* | |
| 92 | Momentum extension | `MomentumBreakout` (like EPs) is exempt from the 25%-above-50-day rule; its extension is bounded by its scan and its ADR stop; the backtest compares with and without the rule. Amends #14. *Why:* a stock that doubled in 6 weeks and flagged for 3 sits ~27% above its 50-day at the breakout, so the rule removed most of Kullamägi's setups |
| 93 | `EARLY_UPTREND` step-up | After 10 sessions, 0.5% / 50% applies only on sessions with a score ≥ +1; otherwise the starter limits stay. Amends #78. *Why:* the unconditional step doubled risk on a slowly failing FTD, the same "stalling rewarded" flaw #69 removed |
| 94 | Correction and rally lows; two-sided regime check | Correction low = each index's lowest low since entering `CORRECTION` (a new low restarts the count); rally low = that low, or for `no_ftd` the lowest low of the 10 qualifying sessions. The regime backtest also reports time in `CONFIRMED_UPTREND` during known uptrends, false `CORRECTION` spells and pillar score distributions, and compares an immediate vs a 2-session downgrade to `CORRECTION`. Amends #47, #79. *Why:* the rally low was undefined on the `no_ftd` path and for score-entered corrections, and the acceptance criteria only rewarded being defensive |
| 95 | Pullbacks | Confirmation gate defined per strategy (pullbacks: above the tested MA, volume ≥ average, RS line not at a 50-session low); `LeaderPullback` funded in `CONFIRMED_UPTREND`, and in `UNDER_PRESSURE` only at conviction ≥ 4. Amends #20. *Why:* the breakout gate almost never passes on a pullback, and pullbacks were funded only while the market was under distribution |
| 96 | Heat clipping | A new entry's risk is clipped to the heat room left; rejected only if the room is under half its planned risk. Amends #53. *Why:* rejecting outright left heat unused, so the 1.25 multiplier deployed less total risk (5%) than 1.0 (6%) |
| 97 | Fill model | A buy-stop-limit that jumps past its limit is a miss unless the price comes back to the limit; stops fill with slippage; when a bar reaches both the stop and a favourable level, the stop comes first. Amends #63. *Why:* "capped at the limit" filled the fastest breakouts, which are mostly winners, and live trading misses them |
| 98 | Pre-registered statistics | Before the trial: measured R spread, minimum detectable effect and a non-inferiority margin δ (default 0.1R) in `docs/backtests/power.md`. An inconclusive BUY-vs-SKIP result allows go-live at 0.5 only if the point estimate > 0 and the lower bound > −δ; the full envelope waits for a significant result. Amends #62, #73. *Why:* at realistic trigger rates the BUY-vs-SKIP interval may not exclude zero for over a year, and a bar set too high invites relaxing it afterwards |
| 99 | Point in time and feeds | EDGAR facts keyed on the `filed` date; the surprise kept out of the quality score; the simulator takes opening-range highs and the 15:40 price from IEX minute bars, fills and volume from SIP. Amends #61. *Why:* period-end dates look ahead, surprise history doesn't exist for backtests, and live triggers are set from IEX |
| 100 | Holdout and early warning | 2023+ is labelled a confirmation period, and reports show results per year; a spike-grade daily-bar edge check on the tuning period (task 1.6b) runs before the build goes further. Amends #61, #89. *Why:* the holdout filters about 20 comparisons, so it isn't clean, and the baseline gate came after months of building |
| 101 | Evening chaining | Evening jobs are chained on predecessor success (the times are fallbacks) and fail closed after 30 minutes; the baseline, breakout tracker and scorecard move from `scan` to a `simulate` job before `regime`; every job fits in Lambda's 15 minutes. *Why:* clock-timed jobs could read stale data after a slow ingest, and `scan` was overloaded |
| 102 | Stop guard | A `stop_guard` checks stop existence, quantity and GTC every 5 minutes in market hours (broker first, database only to repair); order jobs use Lambda async retries. Amends #45, #68. *Why:* a job dying mid-sequence could leave shares unprotected until the next scheduled order job, up to about 2 hours |
| 103 | Through the trigger at 09:36 | If the last price is above the trigger but at or below the limit, the entry is a limit buy at the limit; above it, skipped. Amends #51, #80. *Why:* stocks that broke their opening-range high in the minute before 09:36, often the strongest, were always skipped |
| 104 | Smaller fixes | Early-behaviour exits run only when in the position's plan (heavy-volume reversal in the O'Neil set; time stops on evidence or by Portfolio Manager choice); the 8% stop cap is measured from the limit price; the EP stop cap is 1.5 × pre-gap ADR (backtest compares 1 ×), amending #19; "prove it first" restarts only after a manual drawdown-halt reset; third-party text is passed to the LLM as delimited data |
| | *Build-order review, 2026-10-08* | |
| 105 | Design freeze | No further paper design reviews. From here, the design changes only on a spike or backtest finding (CLAUDE.md §5) or at the user's request. *Why:* four reviews produced 54 decisions in two days, and the remaining open questions (order mechanics, data coverage, IEX vs SIP, real token cost) can only be answered empirically |
| 106 | Edge check first | The spike-grade edge check moves from task 1.6b to spike 0.3h, right after the market-data spike, on daily bars pulled straight to Parquet (no database or job framework). Amends #100. *Why:* it needs only daily bars, so it can run before the data layer (1.1–1.6) is built; a clearly negative result, even with survivorship inflating it, then saves that work |
| 107 | Live EP path later | The live `EpisodicPivot` path (08:40 EP candidates, the Portfolio Manager's EP review, EP entries) is built in task 5.6, after the core execution loop and before the AWS deploy. The EP backtest proxy, guardrails and plan rules stay where they were. No behaviour changes; the paper trial still trades every strategy. Amends #64. *Why:* EP has the most special cases and a live rule that can't be backtested, so it shouldn't hold up the core loop |
| | *Public site, 2026-10-08 (user request, allowed under #105)* | |
| 108 | Public project site | A static site (`web/`) in a private S3 bucket behind CloudFront with origin access control, in its own CDK stack (§12); no backend. Later data comes from one nightly, allow-listed `data/snapshot.json` written by `post_market` that publishes **closed trades only**, never open positions, stops or IDs. Pages say "paper trading" and "not financial advice". It pulls forward Phase 9's read-only dashboard (tasks 0.5 and 5.7). *Why:* the user wants a public page with a link. A static site costs about nothing and can't touch the trading system, whereas an API on Neon would wake the free-tier database on every visit and put it on the internet. Publishing open positions would reveal stop levels and read like a signal service |
| | *Spike findings, 2026-10-09 (0.3a, 0.3h; allowed under #105)* | |
| 109 | No IEX opening range | Opening-range entries use the IEX 09:30–09:34 high at 09:36 when IEX traded; otherwise the consolidated high from delayed SIP at 09:51 (a second `execute_entries` run), with the same limit rules. The simulator reproduces the rule from IEX minute history. Amends #22, #99. *Why:* spike 0.3a found no IEX trade in the first 5 minutes on 48% of symbol-days for names with $10–94M daily volume (over half the universe), 15% for the next tier and 0% for the top 100. Skipping them would drop about half the momentum and EP entries in those names |
| 110 | Pre-market gap source | The 08:40 gap step reads 15-minute-delayed SIP pre-market trades (to 08:25), not IEX; EP candidates are screened at ≥ 7% and confirmed ≥ 10% at the open. Amends #44. *Why:* by 08:40 only 17 of 80 sampled ≥ 10% gappers had any IEX pre-market print (10 flagged), against 78 with delayed SIP (54 flagged at ≥ 10%, 64 at ≥ 7%, 4 false alarms in 79 near-misses) |
| 111 | Universe and survivorship | ADRs are in the universe; classification = Nasdaq Trader ETF flag → symbol suffix → name rules → SIC 6770; the backtest universe adds ticker-enumeration snapshots to Alpaca's asset list; zero-volume bars are dropped; securities are keyed by asset id / CUSIP, with long trading gaps splitting a ticker. *Why:* Alpaca's asset API has no type flag; its asset list misses ~20% of the liquid 2016–17 universe although the bars exist; it emits zero-volume bars after delistings; tickers are reused. IBD and Minervini trade ADRs |
| 112 | Edge check: continue | The 0.3h result (both simple setups ≈ 0R before costs, −0.08R after 0.2% a side; 34% delisted names in the universe) does not pause the build. The 3.8 baseline gate must clearly beat this naive reference, with liquidity-scaled slippage. *Why:* the result is borderline, not clearly negative, and regime-shaped (VCP +0.25R in 2017, −0.70R in 2022); the design never trades these setups ungated or without ranking and plan exits |
| | *Spike review 0.4, 2026-10-10 to 10-15 (spikes 0.3a–0.3h and the out-of-order build; allowed under #105)* | |
| 113 | OTO entries and stop legs | Entries are OTOs with a **GTC parent**, so the stop leg is GTC from the start; the EOD review cancels unfilled parents. A leg stays until a sale must cut the quantity; then, under the lock, the legs are cancelled and one standalone GTC stop is placed (about 1 s with no stop, covered by `stop_guard`). The invariant counts a position's GTC stops together. The `fill_watcher` contingency isn't built. Amends #64. *Why:* spike 0.3b: a leg takes the parent's time-in-force (a `day` leg expired at 16:00 and left SOFI unprotected), and its time-in-force and quantity can't be changed after the fill (`422`), only its price. Swapping only when needed means a position that never trims never has a gap |
| 114 | Buys in a held symbol | Tranche 2 and adds go in as OTOs with their own GTC stop leg. *Why:* while a sell stop rests, Alpaca rejects a plain buy as a "potential wash trade" and accepts an OTO; cancelling the stop to buy would leave the whole position unprotected while the buy works |
| 115 | Partial entry fills | A partly filled buy counts as unprotected: the next order job or `stop_guard` cancels the rest, keeps the leg if it became an active GTC stop for the filled shares, and otherwise places a standalone stop. Amends #72. *Why:* the leg stays `held` (inactive) while the parent is only partly filled, and the old rule cancelled the rest only at the EOD review, hours later. What cancelling a partly filled parent does to its leg is untested (paper fills partly only at random); the rule works either way |
| 116 | Splits and dividends | On a split's ex-date Alpaca rejects resting stops at 04:00 ET; `execute_open` places a scaled stop at 09:20 (09:31 if refused), every invariant repair checks the corporate-actions feed first, and reconciliation compares quantities scaled by the ratio and alerts. Dividends need nothing new (#72). *Why:* spike 0.3b: paper never applied DXJ's 3:1 split to the position, and an unadjusted position against an unscaled plan stop would read as "price through the stop" and be sold; QDTE's stop wasn't lowered on its ex-date |
| 117 | Evening VIX | CBOE's delayed-quote JSON (accepted if `last_trade_time` ≥ 16:15 on T), then the history file (by its last row's date), then FRED; backtests use the history files filtered to sessions. `premarket_check` uses settled closes only and may only downgrade. The credit spread is LLM context only. *Why:* spike 0.3d: the JSON was final at 16:32 ET and matched the history row, which itself updates only at 18:30–21:30 ET, after `regime`; FRED lags a full session; overnight VIX values can't be backtested; the spread series has only 3 years |
| 118 | Daily-bar volume | The evening ingest re-fetches session T−1's bar. *Why:* spike 0.3a: the volume of bars fetched at 16:58 grew later for 187 of 195 symbols, by up to 62% |
| 119 | Earnings timing | Historical timing = the earlier of the 8-K Item 2.02 acceptance and the first earnings headline; upcoming timing falls back to the company's usual timing over its last 8 reports, else before the open. Amends #71. *Why:* spike 0.3e: Finnhub's free calendar has no past dates and leaves timing blank for 61% of upcoming reports; 8-K timing matched news in 96% of events, and morning releases filed later in the day need the headline |
| 120 | Decision price | The last IEX trade if ≤ 2 minutes old, else the IEX quote midpoint if ≤ 1 minute old with a sane spread, else no price (skip entries; a soft exit waits, the hard stop rests). *Why:* spike 0.3a: outside the 500 most liquid names the last IEX trade is a median 4 minutes old (p90 66 minutes), while IEX quotes stay fresh (median 6 s) |
| 121 | LLM cost | About $17–22/month (Analyst about $0.016 a request); alerts above $1.00 an Analyst batch and $0.50 a real-time call; effort is chosen on quality. Amends #65, #78. *Why:* spike 0.3g: requests are about 4,000 input tokens (the README assumed 8,000), thinking is short, and `high` costs about the same as `medium` |
| 122 | Account size and whole shares | $5,000 for the paper trial and live (confirmed 2026-10-09). Shares are floored; 0 shares → skipped; under 2 → a single tranche. The simulator and backtests size in whole shares at the real account size. *Why:* GTC stops can't be fractional, so high-priced leaders get 0–1 shares at $25–50 risk; on the edge-check trades this skips 1–10% depending on risk, puts on 85–90% of the planned risk, and the skipped trades don't differ in expectancy |
| 123 | No PDT rule | The PDT allowance is removed (§5.6 input, §5.7, §8); entries never use margin instead. Amends #12, #72. *Why:* FINRA's Rule 4210 amendment (effective 2026-06-04) replaced the pattern-day-trader test and the $25,000 minimum with an intraday margin standard; spike 0.3b ran six same-day round trips with none blocked, and the account API has no PDT fields. Live behaviour is confirmed before Phase 8 |
| 124 | No OPG exits | Nothing is sold before the open. Planned morning exits and trims, and sales left unfilled at the last close, run at the 09:31 check run through the sell sequence with a marketable limit. Amends #54, #67, #72, #86. *Why:* spike 0.3b: on a closed market a stop replace stays `pending_replace` and keeps every share reserved, so the OPG sale is rejected (`403`) and the stop can't be restored (`422`); OPG orders are also accepted only from 19:00 to 09:28. Approved by the user on 2026-10-10 |
| 125 | Recorded as built | `days_until_next_open` (calendar days) for long weekends, since in sessions the next open is always 1 away (PR #9); the climax setting `fast_gain_min_move_sessions = 15` is provisional, chosen by 3.8; a new database starts with entries disabled until `auratrade kill off`; tasks 2.1–2.3, 3.1 and 3.5a were built before Phase 1 finished, with approval (PRs #12–15); testing gates in the plan: a real-data smoke run (1.6), a fake broker with automated chaos tests before anything runs unattended (1.7), and CI coverage floors (95% overall, 100% on `guardrails/` from 3.5b) |
| 126 | Base count and bars | The count also resets when a low between two bases undercuts the prior base's low, or a base fails (decline > 50%); the final contraction's high is the pivot only in the upper half of the base's range; a count is reliable only from a real IPO in the history or a reset seen in the data, and an unreliable count is sized like a 3rd base (half risk); ingest filters outlier highs and lows and the count never trusts a single-bar extreme; the listing date is the first public trading date; `bases` rows are kept forever. *Why:* the 2.2 build: a crash that recovered into a higher base kept counting up; NVDA's bad 195.95 tick created a false failed base; CELH's OTC uplisting isn't an IPO; the count carries forward past the 2-year bar window |
| 127 | Regime readings | "50 SMA rising" = above its value 10 sessions earlier; VIX/VIX3M is the latest value from any source, and the crisis override is set (with an alert) when every source is more than 2 sessions stale, accepting that one stale day can count one observation twice; expiration flags = quarterly third Fridays (or the session before a holiday) plus S&P 500 and Nasdaq-100 rebalances, counted by default (2.4 decides); start in `CORRECTION`, at least 252 sessions before the first label needed; one rally attempt across both indexes, each with its own low; `EARLY_UPTREND` counts distribution days since entering it; the label at t governs t+1; a downgrade beats an upgrade and an FTD beats the `no_ftd` fallback; an `EARLY_UPTREND` LLM view fails closed to `CORRECTION` when the computed regime isn't `EARLY_UPTREND`; recomputing a row keeps its stored LLM view. *Why:* the 2.3 build needed each of these defined; details in PR #15 |
| 128 | Plan readings | The exhaustion gap opens ≥ 1% above the prior high; the "10-day" trail is a 10-session SMA; a downgrade to `UNDER_PRESSURE` tightens an active 8-week hold's trail one step (to `CORRECTION` the hold ends); the late-stage sale ranks with the climax sales, fires once and isn't suppressed by a hold; provisional menu values Kullamägi ⅓ at session 3, Minervini ⅓ at +2R, O'Neil ¼ at +20% (3.8 chooses); the 3.5a readings (entry session = session 1; breakeven at +3R and the round-trip rule read the highest high, other rules the decision price; partials fire once; a close below the pivot in the first 20 sessions holds and blocks tranche 2; a stop raised through the price exits as `HARD_STOP`; unknown earnings timing counts as before the open; the cushion with no known date runs at last report + 52; a session's changes fold into one plan version). *Why:* the 3.5a build needed each of these defined; details in PR #14 |
| | *Task 1.5 build, 2026-10-15 (recorded as built and approved by the user; live-checked on a scratch database)* | |
| 129 | Ingest readings | Bars are stored for a tracked set (universe-type tickers passing ½ × the price and dollar-volume filters, plus the index ETFs; a newly passing ticker gets its whole window, one that stops passing is dropped an evening later); a split is detected by the re-fetched overlap bar's close moving > 2%, then the window is re-fetched; an outlier lies beyond the body and the neighbours by more than max(15%, 3 × the 20-session average daily range), keeps the body's edge and is logged in `bar_corrections`; coverage counts tickers that traded at T−1 plus the index ETFs, and a missing index alone is `INCOMPLETE`; T's bars are refused before the close + the SIP delay; the evening VIX quote is final 15 minutes after the session's close (13:15 on half days); EDGAR fund filings (N-CSR, N-PORT) make a closed-end fund named like a stock a fund, and EDGAR's SPAC/fund verdict stands over the name rules; the listing date is the first stored bar inside the window unless EDGAR shows earlier periodic reports; industry groups are SIC prefixes named "SIC nnn", the most specific wins; earnings: a closed-day release counts as before the next open, a tie in the usual timing as before the open; per run ≤ 150 surprise requests and ≤ 200 company-facts downloads, 12 quarters kept; up to 4 concurrent requests per vendor under its rate limit; calendar, universe and bars fail the job, the other steps degrade with an alert; more than 50 securities leaving the asset list in one evening is a vendor glitch (fail closed). *Why:* the 1.5 build needed each defined, and the live run (2,761 tickers, 2 years) showed the flat 15% outlier rule clipping real moves of volatile names (241 corrections; 64 at 3 × range), closed-end funds passing as stocks, and a 17-minute nightly run bound by vendor response times (219 s after the change); details in PR #27 |
| 130 | Backfill readings | The Parquet history keys securities as ingest does and adds two splits of its own: a one-day close move of 4× or ¼× starts a new security, and two securities whose bars are identical on ≥ 90% of their sessions are one renamed company (the active one is kept, then the one with an asset id, then the longer history). CIKs come from SEC's current ticker map, so only securities still trading get EDGAR earnings and fundamentals, and backtests report the gap. Retention merges each session leaving Postgres into the backfill's session file rather than writing its own. The code lives in a new `history/` package beside `ingest/`, sharing its rules (bars, outliers, earnings timing, quarters). *Why:* the 1.6 smoke run over the real history (13,672 securities, 16.3M bars since 2016) found 860 old tickers repeating their successor's bars (PKI/RVTY, TASR/AXON), which double-counted stocks in the universe, and level jumps of up to 4× from reorganizations and unadjusted reverse splits, which broke the base count and RS; SEC's map has no historical tickers; a retention file of its own would have dropped the delisted securities Postgres never held |
| | *Task 1.7 build, 2026-10-21 (approved by the user)* | |
| 131 | Skeleton and order-layer readings | The decision price's "sane spread" (#120) is at most 2% of the quote midpoint (provisional); a marketable sell limit sits 0.5% below the decision price; selling at once waits 30 s per step (limit, one re-price, market); an order call with no answer (5xx, dropped connection) is retried up to 3 times with the same client_order_id, after looking the order up; the order lock alerts after 2 minutes of waiting and fails the job after 8, and Postgres ends an idle lock connection after 10 (`idle_session_timeout`). The walking skeleton's trades carry `source = SKELETON` (strategy label `VCP_BREAKOUT`), left out of "your numbers" and the go-live statistics; the skeleton ignores the regime, writes 10 ranked signals a night and enters the top 3 that size to at least one share, with at most 6 positions. `simulate` and `analyst_submit` are placeholder jobs until 3.7 and 4.4, so the evening chain runs whole. `stop_guard` takes the broker's clock as its calendar guard and is recorded in `job_runs` only when it repairs or fails. A split on a held symbol (from `corporate_actions`) scales the plan, places the stop for the broker's actual quantity and opens a `CORPORATE_ACTION` mismatch. A position with no plan is a mismatch and an alert, and its orders are left alone (code never invents a stop level). The invariant cancels a working sale and stops over-covering a position, under the lock: both mean a job died mid-sequence. `auratrade mismatches` and `resolve` exist from 1.7, since reconciliation opens mismatches that block entries. *Why:* the 1.7 build needed each of these defined; the chaos tests and the live paper runs exercise them; details in PR #30 |
| 132 | 1.7's live acceptance moves to AWS | The rest of 1.7's live acceptance (3 paper sessions and the hand check of a job killed right after reducing a stop) runs on the AWS deploy of task 1.8, against the `paper` Alpaca account and the Neon `paper` branch, instead of on this PC against `dev`. 1.7 is ticked when they pass, and they count toward 1.8's 10 unattended sessions. Until then the local scheduler keeps running on `dev`, with nothing depending on it. *Why:* on 2026-10-21 the PC was on a phone hotspot and couldn't be relied on to stay connected through a session; the automated chaos suite already covers the order paths in CI, and the deployed skeleton is where the long record comes from anyway (approved by the user) |
| 133 | AWS skeleton readings | One schedule module (`jobs/schedule.py`) feeds the AWS schedules, the watchdog and the local stand-in. A chained job's fallback fires every 10 minutes up to its deadline. `eod_review` fires at 12:40 and 15:40 every weekday and runs only within 30 minutes before that session's close. The watchdog reports a heartbeat missing 15 minutes past its slot (a chained job's: past its deadline), a run RUNNING for 20 minutes, and any disabled schedule of its environment (how `stop_guard` is watched); each finding is alerted once per session, and every run pings the dead-man's switch. One container image for all functions; order jobs get 15 minutes and 2 async retries, the rest none; events older than an hour are dropped; the scheduler retries only a failed start. Secrets are SSM SecureStrings read at function start, failing closed when none are found; the trading mode is a deploy-time stack setting (default `dry_run`, `live` refused), never a parameter. Each evening job may invoke only its successor; one private bucket per environment, kept on stack deletion, reachable only by `post_market` and `ingest`. *Why:* the 1.8 build needed each of these defined; a scheduler can't follow the calendar, so the half-day rule moves into the job; `stop_guard` writes no heartbeat to watch (approved by the user 2026-10-22) |
| 134 | Public site: simple now, showcase at 5.7 | The current explainer page (0.5) is deployed in 1.8 as it is, with its build-status strip brought up to date, on the CloudFront address (a domain can be added any time). Task 5.7 becomes a designed showcase site: the LLM-vs-baseline results, the verdict scorecard, closed trades with the Analyst's thesis next to the outcome, the regime timeline, optionally a published hash of each night's snapshot so the history can be checked for edits. The architecture stays as #108: static files and one nightly, allow-listed, one-way snapshot; no API, no backend, no open positions. Before live money (Phase 8) the AWS account is hardened. *Why:* the user wants a site that does the project justice and stays secure when real money is connected; a big design pass now would show empty panels, and a dynamic site would open a path toward the trading data for little gain. (User, 2026-10-22.) |
| 135 | Regime thresholds after the 2.4 backtest | Provisional 4-pillar thresholds until the 3.8 re-run: `CONFIRMED_UPTREND` from +2 (debounce at +1; the `no_ftd` fallback also at +2), `CORRECTION` at ≤ −2, the distribution pillar at −1 from 8 days in 25 sessions (`EARLY_UPTREND` still ends on 6 since entering it). `transitions.correction_sessions` (default 1) lets the backtest compare a 2-session downgrade. The switch limit (≤ ~12 a year) counts the two switches of each failed follow-through day apart. The remaining misses are written down, not tuned away: `CONFIRMED_UPTREND` holds 83% / 49% / 55% of 2017 / 2019 / H2 2020–21 against a pre-registered 60%, and the busiest year has 18 switches besides failed FTDs. *Why:* IBD's published values failed 4 of 5 pre-registered checks on 2016–2022: `CORRECTION` 38% of the time, 7 false `CORRECTION` spells, 82 sessions to `CONFIRMED_UPTREND` after the 2020 low; one pillar at −1 was enough with leadership health absent, and ETF volume put the distribution pillar at −1 on 36% of sessions. The adopted set keeps all three declines early, passes the 2020 recovery and leaves 1 false spell (user's choice of option A, 2026-10-23; `docs/backtests/regime.md`) |
| 136 | Strategy-scan readings (3.2a) | *Approved by the user 2026-10-23.* The scans' own rules (§5.3 "How the scans measure it"), all in `risk.yaml` `strategies:` and provisional until 3.8. **VCP:** contractions from the base's extremes (not 2.2's 3% zigzag, which finds 30-50 swings in a long volatile base), so each is shallower than the last by construction; a base of 15-325 sessions (3-65 weeks); the first contraction 5–35% (the 5% floor was added after the real-data run: acquisition targets pinned at the deal price passed as VCPs with first contractions under 5%, and the rule-only baseline would have bought them), the final ≤ 10%, in the upper half of the base; a high needs 3 sessions after it; volume "dries up" = the final contraction's average ≤ the 50-day average. **Momentum:** "top ~2%" = a percentile ≥ 98 on any of the 1-, 3- and 6-month returns; the move ≥ 30% within the 63 sessions before the consolidation high; "higher lows and a tightening range" compares the consolidation's two halves; "riding" = the close at or above a 10- or 20-day SMA that is above its value 5 sessions earlier. **EP proxy:** the gap session itself (≥ 10% on ≥ 3× the prior 50-session volume); pivot = the gap day's open, structural stop = the prior close; the earnings reaction session and the prior 3- and 6-month moves are facts for the ranking, not filters. Each scan's **structural stop** is the floor for the Analyst's stop (momentum and EP stops are set at 09:36 from the low of the day, never below it). The interface's `system_prompt` holds each setup's definition; 4.2 writes and versions the prompts. Real-data run, 2017-2022: `docs/backtests/scans.md` (0 exceptions; VCP 16, momentum 3, EP 1 setups a median session). Momentum consolidations get no depth cap for now (31% pull back more than 25%); 3.8 reports results by depth |
| 137 | Strategy-scan readings (3.2b) | *The user's choices, 2026-10-23.* **`EmergingLeader`'s "smallest drawdown during the correction":** the stock's largest close-to-close fall from a peak, from 21 sessions before the regime's latest `CORRECTION` spell to its end, is at most 1.5x SPY's over the same sessions (O'Neil: leaders correct about 1.5-2.5x the market); chosen over a cross-sectional rank, which always passes a share of the universe even after a crash. The spell counts for 63 sessions after it ends; a new high (price or RS line) is the highest of 252 sessions; pivot = the 52-week high; structural stop = the **5-session** low (the 10-session low was more than 8% below the pivot on 69% of setups; the 5-session low on 46%). **`LeaderPullback`:** from the highest high of the last 10 sessions, at least 2 sessions and **at least 2 x ATR deep** (without it the scan found 80 a median session, now 45); the low within 1 x ATR(14) of the 10- or 21-day EMA (the shorter is the MA tested); the pullback's volume at or below the 50-day average; trigger = the session's high; structural stop = the lower of the pullback low and the tested EMA. Real-data run: `docs/backtests/scans.md` |
| 138 | Ranking, the cap and the verdict cache (3.3) | *The user's choices, 2026-10-23.* The quality score is the **plain average** of its eight components, each scaled to 0-1 (§5.3 "How the score is built"), with no weights to fit; a component without data scores 0.5. **One slot per ticker**, its best-scoring fundable setup. The fundable strategies **take turns** for the 40 slots (each round, every strategy's best remaining setup, in score order); with one ranking, pullbacks took 81% of them in `CONFIRMED_UPTREND`. The catalyst is the share of: an earnings gap-up ≥ 5% on ≥ 1.5× volume in the last 63 sessions, quarterly EPS growth ≥ 25% (O'Neil's C), accelerating EPS. Tightness (stop width ÷ ADR) is ranked within the session's pool. The verdict cache keys a setup on its pivot to the cent (the tested EMA for a pullback) and re-reviews a verdict older than 3 sessions. Real-data run (`docs/backtests/scans.md`): about 72 eligible for 40 slots in the uptrends; the cache sends 61% of the chosen to the Analyst |
| 139 | Chart readings (3.4) | The weekly chart's averages are the 10- and 40-week SMAs (IBD's weekly lines, the 50- and 200-day equivalents; the README named only the daily ones). A week is one ISO calendar week, dated by its last session. Every average is computed over the whole history passed in, so the 200-day is defined from the chart's first bar. Prices on the left axis and short, unrotated date labels: at 800×600 and 600×400 a right-hand axis and rotated dates were clipped. `mplfinance` (with matplotlib) moves from the spikes group to the main dependencies, so the Lambda image grows; `MPLCONFIGDIR` points at `/tmp` there, the only writable place |
| 140 | Guardrail, sizing and multiplier readings (3.5b) | *Approved by the user 2026-10-23.* **Stop width:** the percentage caps are measured from the limit and the ATR / ADR caps from the trigger (§7.1); "keep losses small" caps the width at ½ × the strategy's average winning gain over its last 20 closed trades, measured from the same reference, and its noise-floor check applies only once the strategy has 20 closed trades; a window with no winner reads as a zero average gain, so the entry is rejected (fail closed). Structural stops are checked when the Portfolio Manager proposes (on the limit with the trigger at the pivot) and again at 09:36 on the real trigger and limit; a momentum / EP low-of-day stop is checked only at 09:36, never sits below the setup's invalidation level, and is rejected, not tightened, when wider than its cap. **Approval at proposal time** (`guardrails.evaluate`): checks run in a fixed order and the first rejection ends them; each approval counts against the heat, exposure, positions and clusters of the next. Exposure is reserved on the planned limit and the proposed stop, within the 25% position cap (a position-size clip keeps the approved risk, so heat counts the full planned risk); the 09:36 size never exceeds the reservation; an entry past the regime's exposure or equity is rejected. An entry in a symbol already held or pending is rejected (adds go through tranche 2). **Clusters:** a new entry is rejected when it is linked (same industry group, or a 60-session return correlation ≥ 0.7) to 2 positions held, pending or approved; a correlation that can't be computed counts as linked. **Earnings blackout:** a known report 0–5 sessions from the entry session; with no known date, 10 sessions either side of last + 63 (after that window, no blackout until a date is known). **Tranche 2** also needs the entry's conviction to clear the effective regime's bar, and every entry block (including a data-integrity failure) stops it. Tranche 1 is ⌊50%⌋ of the planned shares (at least 1). **Multiplier:** within a day the closed trades count first (against the row in force since the day before), then the equity; a tier's wins count only while it is the row in force; a recovery past a tier's threshold resets it, even one ended by wins; a new equity high also clears the losing-streak count; "a new high in the last 5 sessions" includes today; "positive expectancy" is a positive sum of the last 10 trades' R. **Your numbers:** a winner is a trade with a gain above 0. The skeleton keeps its own sizing until Phase 5 replaces it. *Why:* the README states each rule but not these edges; each choice fails closed or follows the README's literal wording |
| 141 | Simulator readings (3.6) | *Approved by the user 2026-10-24.* The fill model of §5.12 in `sim/fills.py`, used by the fake broker (`play`) and by `sim/trade.py`, which plays one approved entry forward through the live 09:36 order check, `new_plan`, `plans.resolve` and the tranche-2 gate and sizing. **Slippage** (`risk.yaml` `sim:`, provisional until 3.8): a fraction of the price by the 50-session average dollar volume: 0.05% from $500M a day, 0.10% from $100M, 0.20% from $25M, 0.30% below (the universe floor is $10M; an unknown volume takes the thinnest tier). The same fraction applies to entry fills, stops and soft-rule exits; fills round to the tick against the trader. **Entries:** an unfilled entry stops working at the decision time, when the EOD review cancels it (§5.7), not at the close; a buy that jumps past its limit fills at the limit only on a later bar, even if the jumping bar itself traded back under it; a limit buy placed past the trigger fills at the next bar's open plus slippage when that is within the limit, else rests the same way; the opening-range entry without IEX prints goes in at 09:51 on SIP's range (#109), and a pivot entry without them keeps the pivot. **Prices:** without a fresh IEX trade (≤ 2 minutes) at 09:36 or the decision time, the last SIP trade stands in for the IEX quote midpoint, which has no history; with neither, the session runs only the hard stop (a soft exit waits, §7.3). **Same-bar order:** a bar that fills the entry can also stop it out; a session runs the hard stop up to the decision time, then the plan's rules, then the hard stop at its new level to the close. Stop-raising milestones apply at the decision time (live, the intraday monitor raises them when reached). **Tranche 2:** a marketable limit buy at the decision price × (1 + `marketable_limit_slippage`), sized on that limit within the planned risk and size, filling at the decision price plus slippage. **R** is the trade's P&L over the dollars put at risk on every buy (shares × (fill − initial stop)); the scorecard's R after 5, 10 and 20 sessions reads the mark at each session's close. Orders fill whole (no partial fills). *Why:* the README fixes the fill model's shape but not these numbers and edges; they all lean against the strategy, so the 3.8 gate is not flattered |
| 142 | Watchlist, baseline, tracker and scorecard readings (3.7) | *Approved by the user 2026-10-24.* **Watchlist:** `scan` writes session T's full, uncapped scan to `daily_watchlist` after the skeleton's signals are committed (a failure fails the run without taking the skeleton's entries away): every setup of every strategy, its score, and `rank` = its place in the capped list of T's computed regime. Inputs are Postgres's 2-year window; RS ratings and momentum ranks are ranked within each session's universe rebuilt from the loaded bars (the same filters as `indicators.universe`); the group RS rank uses T's ratings; the base count stays neutral until the `bases` table is kept current (no task does that yet: open item); the 14-session ATR is stored for the guardrails. **Breakout tracker:** a trigger is a setup of T-1's scan (every strategy, pullbacks included, whatever the regime or cap) whose session-T daily high reaches its code pivot; a gap above it counts; one row per (ticker, strategy, trigger session). The result is the close 5 sessions later above the pivot, rescaled for a split since; a symbol without a bar then is judged on its last close (the trigger close if it has none), so every result completes and the pillar never refuses a window; results catch up after a missed evening, triggers don't. The `regime` job now reads it: leadership health starts scoring once 10 complete breakouts fall in its window (about 5 weeks after the first evening), on the provisional 4-pillar thresholds (#135) until the 3.8 re-run sets them on 5. **Baseline book:** decided in `simulate` at T from T-1's capped list on T-1's computed regime (no LLM view): every listed setup proposed in rank order at its code pivot and structural stop (never tightened: a stop wider than its cap is rejected), the default exit plan and the effective risk; "conviction >= 4" = the top quarter of the list by score, rounded up (the rest 3); no EPs until the morning candidate list (5.6); every proposal is stored, a rejection with its finding. Its own account: $5,000 (`baseline.start_equity`) from its first evening, "prove it first" at the start, its own multiplier replayed over its days, its own strategy records; entry blocks: a half day, the crisis override and its own daily, weekly and drawdown halts (the drawdown halt isn't latched: nobody re-enables a shadow book). Trades are re-played each evening from their entry session (minute bars cached in S3, `sim/minute_bars/feed=<feed>/<session>.parquet`), so a re-run writes the same rows. **Simulator sessions:** the decision time is 20 minutes before the close (15:40, 12:40); the day's full volume stands in for the 15:40 projection (no pace history); a regime downgrade between the two evenings before a session reaches its review (not the entry session's); tranche 2 needs the strategy fundable in that regime, no crisis override and a full day; the earnings cushion runs before a known report or the estimated window; the RS line is close / SPY. **Scorecard:** a unit is (ticker, strategy, structure), the verdict cache's key (the pivot to the cent; a pullback's tested EMA); a verdict is in force on the trading sessions whose previous evening it was still cached (at most 3 sessions old: the 4 sessions after the review), the same for every verdict; an attempt is made only on a session whose previous evening still listed the setup; triggered = the simulated entry filled (a miss or a 09:36 skip is an attempt); each unit is sized on a notional $100,000 at 1% (R doesn't depend on the size; whole shares would distort it) with no strategy record, and followed to 20 sessions or its exit; intervals resample whole trigger sessions (2,000 draws, 90%, nearest rank). **Plan counterfactual:** default-plan R keeps the decided pivot and stop and swaps the exit plan and risk for the defaults. **AWS:** `simulate` 1 GB, 10 minutes, with the bucket; `scan` 15 minutes. Measured on `dev` (10-20, from this PC): the full scan loads 2,741 symbols in 32 s and scans them in 8 s (80 setups); the baseline evening took 11 s. *Why:* the README sets what each piece does but not these edges; each choice fails closed, follows the live code, or keeps the comparison like for like |
| 143 | The EP proxy at 09:36 (3.8 diagnostics) | *Approved by the user 2026-10-24 (option A).* The 3.8 diagnostics (`docs/backtests/baseline-diagnostics.md`) found the EP proxy looked ahead: it admitted a gap on the gap session's full-day volume and ranked it on that session's close, while the pool entered it at 09:36 the same morning. The backtest now trades the proxy's **09:36 view**: the gap at the open and ≥ 25% of the prior 50-session average daily volume traded from the open to the entry (`min_opening_volume_ratio`, SIP minute bars), with the ranking features, price and ATR from the prior close (`EpisodicPivot.morning`, `backtest.pool.confirm_morning_eps`). The after-close view (≥ 3× full-day volume) stays for the evening scan and the breakout tracker, so the regime is unchanged. The threshold was fixed before the run (`docs/backtests/baseline-diagnostics-preregistration.md`). Live EPs stay task 5.6, where 09:36 volume is only IEX's or 15 minutes delayed. *Why:* the only strategy that looked positive (+0.23R) rested on information the entry couldn't have had |
| | *Strategy decision after the failed 3.8 gate, 2026-10-25 (the user's answers)* | |
| 144 | The entry at the close: two pre-registered candidates | *Approved by the user 2026-10-25, with the pre-registration.* **Result (2026-10-25, `docs/backtests/close-entry.md`): nothing passes.** The pool -0.30R (-0.39 to -0.20) over 1,003 trades; EP at the close -0.12R (276 trades) against +0.21R at 09:36; every strategy below its 09:36 reference. By the pre-registered rule: no further runs on 2017-2022, the holdout stays closed, the 09:36 table stays the spec, and the fallback F applies.  **The change:** one new entry mechanism is tested on 2017–2022, once: confirmation at the 15:40 decision price, then an OTO marketable limit (the decision price × 1.005) with its GTC stop leg, one tranche (§7.1 "Candidate"). **Candidate 1:** `EpisodicPivot` bought on the gap session if the gap held (price ≥ the open, upper half of the range, volume ≥ 3 × average), stopped at max(the gap session's low, the prior close), at most 8% below the limit. **Candidate 2:** the four scan strategies bought on the trigger session if their confirmation gate passes (momentum takes the breakout gate), stopped at max(structural, the trigger session's low), under each strategy's cap measured from the limit. **Two comparisons:** the gate's volume condition off; the structural stop (candidate 2 only). **Additions to the gate (§15):** positive in ≥ 4 of 6 tuning years and ≥ 0 without the best year; ≥ 300 closed trades; a 21-session-block interval reported. **A decision-price check:** a 200-attempt minute-bar sample of the gate at 15:40 vs the close; more than 15% disagreement and nothing is adopted. **Fixed in advance:** a strategy with a positive estimate that fails is not funded (scored in `dry_run`; reconsidered after 100 triggered setups on the scorecard). If nothing passes, no more runs on 2017–2022 and the fallback is the LLM-as-filter test (F): a BUY-vs-SKIP bar on the scorecard, pre-registered before 4.6. **Live, for entries only:** without an IEX decision price, the reference falls back to the delayed SIP last trade, the limit bounding the fill; a limit-on-close order is rejected because Alpaca won't attach a stop leg to it (CLAUDE.md §3.5). The ranking's tightness component stays (#138). Option C (VCP filters found after the fact) is dropped. *Why:* 469 trades were stopped on the entry session and stops under 2–3% lost most; the 09:36 entries take the side of the documented daytime reversal (Akbas et al. 2022), and an EP's 09:36 stop sits inside the gap day's noise by construction. The close entry pays the day's move in exchange, so the result is uncertain (the review puts a pass at about 1 in 3). Details: `docs/strategy-review-2026-10-25.md`, `docs/backtests/close-entry-preregistration.md` |
| 145 | The baseline gate moves from "before Phase 4" to "before capital" | Phase 4 (the LLM layer, the evening pipeline and the verdict scorecard in `dry_run`) is built while the close entry is tested, on separate branches. The gate now blocks capital: LLM entries placed with the broker (Phase 5's live entry path) and the paper trial. 4.5 and 4.6 still need the user's OK with a cost estimate (about $20–40 a month once deployed). Verdicts don't depend on the entry policy (BUY/WATCH/SKIP on the evening scan), so the scorecard re-scores them under whichever entry is adopted. *Why:* the forward scorecard is the only out-of-sample evidence on whether the LLM adds value, and it needs a year or more of setups (§15 "The sample problem"); waiting for the gate delayed it while saving only the dry-run token cost (user, 2026-10-25) |
| | *Strategy change, 2026-10-25 (the user)* | |
| 146 | From chart-pattern setups to a momentum book | *Decided by the user 2026-10-25.* The system stops trading the five chart-pattern setups and becomes a relative-strength momentum book: a small number of the strongest US stocks by intermediate-term return, rebalanced on a schedule, each with a protective stop at the broker. **Kept:** the LLM decides within code's list and price levels, code enforces limits (#2); every CLAUDE.md §3 hard rule; $5,000 paper in whole shares; free data tiers; the budget; validation by a pre-registered backtest on 2017-2022 and the 2023+ holdout once (still unopened). **Superseded:** the setup pool as the candidate list, the 09:36 and close entries, the per-trader exit menus, fallback F (#144). The rules, the pre-registration, the README rewrite (§1, §5, §7, §8, §15) and the re-planned Phases 4-7 come from a separate planning step; no task after 3.8 starts before it is approved. *Why:* three tests of the setups were negative (0.3h about 0R before costs; 3.8 -0.11R; the close entry -0.30R); the losses come from the entries, small winners and costs, not from the code or the account size; momentum is the best-documented anomaly, turns over slowly and needs only daily bars. Details: `docs/strategy-change-momentum.md` |
| | *The momentum book, 2026-10-25 (decided under the user's delegation)* | |
| 147 | The momentum book's rules | *Approved 2026-10-25 (the user delegated the open choices: "decide for me; as profitable as you can"; `docs/momentum-plan.md` §10).* **Ranked set:** the point-in-time universe narrowed to the 1,000 with the highest 50-session average raw dollar volume, ≥ 253 bars. **Signal:** 12-1 (close 21 bars back ÷ close 252 bars back − 1). **Book:** 8 equal slots (equity × 100% ÷ 8), whole shares, no resizing. **Rebalance:** ranked on the last session of each month, traded at 15:40 on the next session (half days move it), sells then buys; a buy blocked by a halt or missing price retried once. **Hold zone:** kept while in the top decile (percentile ≥ 90), sold at the rebalance below it or out of the ranked set. **Buys:** only from the first 16 eligible names (not held, a price, ≥ 1 share fits, ≤ 2 per cluster); the LLM picks among them at 1 or ½ slot, the rule book takes them in rank order. **Stop:** GTC at 80% of the entry decision price, raised to 80% of each rebalance's decision price, never lowered; after a stop-out the slot is cash until the next rebalance. **Overlay:** effective regime `CORRECTION` or the crisis override blocks buys, never forces sells. **Earnings:** held and bought through; the LLM may step aside. The setup scans stay as facts for the Book Analyst. The setup system's spec is archived (`docs/archive/README-setup-system.md`). *Why:* each rule's source is in §5.3, §7 and `docs/momentum-plan.md` §2; where the sources disagree the plan chose a primary and pre-registered the alternative (C1-C7, #148) |
| 148 | The book's risk limits, LLM roles and pre-registered backtest | *Approved 2026-10-25, as #147.* **Risk:** the setup system's R-based limits (1.5% a trade, 6% heat, the progressive-exposure multiplier) don't apply to the book: about 2.5% of equity per position to its stop, about 20% for a full book; limits are the slot, 8 positions, 100% exposure, the cluster cap, tighten-only; the drawdown halt is −30% (buys only, manual re-enable; the setups' −15% stays in `halts:` for their code); half slots at the start of the trial and of live trading. **LLM:** a monthly Book Analyst batch over the top 40 and the holdings, a Portfolio Manager on execution and event days (deadline 12:00), a weekly review; about $3-8 a month. **Validation** (`docs/backtests/momentum-preregistration.md`): tuning 2017-2022, the holdout once for the frozen configuration; the gate G1 excess CAGR over SPY > 0, G2 over MTUM ≥ 0, G3 positive in ≥ 3 of 6 years and without the best year, G4 CAGR ÷ max drawdown ≥ SPY's and max drawdown ≤ 45%; seven one-change comparisons judged on tuning only (a 90% block-bootstrap interval wholly in the variant's favour, no deeper drawdown); the holdout never chooses between them. *Why:* a 20% catastrophe stop can't live under a 6% heat cap; a −15% halt would stop a momentum book in most bad years; 6 years can't make a 2-5% edge significant, so the gate uses point estimates and robustness, and the holdout is kept clean for the one test |
| 149 | The re-plan from 3.8 on | Tasks 3.9-3.16 build and run the book's backtest (spec, ranking, guardrails, portfolio simulation, backtest and report, blind check, the one scored run, the holdout on a pass). Phase 4 (the LLM layer, new schemas) starts **after the tuning gate passes**; its rule book and scorecard replace the setup baseline (4.5); the sanity set (4.6) and the dry-run deploy (4.7) need the user's OK with a cost estimate. Phase 5 builds `execute_close` and drops the 09:36 entries, tranches, the intraday monitor and the live EP path. The paper trial runs the later of 6 rebalances or 3 months at half slots. The old Phases 4-7 are archived (`docs/archive/execution-plan-setup-system.md`). *Why:* the backtest decides whether anything else is worth building; monthly decisions mean waiting costs about one month of forward data |
| 150 | Book simulator readings (3.12) | *Taken under the user's delegation (#147), before the blind check, so within the pre-registration's step 2; listed in the 3.13 report.* The pre-registration's §3 leaves these open; each takes the conservative or the live side. **Halts** are measured on the book marked at E's close after the session's stops, before its trades; the week's base is the last close of the previous calendar week. **The retry** runs when E's buys were blocked by a daily or weekly loss halt and nothing else, or when slots stayed empty after a name with no bar on E was passed over; it is the next session, a half day blocks it (counted), and it re-runs the candidates and the rule book with R's ranking and that session's prices. Rule sells and stop raises are E's only. **A rule sell with no bar on E** sells at the holding's next close, and it keeps its slot until then. **Units:** quantities, stops and marks are carried split-adjusted, and a stop is checked on the split-adjusted bar. **A security that ends** exits on the first session it has no bar (with a successor, at min(its last raw close, the successor's first raw open)), at the tier slippage. **Cluster data:** each name's closes on the 61 sessions up to R, a session without a bar carrying the close before it; a name with too short a window counts as linked (#140). **Slippage tier:** the 50-session average dollar volume up to the session before the fill. **Every pick goes through the live buy guardrail** (`guardrails/book.py`) with all blocks in force, so a blocked session buys nothing; the invariants of the pre-registration's §7 raise an error when broken. **Benchmarks:** SPY and MTUM total-return bars sit in their own history table (`benchmarks`, `scripts/backfill.py benchmarks`), apart from the split-only bars. *Why:* the simulator needed each of these defined; none changes a rule, a threshold or the gate |
| 151 | Backtest harness readings (3.13) | *Taken under the user's delegation (#147), before the blind check (pre-registration step 2); listed in the report.* **Config:** comparisons C1, C5 and C7 become `book:` values (`signal`, `earnings_rules`, `cluster_cap`; the primary's values unchanged), so a frozen configuration lives wholly in `risk.yaml`; the blind check's four stop thresholds of §7 sit in `book.backtest`. **C5:** a name is not bought when a known report reacts within `event_report_sessions` (5) sessions after the buy session; a holding is sold at the close of the session before a report reacts if its value is under +10% of its cost. **C6:** `universe_size` 1,000,000. **Successors:** the next piece of the same ticker in the loaded history, as §3.8 reads (gap splits included; a piece starting after the period isn't loaded, so the exit takes the last close). **Blind shares:** price skips = price-fit and no-cash skips over the names the rule book reached (its picks plus every skip); cluster skips over the candidates listed plus the names the cluster rule removed. **References:** the equal-weighted set and the signal check use each ranked name's price relative from E's close to the next E's (the last close before, for a name that stopped trading). **The regime** of the 5-pillar variant is computed once and cached (it reads no book result). **The holdout** refuses to run unless `risk.yaml` hashes to the value 3.16 records. **Noted, not changed:** the adoption statistic is the mean daily return difference, which is arithmetic, so of two curves with the same end the more volatile scores slightly higher; the drawdown conditions temper it. *Why:* the harness needed each of these defined; none changes a rule, a threshold or the gate |

---

## 17. Known limitations

- **The edge is small, if it exists.** Long-only momentum has historically earned a few percent a year over the market, with deep drawdowns (25-40% is common) and occasional crashes when beaten-down stocks rebound (Daniel & Moskowitz 2016). Its premium is smaller than it was (McLean & Pontiff 2016).
- **At $5,000 the running costs (~$6-15 a month) are about the size of the expected edge.** Whole shares skip high-priced leaders; the backtest measures that cost on a notional $100,000 account.
- **Six years can't prove a small edge.** The backtest gate rests on point estimates (§15), and 2023-25, the holdout, was unusually kind to momentum.
- **LLM decisions can't be backtested** without hindsight bias; the scorecard and the paper trial are the honest tests, and a meaningful information coefficient takes a year or more of monthly reviews.
- **LLM output is non-deterministic.** Tighten-only plans, logging and guardrails contain it.
- **Prompt injection.** News and filing text reach the LLM. They are passed as delimited data (§9), and the guardrails bound the damage, but a planted headline could still bias a choice among the candidates.
- **Gap risk:** the broker stop doesn't protect against overnight gaps, and the book holds through earnings, where gaps are largest. With 8 slots one −30% gap costs about 3.75% of equity.
- **Concentration:** 8 names and drift without resizing can leave one holding a large share of the book; the backtest reports the largest weight.
- **Paper fills are optimistic.** Paper also never applies splits to positions, doesn't lower stops for dividends, and fills partly only at random (spike 0.3b), so those paths are tested on the fake broker (task 1.7), and a split on a held name during the trial needs the reconciliation rule in §5.7.
- **Survivorship bias:** delisted coverage is good but not complete (ticker enumeration misses 5-letter tickers, and ticker reuse can mix securities), so backtests may still miss some failed stocks. Each report quantifies the gap (§15).
- **Price-only returns:** the book's backtest credits no dividends (there's no dividend history for the whole universe), against total-return benchmarks. That understates the book slightly.
- **Point-in-time gaps:** dollar volume stands in for market cap; industry groups are today's, not historical.
- **Free-tier data limits:** the 15:40 decision price comes from IEX (#120), and a buy without one falls back to delayed SIP (#144); history starts in 2016; EDGAR covers only securities still trading (#130); IBD's distribution and follow-through counts use SPY/QQQ volume.
- **Regime lag:** trend-based detection reacts after the turn.
- **Vendor outages** fail closed: no new buys; hard stops stay.

---

## 18. Methodology sources

Primary sources are preferred: the traders' own writing or interviews, IBD's own educational articles (syndicated on Yahoo Finance), and peer-reviewed research. Book-derived numbers taken from summaries are marked † and should be checked against the books.

**Momentum research and practitioners** (the momentum book, #147)
- Jegadeesh & Titman (1993), "Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency", *Journal of Finance*: the 12-month formation, monthly rebalancing; returns around earnings announcements.
- Chan, Jegadeesh & Lakonishok (1996), "Momentum Strategies", *Journal of Finance*: price momentum and earnings momentum.
- Moskowitz & Grinblatt (1999), "Do Industries Explain Momentum?", *Journal of Finance*: industry momentum (the cluster cap's trade-off).
- Hong, Lim & Stein (2000), "Bad News Travels Slowly", *Journal of Finance*: momentum is stronger in small, less-covered firms (comparison C6).
- Lesmond, Schill & Zhou (2004), "The Illusory Nature of Momentum Profits", *Journal of Financial Economics*; Korajczyk & Sadka (2004), "Are Momentum Profits Robust to Trading Costs?", *Journal of Finance*: costs in small, illiquid names (the liquidity cut).
- Novy-Marx (2012), "Is Momentum Really Momentum?", *Journal of Financial Economics*: intermediate-horizon momentum.
- Asness, Moskowitz & Pedersen (2013), "Value and Momentum Everywhere", *Journal of Finance*: momentum across markets and asset classes.
- Israel & Moskowitz (2013), "The Role of Shorting, Firm Size, and Time on Market Anomalies", *Journal of Financial Economics*: momentum doesn't weaken with size.
- Da, Gurun & Warachka (2014), "Frog in the Pan: Continuous Information and Momentum", *Review of Financial Studies*: smooth momentum (a fact for the Analyst).
- Barroso & Santa-Clara (2015), "Momentum Has Its Moments", *Journal of Financial Economics*: volatility and crashes.
- Novy-Marx (2015), "Fundamentally, Momentum Is Fundamental Momentum", NBER working paper: earnings momentum (hold through reports).
- Daniel & Moskowitz (2016), "Momentum Crashes", *Journal of Financial Economics*: crashes in panic states, driven by the losers' leg (the overlay).
- Han, Zhou & Zhu (2016), "Taming Momentum Crashes: A Simple Stop-Loss Strategy", working paper: a 10% stop (comparison C2), cash until the next rebalance.
- McLean & Pontiff (2016), "Does Academic Research Destroy Stock Return Predictability?", *Journal of Finance*: momentum survived publication, smaller.
- Novy-Marx & Velikov (2016), "A Taxonomy of Anomalies and Their Trading Costs", *Review of Financial Studies*: the buy/hold spread (the hold zone).
- Hoffstein, Faber & Braun (2020), "Rebalance Timing Luck: The (Dumb) Luck of Smart Beta": the timing-luck diagnostic.
- Gray & Vogel, *Quantitative Momentum* (2016): the ~1,000-stock universe, 12-1 ranking, equal weights, no stops.
- Clenow, *Stocks on the Move* (2015): an index-trend filter for buys (comparison C4), selling below the top 20%, no stops.
- Antonacci, *Dual Momentum Investing* (2014); Faber (2007), "A Quantitative Approach to Tactical Asset Allocation": trend filters.

The trader sources below were the setup system's (archived); the regime layer still follows IBD.

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
