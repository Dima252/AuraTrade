# Spike 0.3c: market calendar

**Run:** 2026-10-08 (Alpaca `GET /v2/calendar`, paper endpoint; `pandas_market_calendars` 5.5.0, calendar `NYSE`).
**Result:** the two sources agree exactly over 12 years; README §4 (Alpaca primary, `pandas_market_calendars` the cross-check) stands, with the three notes below.

## What the README assumes

§4: Alpaca's calendar is primary and `pandas_market_calendars` is the cross-check. A disagreement blocks increases of exposure for the day. `market_sessions` stores open/close and `is_early_close`, and early closes are at 13:00.

## Results, 2016-01-01 to 2027-12-31

| Check | Result |
|---|---|
| Sessions | Alpaca 3,016, `pandas_market_calendars` 3,016 |
| Dates in only one source | none |
| Same date, different open/close | none |
| Early closes | 24 in both sources, all 09:30–13:00 (below) |
| Unscheduled closures | both have 2018-12-05 (President Bush) and 2025-01-09 (President Carter) |
| Sessions per year | 2016: 252 · 2017: 251 · 2018: 251 · 2019: 252 · 2020: 253 · 2021: 252 · 2022: 251 · 2023: 250 · 2024: 252 · 2025: 250 · 2026: 251 · 2027: 251 |
| Alpaca horizon | covers 2028 too (251 sessions) |

**Early closes (13:00):** 2016-11-25; 2017-07-03, 11-24; 2018-07-03, 11-23, 12-24; 2019-07-03, 11-29, 12-24; 2020-11-27, 12-24; 2021-11-26; 2022-11-25; 2023-07-03, 11-24; 2024-07-03, 11-29, 12-24; 2025-07-03, 11-28, 12-24; 2026-11-27, 12-24; 2027-11-26.

## How each source represents a session

**Alpaca** returns one row per session:

```json
{"date": "2026-11-27", "open": "09:30", "close": "13:00",
 "session_open": "0400", "session_close": "2000", "settlement_date": "2026-11-30"}
```

- `open` / `close` are **ET wall-clock strings with no time zone**. An early close appears only as `close: "13:00"`. There is no flag.
- `session_open` / `session_close` are the extended-hours bounds (04:00–20:00). Nothing reads them.
- `settlement_date` moves from T+2 to T+1 on 2024-05-28, as the SEC rule did.

**`pandas_market_calendars`** returns tz-aware UTC timestamps (`market_open`, `market_close`). Its `early_closes()` lists the same 24 dates.

## Notes for the calendar module

1. Localize Alpaca's `open`/`close` to `America/New_York` for each date, then store UTC (CLAUDE.md §6). Treat them as ET wall-clock times, never as UTC.
2. Derive `is_early_close` as `close < 16:00`. Session minutes come from open/close: 390 normally, 210 on early closes. They feed the half-day volume normalization of the regime's indexes.
3. Exact agreement over 12 years means the disagreement rule will fire only on a genuine late change, such as an unscheduled closure that one source hasn't picked up yet. That is the case it exists for. Compare open and close times too, not just the date sets.

## Not covered

- Behaviour during an unscheduled closure announced at short notice (none occurred during the spike). The cross-check rule is there to catch it.
- How quickly `pandas_market_calendars` publishes a new closure. Keep the package updated, because a stale version is the likely source of a disagreement.
