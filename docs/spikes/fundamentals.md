# Spike 0.3e: fundamentals, earnings dates and industries

**Run:** 2026-10-09, `uv run --group spikes python -m spikes.fundamentals <scenario>` (`universe`, `edgar`, `calendar`, `history`, `facts`). Sources: SEC EDGAR (submissions, company facts; `EDGAR_USER_AGENT` header, about 6 requests a second), Finnhub free tier, Alpaca news for the timing check. Raw output in `spikes/fundamentals/output/` (git-ignored).
**Decision: ADJUST** (proposed, for approval at 0.4). EDGAR carries everything the backtests need, and Finnhub covers upcoming dates for most US companies. Two changes: historical earnings timing takes the **earlier of the 8-K and the first earnings headline**, and upcoming before/after-market timing comes from the company's own 8-K habit when Finnhub leaves it blank, which is most of the time.

## Universe used

Spike-grade, not point in time: the 5,018 common-stock symbols from spike 0.3a, filtered to last close ≥ $10 and 50-session average dollar volume ≥ $10M on raw SIP bars (2026-07-30 to 2026-10-08). **2,191 symbols**, of which **2,188 map to an SEC CIK** through `company_tickers.json` (`BRK.B` → `BRK-B`). The three misses are thin banks.

## Industries (SIC)

| Check | Result |
|---|---|
| SIC code from EDGAR submissions | **2,180 of 2,191 (99.5%)** |
| Distinct 4-digit SIC codes | 344 (largest: 2834 pharma 136, 7372 software 101, 6798 REITs 74, 3674 semiconductors 62, 6022 state banks 61) |
| Blank checks (SIC 6770) | none left after the universe filters |
| Foreign filers (20-F / 40-F, no 10-Q) | **214 (9.8%)**: ADRs and Canadian cross-listings |

**Grouping:** roll a SIC code up to its 3-digit and then 2-digit prefix until each group has a minimum number of members:

| Minimum group size | 5 | 8 | 10 | 12 | **15** | 20 |
|---|---|---|---|---|---|---|
| Groups | 194 | 142 | 127 | 113 | **102** | 93 |

A minimum of **15 gives 102 groups**, close to the README's ~100. SIC's known weakness stays (themes such as AI infrastructure span several codes), which is why the correlated-cluster guardrail also uses return correlation (README §8).

## Earnings calendar (Finnhub free tier)

| Question | Answer |
|---|---|
| Upcoming dates | One request per day. A request returns at most **1,500 rows** (2026-11-02 to 11-06 hit the cap as one range), so query day by day |
| Coverage: next date within 63 sessions | **88% of the universe**; **94% of domestic filers**, only **36% of foreign filers** |
| Before/after-market timing (`hour`) | **Blank for 61%** of covered names (1,188 blank, 426 `amc`, 319 `bmo`) |
| EPS estimate present | 1,910 of 1,933 covered names |
| **Past dates** | **None.** Every past day queried (2026-07-30, 2025-07-31, 2022-07-28, 2019-07-30) returns 0 rows, and a symbol query from 2016 returns only the next report |
| Surprises (`/stock/earnings`) | Free, **last 4 quarters only** (48 of 50 sampled symbols return 4). Median absolute surprise 12.7% |
| Rate limit | 60 requests a minute (`x-ratelimit-limit: 60`) |

So the README's plan stands for **upcoming** dates, but Finnhub can't supply history at all. Its EPS "actual" is the vendor's adjusted figure, not GAAP: AMD's last quarter shows 1.66 on Finnhub and 1.38 diluted EPS in the 10-Q.

**Uncovered domestic names** (6% of them) include recent IPOs, dual-class lines (`BATRK`, `LLYVA`, `BELFB`) and some large names with an unconfirmed date (`CHTR`, `CI`, `ENB`). The estimated-date blackout (last report + 63 sessions, README §8) applies to these.

## Earnings history from 8-K Item 2.02 (2016–2025)

A sample of 50 domestic filers (25 from the 400 most liquid, 25 from the rest; seed 11). Every 8-K whose items include 2.02, matched against the Alpaca/Benzinga headline mentioning earnings, results, EPS, revenue or the quarter that is closest in time (−3 days to +1 day).

