"""Spike 0.3c: Alpaca's calendar vs pandas_market_calendars (NYSE), 2016-2027.

    uv run --group spikes python -m spikes.calendar

Prints every disagreement (missing sessions, different open/close times) and how early
closes look in each source. Findings: docs/spikes/calendar.md.
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pandas_market_calendars as pmc

from spikes._common import Env, Log

HERE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
START, END = date(2016, 1, 1), date(2027, 12, 31)
CALENDAR_URL = "https://paper-api.alpaca.markets/v2/calendar"

Session = tuple[time, time]


def alpaca_sessions(env: Env, log: Log) -> dict[date, Session]:
    response = httpx.get(
        CALENDAR_URL,
        params={"start": START.isoformat(), "end": END.isoformat()},
        headers=env.headers(),
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    log.event("alpaca_payload", count=len(rows), first=rows[0], last=rows[-1])
    return {
        date.fromisoformat(r["date"]): (
            time.fromisoformat(r["open"]),
            time.fromisoformat(r["close"]),
        )
        for r in rows
    }


def pmc_sessions() -> dict[date, Session]:
    schedule = pmc.get_calendar("NYSE").schedule(START.isoformat(), END.isoformat())
    out: dict[date, Session] = {}
    for idx, row in schedule.iterrows():
        opened: datetime = row["market_open"].tz_convert(ET).to_pydatetime()
        closed: datetime = row["market_close"].tz_convert(ET).to_pydatetime()
        out[idx.date()] = (opened.time(), closed.time())
    return out


def main() -> None:
    env = Env()
    log = Log(HERE, "calendar", env.secrets())
    alp, ref = alpaca_sessions(env, log), pmc_sessions()
    log.event(
        "coverage",
        alpaca_first=min(alp),
        alpaca_last=max(alp),
        alpaca_sessions=len(alp),
        pmc_first=min(ref),
        pmc_last=max(ref),
        pmc_sessions=len(ref),
    )
    only_alpaca = sorted(set(alp) - set(ref))
    only_pmc = sorted(set(ref) - set(alp))
    different = sorted(d for d in set(alp) & set(ref) if alp[d] != ref[d])
    log.event("only_in_alpaca", dates=only_alpaca)
    log.event("only_in_pmc", dates=only_pmc)
    log.event(
        "different_hours",
        rows=[{"date": d, "alpaca": alp[d], "pmc": ref[d]} for d in different],
    )
    regular: Session = (time(9, 30), time(16, 0))
    early = sorted(d for d, s in alp.items() if s != regular)
    log.event(
        "alpaca_non_regular_sessions",
        count=len(early),
        rows=[{"date": d, "alpaca": alp[d], "pmc": ref.get(d)} for d in early],
    )
    by_year: dict[int, int] = {}
    for d in alp:
        by_year[d.year] = by_year.get(d.year, 0) + 1
    log.event("alpaca_sessions_per_year", counts=by_year)


if __name__ == "__main__":
    main()
