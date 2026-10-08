# Spike 0.3f: news

**Run:** 2026-10-08 after the close, `uv run python -m spikes.news` (Alpaca `GET /v1beta1/news`, free plan, `dev` keys).
**Decision: GO.** README §5.1 ("News: Alpaca news API, on demand") stands. Notes for the context builders (task 4.3) are below.

## Results

| Question | Answer |
|---|---|
| Available on the free plan? | **Yes**: HTTP 200, the same keys as market data |
| Rate limit | `x-ratelimit-limit: 200` per minute, shared with the other market-data endpoints |
| Source | **Benzinga only** (every article from 2025 on is tagged `benzinga`; older ones have an empty `source`) |
| Latency | the newest article was 3 minutes old when fetched |
| History | back to **2015-01-01** |
| Volume, whole market | 2026-10-07: 986 · 2026-10-01: 832 · 2025-03-12: 843 · 2021-06-09: 782 · 2016-06-08: 517 articles a day; one day takes 3 s to fetch (50 per page) |
| Tagged symbols | 95–97% of articles carry at least one symbol; the median is one symbol |

**Volume per symbol, last 30 days:** NVDA 563 · AAPL 253 · F 48 · IONQ 39 · SMCI 32 · APP 26 · DXJ 16 · BROS 15 · SOFI 11 · CELH 2.

## Payload

Fields: `id`, `headline`, `author`, `created_at`, `updated_at`, `summary`, `content`, `url`, `images`, `symbols`, `source`.

- `content` (HTML) comes only with `include_content=true`. About 70% of large-cap articles have it (4–9k characters). Newsdesk one-liners ("Reported Earlier, Japan Current Account…") have an empty `summary` and `content`: the headline is the whole item.
- Mega caps get swept into round-ups: NVDA is tagged on memory-sector and crypto articles. Raw counts overstate company-specific news.
- Pagination uses `next_page_token`, with up to 50 items a page; `sort=asc|desc`.

## Notes for the LLM context (task 4.3)

1. **Headlines only, capped per symbol** (e.g. the latest 10 in a window), with `created_at`. Including article bodies would add 4–9k characters each and blow the token budget (§9). NVDA alone has ~19 articles a day.
2. **Filter round-ups:** prefer articles tagged with ≤ 3 symbols, so multi-ticker lists ("10 Information Technology Stocks Whale Activity…") don't crowd out company news.
3. **Thin names are normal:** CELH had 2 articles in 30 days. An empty news list must read as "no news found", never as an error that fails closed (§3, rule 6, applies to *missing data*, not to quiet tickers).
4. **EP backtests:** news history back to 2015 exists, so an EP "news" flag could be tested later. README §17 already documents the gap + volume proxy, and nothing changes now.

## Not covered

- Whether Benzinga's tagging is complete for small caps (spot-checked only).
- Real-time streaming (`wss://stream.data.alpaca.markets/v1beta1/news`). The design reads news on demand, so it isn't needed.