| Check | Result |
|---|---|
| 8-K Item 2.02 filings | **1,776** (160–196 a year) |
| Years with fewer than 4 | 84 name-years, almost all before the company listed (DDOG 2019 IPO, LIN 2018 merger, FOXA 2019 spin-off, SUNB). A few real gaps: VMC 2017–18 and FN 2018 with 3 |
| `acceptanceDateTime` time zone | **UTC** (the trailing `Z` is real): the news agrees with UTC in 1,576 of 1,611 events and with Eastern time in 35 |
| Events with an earnings headline | 1,611 of 1,776 (91%) |
| **Same first reaction session** (8-K vs headline) | **1,553 of 1,611 (96.4%)** |
| 8-K after the first headline | median **43 minutes**; 10% of filings come before it, 10% more than 5 hours after |
| 8-K timing | 862 after the close, 355 before the open, 394 during the session |

**The 58 mismatches (3.6%)** share one pattern: the press release comes out before the open and the 8-K is filed after the close (VMC in 2016–18: release at 08:00, 8-K at 16:30–18:00; LIN 2019; CTSH). Going by the 8-K alone would put the reaction a session late, so the blackout and the earnings cushion would act a day late in backtests.

**Proposal for task 1.6 (backfill):** the report time is the **earlier of the 8-K acceptance and the closest earnings headline**. Where there's no headline (9%), the 8-K alone. Many of the 394 "during the session" 8-Ks are morning releases filed later the same day, and the earlier-of rule handles them too.

**Proposal for upcoming reports:** when Finnhub's `hour` is blank, use the company's usual timing from its last 8 reports (by the rule above). If it has no history, treat the report as before the open, which is the conservative choice: the cushion runs one session earlier.

## Fundamentals from company facts

The same 50 names, quarters ending 2016–2025. Q4 = the 10-K's annual value minus the three 10-Q quarters, because companies seldom tag a Q4 value. The first-filed value is kept (point in time).

| Check | Result |
|---|---|
| Names with quarterly revenue | 48 of 50 (the two without: pre-revenue biotech VOR, bank SFBS) |
| Quarters with both revenue and diluted EPS | median **100%**; **42 of 48 names ≥ 90%** |
| Revenue tags needed | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax` (ASC 606, 2018 onward), `SalesRevenueNet` (before 2018), `…IncludingAssessedTax`, `SalesRevenueGoodsNet`. Companies switch tags over time, so the extractor merges them by period |
| EPS tags needed | `EarningsPerShareDiluted`; also `IncomeLossFromContinuingOperationsPerDilutedShare` (GD reports only this from 2018 on: adding it lifted GD from 8 of 40 quarters to at least 36) and `EarningsPerShareBasicAndDiluted` |
| Derived Q4 values | 296 revenue, 302 EPS (about 1 in 5 quarters) |
| **Filed minus period end** | median **35 days**, 10th–90th percentile 25–58 days |

The filing lag confirms README §5.1: keyed on the period end, a backtest would look ahead by about 5 weeks. **Weak spots:** banks (revenue is net interest income plus fees, not one tag; BNY has 4 of 10 quarters), companies with no revenue yet, and recent IPOs (the "expected quarters" window is short). A Q4 EPS derived by subtraction is approximate when the share count changes during the year.

**Foreign filers (9.8% of the universe)** file no quarterly XBRL. EDGAR gives them annual figures only, so their quarterly growth and acceleration features will be missing. The quality score must treat that as missing, not as zero.

## Notes for the build

1. **1.3 `edgar` client:** UTC acceptance times; a User-Agent with a real email; at most 10 requests a second (6 was used, and the 2,188 submissions took about 15 minutes); load the older `files` pages of submissions for 2016 coverage.
2. **1.3 `finnhub` client:** the calendar day by day (the 1,500-row cap); put the key in the `X-Finnhub-Token` header, never the query string, so it can't leak through an httpx error message (the 0.3d FRED incident).
3. **1.5 industries:** SIC rollup with a minimum group size of 15. Foreign filers without a SIC (11 names) fall back to Finnhub's profile industry.
4. **1.6 backfill:** 8-K Item 2.02 history plus headline timing (the earlier of the two), fundamentals keyed on `filed`, revenue and EPS tag fallbacks as listed above.
5. **Quality score (3.3):** a missing quarterly growth figure (foreign filers, banks) must not count against or for a stock; leave it out of that stock's score.

## Not covered

- Finnhub's accuracy against what actually happens. The 2026-10-09 calendar snapshot (`output/finnhub_calendar_2026-10-09.json`, 5,198 rows) can be checked against the 8-K filings after the October–November season. Suggested as a short follow-up check in Phase 1.
- Bank revenue (net interest income + noninterest income) was not modelled.
- Point-in-time SIC: EDGAR gives the current code only. Historical industry changes are rare and are ignored.
