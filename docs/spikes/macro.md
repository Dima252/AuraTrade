# Spike 0.3d: macro data timing (VIX, VIX3M, FRED)

**Run:** 2026-10-09 to 10-13: history checks, a weekend snapshot, and an evening watch on Monday 2026-10-12 (every 2 minutes, 11:05-22:17 ET).

**Result:** the evening regime reads VIX and VIX3M from **CBOE's delayed-quote JSON**, final soon after the close, with CBOE's history file and FRED as fallbacks (#26). CBOE's history files appear hours after the evening jobs and FRED a full session later, so the obvious source order would score the volatility pillar on yesterday's VIX every day.

## Sources

| Source | URL (free; FRED needs a key) |
|---|---|
| CBOE history files | `https://cdn.cboe.com/api/global/us_indices/daily_prices/{VIX,VIX3M}_History.csv` |
| CBOE delayed quotes | `https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{VIX,VIX3M}.json` |
| FRED | `VIXCLS`, `VXVCLS` (VIX3M), `BAMLH0A0HYM2` (ICE BofA high-yield spread) |

## Findings

| Question | Answer |
|---|---|
| History depth | VIX from 1990, VIX3M from 2009 (FRED's from 2007); no gaps from 2016 |
| CBOE against FRED | **Identical**: no difference over 2,740 VIX days and 2,707 VIX3M days |
| Extra rows | The VIX file has rows on US market holidays from 2022 (CBOE computes VIX in global hours); only `market_sessions` dates are kept |
| **When the delayed-quote JSON is final** | **16:32 ET** (the 16:15 settlement plus about 16 minutes of delay); unchanged for the rest of the evening. Accepted once its `last_trade_time` is 15 minutes past the close |
| JSON against the history row | **Equal**, both series, to the cent, on 2 of 2 samples |
| **When the history files update** | VIX3M around 18:31 ET, VIX around 21:11 ET, plus up to 30 minutes of CDN cache: after the 17:25 `regime` job |
| `Last-Modified` as a change signal | **Not usable:** the CDN serves two copies and the header flips; the file's last row date is checked instead |
| **FRED's lag** | One full session: the 2026-10-08 value appeared on 10-09 at 09:38 ET |
| The credit spread | Only 3 years of history on FRED: context for the LLM only, never part of a rule |

## What it means

- **Evening:** today's close from the delayed-quote JSON; the history file the next evening confirms it.
- **Fallbacks, in order:** the CBOE history file (if it already has the session), then FRED. Data more than 2 sessions stale in every source scores the volatility pillar 0 and turns the crisis override on (fail closed).
- **Backtests** use the CBOE history file, filtered to sessions.
