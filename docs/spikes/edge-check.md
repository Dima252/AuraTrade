# Spike 0.3h: early edge check (daily bars, 2017–2022)

**Run:** 2026-10-08/09. `uv run --group spikes python -m spikes.edge_check.fetch` (data), then `… .repair`, then `… .run`. Spike-grade: it **tunes nothing**, it isn't a fill model, and nothing outside `spikes/` may import it. Holdout (2023+) **not touched**: the data stops at 2022-12-31.

**Decision: continue to Phase 1 (#112).** Result: both simple setups are about breakeven before costs and slightly negative after a crude 0.2%-a-side slippage. The 90% intervals exclude zero after costs. Per the plan this goes to you before Phase 1. **My reading: weak and borderline, not "clearly negative"** (details and a recommendation at the end). It's your call whether to continue.

## Setup

**Universe** (point in time, per session): raw close ≥ $10, 50-session average dollar volume ≥ $10M, ≥ 252 traded sessions. Candidates = today's 5,018 common stocks + 2,116 cleaned inactive symbols + 2,905 tickers recovered by enumeration (0.3a), 10,040 in all. ETFs and other non-common listings known today are dropped.
- **3,569 securities passed the filter at some point: 2,359 still active, 1,210 (34%) delisted** (451 from Alpaca's inactive list, 759 only through enumeration). The median universe was 1,738–1,979 names per session.
- Zero-volume bars (zombies after a delisting) are dropped. A gap of more than 30 sessions splits a ticker into separate securities (ticker reuse).

**Setups** (fixed in advance from README §5.3 / the plan; nothing was varied):
- **Trend Template**: all 8 rules, with RS = the percentile of 0.4·R63 + 0.2·R126 + 0.2·R189 + 0.2·R252 within the universe.
- **VCP-lite**: TT; three consecutive 10-session segments, each range tighter than the last, the last ≤ 10%; 30-session depth ≤ 35%; final-segment volume below the prior 50-session average; close below the pivot. Pivot = the 10-session high; stop = the 10-session low.
- **Momentum-lite**: TT; top 2% of the universe by 1-, 3- or 6-month return; ≥ 30% above the 63-session low; 10-session consolidation ≤ 15% deep with higher lows; close > 10-day > 20-day, 20-day rising; close below the pivot. Pivot = the consolidation high; stop = its low.
- **Entry**: next session, when the high reaches the pivot; fill = max(pivot, open) + 0.2%; skipped if it opens more than 5% above the pivot. **Stop** = base low, capped at 8% below the fill. **Exit**: the stop (at min(stop, open) − 0.2%; on the entry day the stop is assumed to come first), or a close below the 50-day (VCP) / 10-day (momentum), at that close − 0.2%. Delisted → the last close. One open trade per security; no portfolio limits, no regime gating.

## Results (R = multiples of the initial risk, after slippage)

| | Trades | Win rate | Avg R | 90% CI (session bootstrap) | Avg R before slippage | Avg win / loss R | Profit factor | Avg hold |
|---|---|---|---|---|---|---|---|---|
| **VCP-lite** | 6,236 | 26.2% | **−0.081** | −0.139 to −0.020 | **+0.004** | +2.08 / −0.85 | 0.89 | 25 sessions |
| **Momentum-lite** | 1,941 | 30.2% | **−0.080** | −0.122 to −0.037 | **−0.014** | +0.97 / −0.54 | 0.81 | 8 sessions |

**Per entry year (avg R after slippage; before slippage in brackets):**

| Year | VCP-lite | Momentum-lite |
|---|---|---|
| 2017 | **+0.25** (+0.36), n 1,528 | +0.04 (+0.11), n 437 |
| 2018 | −0.20 (−0.12), n 1,170 | −0.14 (−0.07), n 428 |
| 2019 | −0.05 (+0.04), n 1,226 | −0.13 (−0.06), n 379 |
| 2020 | +0.07 (+0.15), n 738 | +0.11 (+0.18), n 136 |
| 2021 | −0.24 (−0.17), n 1,019 | −0.10 (−0.04), n 298 |
| 2022 | **−0.70** (−0.66), n 555 | −0.20 (−0.12), n 263 |

**By market trend at entry (SPY vs its 200-day):** VCP above −0.05 (CI −0.10 to +0.01, n 5,582), below −0.36 (n 654). Momentum above −0.07 (CI −0.12 to −0.03, n 1,633), below −0.12 (n 308).

**By source:** VCP active −0.07 / inactive −0.13 / enumerated −0.10. Momentum −0.09 / −0.08 / −0.06. The delisted names do slightly worse, as expected, but they aren't what drives the result.

**Exits:** VCP 3,676 on the 50-day close, 2,414 stops (63 on the entry day), 109 delisted, 37 open at the end. Momentum 1,678 on the 10-day close, 236 stops (31 on the entry day).

**Sanity checks:** the biggest winners are real moves (VCP: NKTR 2017 +35R, RACE, ALGN, ABMD, TDOC 2020; momentum: TSLA Jan–Feb 2020 +10R, ARWR, AXSM). Stop widths: median 6.0% (VCP) and 8.0% (momentum). NVDA's 2017 prices are ~$2.7 split-adjusted, but the universe filter correctly uses the raw close.

## How to read it

- **The survivorship bias is small here.** The plan expected a universe missing most delisted names, which would make this an upper bound. With the 0.3a enumeration, a third of the universe is delisted names, so the result is close to unbiased on that score. It's a fair, rough estimate rather than a flattering one.
- **The sign is set by costs.** Both setups are within ±0.02R of zero before slippage. A flat 0.2% a side is crude: roughly right for the $10–100M names that dominate the universe, too high for the top few hundred.
- **It's regime-shaped.** VCP made +0.25R in 2017 and +0.07R in 2020, and −0.70R in 2022 and −0.24R in 2021 (the speculative top and the bear). The design never takes these setups ungated. §6 funds breakouts only in `CONFIRMED_UPTREND` (and at conviction ≥ 4 in `UNDER_PRESSURE`), ranks a top 40, and adds early-behaviour rules, partial profit-taking and the 8-week hold. None of that is here. A crude SPY > 200-day filter only lifts VCP to −0.05R, so the regime classifier has real work to do.
- **The setups are cruder than the strategies.** One fixed segmentation, no base count, no RS line, no group rank, no catalyst, and exits that give back a lot (a 50-day close exit after a 2R gain). The win/loss profile (26% winners averaging +2.1R) is the typical breakout shape; the edge, if any, is in the selection and exit details that 3.8 tests.

## Recommendation

Not clearly negative enough to stop. It is, though, a real warning that **the edge has to come from regime gating, selection and exits, not from the base patterns themselves.** That's what the 3.8 baseline gate tests formally. Two options for you:
1. **Continue to Phase 1 (recommended)**, and carry this into 3.8 as the "naive" reference. The gate there must beat it clearly. Keep the cost model honest: a liquidity-scaled slippage will matter as much as any rule.
2. **Pause** and run one more spike-grade variant gated by a simple regime proxy (e.g. SPY above its 50- and 200-day, with no 2022-style distribution) before building the data layer. This doesn't tune anything, but it's a second look at the same period, so it would weaken 3.8's tuning-period evidence slightly.

## Reproduce

```bash
uv run --group spikes python -m spikes.alpaca_data assets                       # universe.json
uv run python -m spikes.alpaca_data.enumerate 2016-01-04 2016-01-08             # recovered tickers
uv run python -m spikes.alpaca_data.enumerate 2017-06-05 2017-06-09
uv run --group spikes python -u -m spikes.edge_check.fetch > spikes/edge_check/fetch.out   # ~16 min
uv run --group spikes python -m spikes.edge_check.repair                       # re-fetch 429'd chunks
uv run --group spikes python -m spikes.edge_check.run                          # ~30 s
```

Data: 9.9M split-adjusted bars + 10.2M raw closes for ~9,000 symbols (`spikes/edge_check/data/`, git-ignored).
