# Recorded HTTP fixtures

Real vendor responses, recorded once from free endpoints, so tests and CI never call a vendor (CLAUDE.md §3.3). Only response bodies are stored, never URLs or headers, and a recording is refused if it contains a key.

| Folder | Recorded | What | Trimmed |
|---|---|---|---|
| `alpaca/calendar_*.json` | 2026-10-09 | `GET /v2/calendar` | no |
| `alpaca/bars_daily_all.json` | 2026-10-26 | SIP daily bars for MTUM and SPY with `adjustment=all` (total return; the total-return stage's tests), 2026-09-16 to 09-22 | no |
| `alpaca/` (the rest) | 2026-10-15 | SIP daily bars (forced into 3 pages with `limit=3`), IEX minute bars, the recent-SIP `403`, IEX latest trades and quotes, corporate actions (splits, dividends, mergers, spin-offs, name changes), assets, news (2 pages of 2) | assets to 6 symbols |
| `cboe/` | 2026-10-15 | `_VIX.json` / `_VIX3M.json` delayed quotes (intraday, so `last_trade_time` isn't the settlement), history CSVs | CSVs to the first 2 and last 6 rows |
| `fred/` | 2026-10-15 | `VIXCLS` observations; the `400` for an unregistered key | no |
| `edgar/` | 2026-10-15 | `company_tickers.json`, Apple's submissions and its first older page, Apple's company facts | tickers to 4; filings to 14 recent and 6 older; facts to 3 tags × 8 values |
| `finnhub/` | 2026-10-15 | earnings calendar for 2026-10-20, AAPL surprises and profile, an unknown symbol's profile (`{}`) | calendar to 6 rows |
| `nasdaq/` | 2026-10-15 | `nasdaqlisted.txt`, `otherlisted.txt` | to 4 and 6 symbols, header and footer kept |
| `french/` | 2026-10-27 | Kenneth French's `F-F_Research_Data_Factors_daily.csv` (unzipped; the tests zip it) | to the notes, the header, the first 5 and last 6 rows and the footer |
| `shiller/` | 2026-10-27 | the Open Knowledge `s-and-p-500` `data.csv` (Shiller's monthly data) | to the first 3 rows, 2023-05 to 08 and the last 2 |
| `anthropic/` | 2026-10-27 | `recorded_turn1.json`, `recorded_turn2.json`: one real decision (a tool round, then a structured answer) recorded with `scripts/record_claude_fixtures.py --paid` (paid, $0.017, approved by the user). The rest are **hand-built** in the same wire format for the paths a real call can't be asked to take: a refusal after fallbacks, a fallback-served answer, a mid-output fallback, a 529 | no |
| `bases/` | 2026-10-10 | daily bars for the base-count tests (task 2.2) | |

To re-record after a vendor changes a format, fetch the same endpoint, trim it the same way, and check that no key appears in the body.
