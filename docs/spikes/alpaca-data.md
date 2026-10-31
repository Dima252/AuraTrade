# Spike 0.3a: Alpaca market data (free plan)

**Run:** 2026-10-08 after the close, and 2026-10-09 in market hours, on the free plan's keys.

**Result: the free plan covers the design**, with two rules it needed: a staleness rule for the decision price (#23) and the re-fetch of the previous session's bar (#25).

## Answers

| Question | Answer |
|---|---|
| Daily bars, `feed=sip`, `adjustment=split`, many symbols a request | **Yes**: up to 1,000 symbols a request, 10,000 bars a page, paginated |
| History depth | **2016-01-04** for every name tested. Nothing earlier (README §17) |
| Rate limit | **200 requests a minute**, shared across the data endpoints; a full page takes ~7 s, so latency dominates |
| Recent SIP | Refused for anything newer than 15 minutes (`403 subscription does not permit querying recent SIP data`). A date-only `end` for today counts as recent, so the evening ingest passes an explicit `end` |
| Do daily bars match the regular session? | **OHLC yes** (the official open and closing print). **Volume no:** it includes extended hours and the closing auction |
| Does a daily bar change after the evening? | **Its volume does:** bars fetched at 16:58 grew later for 187 of 195 symbols, by up to 62%. The ingest re-fetches the previous session's bar (#25) |
| Raw (unadjusted) closes | Yes, `adjustment=raw`; stored beside the split-adjusted bar |
| Corporate actions | `GET /v1/corporate-actions`: splits, reverse splits, name changes, mergers, cash dividends, spin-offs |
| Real-time IEX quotes and trades | Answer on the free plan (`feed=iex`) |
| IEX against SIP at 15:40 | Median absolute difference 0.024%, p90 0.14%: fine for a decision price where IEX traded recently |
| Tickers | Alpaca's asset API has no type flag, and tickers are reused after delistings, so securities are keyed by the asset id (#25) |

## Real-time freshness (2026-10-09, 12:30 ET)

| Sample | Latest IEX trade age: median / p90 | Latest IEX quote age: median / p90 |
|---|---|---|
| The 500 most liquid names | **29 s** / 3 min | **0 s** / 22 s |
| The rest of the liquid universe | **4 min** / 66 min | **6 s** / 2.3 min |

- SIP's delay is 15-16 minutes.
- **IEX quotes stay fresh where IEX trades are rare:** a resting IEX bid and ask move with the market; a trade needs someone to trade on IEX.
- **So the decision price** (#23) is the last IEX trade if at most 2 minutes old, else the IEX quote midpoint if at most 1 minute old with a sane spread, else no price. The portfolio's funds are among the most liquid names, where the last trade is usually seconds old.
