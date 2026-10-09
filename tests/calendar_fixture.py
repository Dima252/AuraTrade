"""Alpaca's real calendar, 2025-12-01 to 2027-01-29, recorded from GET /v2/calendar."""

from __future__ import annotations

import json
from pathlib import Path

from auratrade.calendar.sessions import Session
from auratrade.calendar.sources import SessionTimes, parse_alpaca_calendar

FIXTURE = Path(__file__).parent / "fixtures/alpaca/calendar_2025-12_2027-01.json"


def alpaca_times() -> list[SessionTimes]:
    return parse_alpaca_calendar(json.loads(FIXTURE.read_text(encoding="utf-8")))


def to_session(s: SessionTimes) -> Session:
    return Session(s.day, s.open_at, s.close_at, s.is_early_close, s.minutes)
