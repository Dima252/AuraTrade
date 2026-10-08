# Spike 0.3a: Alpaca market data (free plan)

**Run:** 2026-10-08, after the close. Script: `spikes/alpaca_data/` (`uv run --group spikes python -m spikes.alpaca_data <scenario>`; raw output in `spikes/alpaca_data/output/`, git-ignored). The checks of real-time freshness need market hours (`realtime` scenario, run sheet at the end).

**Decision: GO.** Three findings changed the design (adopted 2026-10-09 as decisions #109–111), plus implementation notes:
1. **IEX often has no opening range.** In the thinner half of the eligible universe, IEX prints no trade in the first 5 minutes on about half the days, so there's no IEX opening-range high to trigger on (below, with options).
2. **IEX pre-market is too thin to find gaps at 08:40.** Delayed SIP is the fix (below).
3. **Survivorship needs a workaround, and there is a cheap one.** Alpaca's asset list forgets most pre-2018 delistings, but the bars endpoint still serves them. Enumerating every 1–4 letter ticker recovers them in about 3 minutes per snapshot week.

## Answers to the plan's questions

| Question | Answer |
|---|---|
| Multi-symbol daily bars, `feed=sip`, `adjustment=split`, on the free plan? | **Yes**: up to 1,000 symbols a request (tested), 10,000 bars a page, `next_page_token` pagination |
| History depth | **2016-01-04** for every name tested (AAPL, MSFT, SPY, F, XOM, JPM, KO, INTC, GE, T): 2,706 sessions to 2026-10-07. Nothing earlier, as §17 assumes |
| Rate limits | **200 requests/minute** (`x-ratelimit-limit: 200`), shared across the data endpoints. **Latency, not the limit, dominates:** a full 10k-bar page takes ~7 s |
| Recent SIP | `403 subscription does not permit querying recent SIP data` for anything newer than 15 minutes. **A date-only `end` for today counts as recent**: the 17:00 ingest must pass an explicit `end` (e.g. now − 16 min, or `T20:00:00Z`) to get session T's bar |
| Do 1-day bars match regular-session OHLC? | **OHLC: yes.** In 50 of 50 symbol-days, the daily high/low lay within the 09:30–16:00 minute range, though extended hours went beyond it on 27. The open is the official open (median difference from the first regular-session minute: $0.00), and the close is the official closing print (median $0.025 from the 15:59 minute's close). **Volume: no.** Daily volume ≈ the 04:00–20:00 minute total (median ratio 1.004) and is 1.29× the 09:30–16:00 minute total (the closing auction, which prints at 16:00, plus extended hours) |
| Real-time IEX quotes and 1-min bars | Latest quote/trade/bar endpoints answer on the free plan (`feed=iex`). Freshness during the session is unconfirmed (`realtime`, run sheet) |
| IEX vs SIP opening range, 50 symbols | **IEX ORH below SIP in 182 of 211 symbol-days with IEX trades; equal in 29.** Shortfall median 0.22%, p90 0.88%, max 1.95%. **No IEX trade at all in 09:30–09:34 on 39 of 250 symbol-days (16%)**. By liquidity: see "Opening range" below |
| IEX vs SIP at 15:40 | Median absolute difference 0.024%, p90 0.14%, max 1.06%: fine for the decision price |
| IEX share of volume | Median 5.4% (p10 3.2%, p90 7.9%). IEX has a trade in 57% of the minutes SIP does (median) |
| Pre-market IEX quotes for ≥ 10% gaps | **No**: only 17 of 80 gappers had any IEX pre-market trade by 08:40. Delayed SIP works ("Pre-market gaps" below) |
| A 2,500-symbol day | **5,018 symbols, one session: 19 s** (6 requests of 1,000 symbols). Daily ingest is trivial |
| Full history (backfill, task 1.6) | 300 symbols 2016→2026: 576k bars, 59 requests, 405 s (~1,400 bars/s, sequential). About 7,300 symbols ≈ 14M bars ≈ 2.8 h sequential per adjustment; latency-bound, so **parallel requests under the 200/min limit cut it to ~20 min** (the 0.3h fetch uses 12 workers) |
| ETFs, SPACs, preferreds | **Alpaca's asset API has no type flag** (fields: `class, exchange, symbol, name, status, tradable, marginable, shortable, easy_to_borrow, fractionable, attributes`…). See "Classification" below |
| Survivorship | See "Survivorship" below |
| Raw (unadjusted) closes | **Yes**, `adjustment=raw`. NVDA 2024-06-07: raw $1,208.88, split $120.89, all $120.54 |
| Corporate-actions feed | **Yes**, `GET /v1/corporate-actions` (data API): forward/reverse splits (with old/new CUSIP), name changes (old/new symbol + CUSIP), cash/stock mergers, cash dividends, spin-offs. **Sparse before 2021**: mergers + name changes per year 2016: 0, 2017: 4, 2018: 2, 2021: 1,388. Splits and dividends go back further (AAPL dividends 2016–18, the AAPL 2020 split). The trading API's `/v2/corporate_actions/announcements` is forward-looking but thin: it listed 3 dividend ex-dates for the next 7 weeks |
| How far back do IEX 1-min bars go? | **2020-07-27** (the first SPY IEX minute bar is 2020-07-27 12:49 UTC). Nothing for any probe date before that |

## Opening range (finding 1)

The 5-minute opening-range high (ORH) is a trigger for VCP / pullback / emerging-leader entries when the range trades above the pivot, and always for `MomentumBreakout` and `EpisodicPivot` (§7.1). Live code takes it from **IEX** (§4, §5.12).

- Where IEX has trades, its ORH is a little **below** SIP's (median 0.22%). The trigger is therefore slightly early, and at 09:36 the consolidated price is often already above it. §7.1 already handles "already through the trigger" with a limit buy at the limit, so that part is fine.
- **The gap:** 16% of symbol-days had **no IEX trade in the first 5 minutes**. Then there is no IEX opening range to compute.

**By liquidity** (`orh_liquidity`: the 2,201 names that pass ≥ $10 and 20-day average dollar volume ≥ $10M, ranked by dollar volume; 40 names × 5 sessions per bucket, 2026-10-02 to 10-08):

| Bucket | Avg $ volume / day | No IEX trade 09:30–09:34 | IEX ORH below SIP (median / p90) | When IEX's first trade came (the misses) |
|---|---|---|---|---|
| Ranks 1–100 | $1.1B–26B | **0 / 200** | 0.08% / 0.30% | — |
| Ranks 101–500 | $249M–1.1B | **8 / 200 (4%)** | 0.14% / 0.52% | 09:35–09:46 |
| Ranks 501–1,000 | $94M–249M | **29 / 200 (15%)** | 0.21% / 1.0% | mostly 09:35–09:41; one none by 09:50 |
| Ranks 1,001+ (1,201 names) | $10M–94M | **96 / 200 (48%)** | 0.35% / 1.3% | 44 by 09:50, **52 with no IEX trade by 09:50** |

More than half the eligible universe is in the last bucket. **There, an IEX opening range is missing about half the time.**

**Options** (adopted: **B**, decision #109):
- **A: fail closed per symbol.** If IEX printed nothing in 09:30–09:34, an ORH-triggered entry for that symbol is skipped today. Pivot triggers are unaffected (they come from daily bars). This is the simplest option and matches §3 rule 6, but it drops about half the ORH entries in the thinner half of the universe. Those are mostly momentum breakouts and EPs, the strategies that always use the ORH.
- **B (recommended): fall back to delayed SIP.** Use IEX at 09:36 where it has the range (all of the top 500 names, 85% of the next 500). Where it doesn't, take the **consolidated** 09:30–09:34 high from 15-minute-delayed SIP, which is available at 09:50, and place that symbol's entry at **09:51** with the same "already through the trigger" rule (§7.1). Nothing is skipped for lack of data. The SIP level is the true consolidated range, and the simulator can reproduce the rule exactly (from minute history it knows whether IEX traded). The cost is a 15-minute later entry for those symbols (some fast breakouts become limit-or-skip), and `execute_entries` needs a second run at 09:51.

## Pre-market gaps

**Question:** at 08:40 (§3: IEX pre-market from 08:00), can IEX pre-market data detect a ≥ 10% gap for the EP list?

**Sample:** in the last 138 sessions there were 778 gaps of ≥ 10% (open ÷ prior close; prior close ≥ $10 and prior-day dollar volume ≥ $10M) and 2,751 gaps of 5–10%. 80 of each were sampled evenly. The estimated gap = the last pre-market price up to the cutoff ÷ the prior close.

| Source and cutoff | Usable at | ≥ 10% gaps with any pre-market trade | Flagged ≥ 10% | Flagged ≥ 7% | Median abs. error | False alarms (5–10% gaps flagged ≥ 10%) |
|---|---|---|---|---|---|---|
| IEX trades to 08:40 | 08:40 | **17 / 80** | **10** | — | −1.1% (signed) | 2 / 8 with a trade |
| IEX quotes 08:30–08:40 (mid) | 08:40 | 7 / 80 | 3 | — | −1.8% (signed) | 0 |
| IEX trades to 09:00 | 09:00 | 20 / 80 | 12 | 15 | 2.0% | 2 / 9 |
| IEX trades to 09:25 | 09:25 | 24 / 80 | 17 | 19 | 1.7% | 2 / 12 |
| **Delayed SIP to 08:25** | **08:40** | **78 / 80** | **54** | **64** | 2.7% | 4 / 79 |
| Delayed SIP to 08:40 | 08:55 | 79 / 80 | 50 | 69 | 2.8% | 4 / 79 |
| Delayed SIP to 09:25 | 09:40 | 79 / 80 | 64 | 73 | 1.2% | 4 / 79 |

**IEX pre-market is too thin to find gaps:** most gappers have no IEX pre-market print at all, even at 09:25. **15-minute-delayed SIP works:** at 08:40 it has the consolidated pre-market tape up to 08:25, and 78 of 80 gappers traded by then. A ≥ 7% screen at 08:40 catches 64 of 80 ≥ 10% gaps with few false alarms. The actual open re-check (§5.5, `execute_entries`) then confirms the ≥ 10%.

**This contradicts §3 / §5.5 as written** ("IEX pre-market trading starts at 08:00, so gaps are measured here"). The gap step's *data source* should change, not its time: **measure pre-market gaps at 08:40 from delayed SIP (to 08:25)**, screen EP candidates at ≥ 7%, and confirm ≥ 10% at the open. That's the recommended fix for you to approve; the 08:40 schedule and the 09:10 deadline stay. The "setups gapping > 5% above their pivot are invalid" check uses the same delayed-SIP price.

## Classification (ETFs, SPACs, preferreds)

13,199 active, tradable assets on NYSE / NASDAQ / AMEX / ARCA / BATS. Classified in this order:

| Rule | Count | Notes |
|---|---|---|
| **ETF flag in the Nasdaq Trader symbol directory** (`nasdaqlisted.txt` / `otherlisted.txt`, free, no key) | 5,760 | Catches 392 ETFs/ETNs whose names don't say "ETF" ("ProShares Ultra Silver", "…ADRhedged"). 13 the other way round (VXX, DGP…) are ETNs; add an `ETN` name check |
| Symbol punctuation (`.PRx` preferreds, `.U` units, `.WS` warrants, `.RT` rights, class shares like `MOG.B`) | 514 | Class shares (`BRK.B`, `MOG.B`) are common stock: whitelist a single-letter class suffix |
| SPAC-like name (`Acquisition / Merger / Capital Corp`) | 650 | Almost all SPAC shells. EDGAR SIC 6770 (blank checks) is the proper test (0.3e) |
| Non-common name (`Fund`, `Trust`, `Units`, `Warrants`, `Preferred`, `Depositary Shares`, `Notes due`…) | 1,257 | **Over-excludes:** REITs named "…Trust Incorporated Common Stock" and **ADRs** ("American Depositary Shares"). Test "Common Stock / Ordinary Shares" first, and decide whether ADRs are in the universe (IBD and Minervini trade them) |
| **Common** | 5,018 | NYSE 1,749 · NASDAQ 3,033 · AMEX 231 · ARCA 4 · BATS 1 |

The price ≥ $10 and dollar-volume ≥ $10M filters remove most SPAC shells and preferreds anyway. ETFs are the class that matters (liquid, and they would pass the filters), and the directory flag handles them. **Recommendation for task 1.3:** use the directory's ETF flag, then the symbol and name rules, then SIC 6770 from EDGAR.

## Survivorship (finding 2)

**Known delistings, 2017–2022** (48 hand-picked acquisitions, bankruptcies and ticker changes, e.g. WFM, TWX, MON, CELG, RHT, TWTR, XLNX, JCP, GNC):
- **47 of 48 have bars** under their last ticker; **44 have the full history** from 2016-01-04 to the delisting. Missing: STI (merged into TFC); HTZ (only the 2021 relisting; the 2016–20 history isn't under HTZ, HTZZ or HTZGQ).
- **But 26 of the 48 aren't in Alpaca's asset list at all** (WFM, TWX, AET, PNRA, TWTR…), and **7 show as `active` because the ticker now belongs to someone else** (SPLS, SHLD, APC, STI, BBT, FB; HTZ relisted). Only 15 appear as `inactive`. A universe built from the asset list loses most of them, even though their bars exist.

**Alpaca's own inactive list:** 2,874 inactive symbols on the listed exchanges. 67 are renamed `<SYM>_DELISTED`, and 509 are junk codes (`0029900E0`, `D012219`) that make a whole multi-symbol request fail with `400 invalid symbol`, so they must be filtered out. Of 400 cleaned ones sampled, 391 have bars. Their last traded bars fall mostly in 2019–2022 (2018: 29, 2019: 57, 2020: 76, 2021: 108, 2022: 73), so **the list covers delistings from about 2018 on**.

**Universe size by year** (current commons + cleaned inactive; symbols with a traded bar in the first week of January): 2016: 3,619 · 2018: 4,119 · 2020: 4,238 · 2022: 4,365 · 2024: 4,319 · 2026: 4,911. The early years are visibly short.

**Workaround found: enumerate tickers.** The bars endpoint answers for any ticker, listed or not. Requesting all 475,254 possible 1–4 letter tickers for one week takes 476 requests and **~3 minutes**:

| Snapshot week | Tickers with traded bars | Not in the asset list | Liquid that day (≥ $10, ≥ $10M) | Liquid and not in the asset list |
|---|---|---|---|---|
| 2016-01-04 to 08 | 7,944 | 2,554 | 2,187 | **454 (21%)** |
| 2017-06-05 to 09 | 7,901 | 2,092 | 2,292 | **386 (17%)** |

So about a fifth of the liquid 2016–17 universe is invisible through the asset list and recoverable by enumeration. Five-letter tickers aren't enumerated (26⁵ = 11.9M), which leaves a small residual gap (mostly NASDAQ class shares).

**Data traps found on the way (important for 1.5, 1.6 and `sim/`):**
- **Zombie bars after a delisting:** Alpaca keeps emitting daily bars at the **last close with volume 0**, for at least 18 months (MON: last real trade 2018-06-06, flat $127.95 bars through 2019-12-31; also CA, XL, SHLD, CHK). **Drop `v == 0` bars.** Otherwise a backtest holds a delisted stock at a frozen price and the universe keeps it "listed".
- **History follows the current ticker.** FB's 2021 bars come back only under META, SQ's only under XYZ, ANTM's only under ELV. The old ticker returns nothing.
- **Ticker reuse mixes securities.** BBT's bars after BB&T became TFC (Dec 2019) are a different security ($10–12 in 2020, versus $54 TFC), and its pre-change bars look mixed too. A long gap in trading is the tell. The 0.3h check treats a gap of more than 30 sessions as a new security. For the real backfill, key securities by **Alpaca asset id / CUSIP** where available and use the corporate-actions name changes. Don't key by ticker alone.
- **`adjustment=split` doesn't adjust spin-offs:** GE's 2021 history under `all` is ~40% lower than under `split` (the later GEHC and GE Vernova spin-offs). That's the intended behaviour for broker prices (§5.1), but a spin-off ex-date then appears as a real price drop in RS and 52-week ranges.
- Daily volume includes extended hours (above). **The volume-pace curve (§5.8) must be built from the same definition as the 50-day average it is compared with.** Either build both from SIP 09:30–16:00 minutes, or project full-day volume including the closing auction.

- **Bar revision after 17:00 (pending):** daily volume includes extended hours, so a bar fetched by the 17:00 ingest may grow later. 195 of the 2026-10-08 bars were saved at 16:58 ET; `bar_revision compare` (run sheet) shows whether they changed. If they did, the gap-fill ingest should re-fetch session T−1's bar the next evening.

## README impact (for task 0.4)

- §4 / §5.12 / §7.1: the IEX opening-range gap → option A or B above.
- §3 / §5.5: the 08:40 gap step reads delayed SIP (to 08:25), not IEX; EP screen ≥ 7%, confirmed ≥ 10% at the open.
- §5.1 / §15: the universe source for backtests = asset list + enumeration snapshots; drop zero-volume bars; split securities on long gaps; report the residual gap (5-letter tickers, ticker reuse).
- §5.1: classification order (directory ETF flag → symbol → name → SIC 6770); decide on ADRs.
- §5.12: IEX minute history starts 2020-07-27, so the tuning period 2016–2022 is mostly SIP-only (expected by the README's "SIP before that").
- §3 / 1.5: the ingest passes an explicit `end` for session T.

## Run sheet (market hours)

| When (ET) | Command | Answers |
|---|---|---|
| Fri 2026-10-09, during the session | `uv run --group spikes python -m spikes.alpaca_data realtime` | Real-time IEX freshness (age of the latest quote/trade/bar for 50 symbols); confirms SIP's 15-minute delay |
| Fri 2026-10-09 or later | `uv run python -m spikes.alpaca_data.bar_revision compare` | Whether the 2026-10-08 daily bars fetched at 16:58 ET changed later (after-hours volume) |
