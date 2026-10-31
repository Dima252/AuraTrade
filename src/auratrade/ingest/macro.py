"""The macro step (README §5.1, #26; spike 0.3d).

- **Evening VIX/VIX3M for session T:** CBOE's delayed-quote JSON, accepted only when its
  `last_trade_time` is on T and at least `quote_settle_minutes` after the session's close (16:15
  on a full session); otherwise the CBOE history file if its last row is T; otherwise FRED, which
  is a session behind; otherwise nothing, and the regime's staleness rule applies (§4).
- **Confirmation:** each evening the history files' recent rows replace earlier quote and FRED
  values, filtered to sessions (the VIX file has rows on holidays); a quote value that differs
  from its history row by more than the tolerance is logged.
- **Credit spread:** FRED, stored for the Portfolio Manager's context only.

A value is never replaced by a lower-ranked source: history file > delayed quote > FRED.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from typing import Protocol
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.data.macro import CboeHistory, CboeIndex, DelayedQuote, FredSeries
from auratrade.db.models import MacroObservation

log = structlog.get_logger(__name__)
NY = ZoneInfo("America/New_York")

CBOE_HISTORY, CBOE_QUOTE, FRED = "CBOE_HISTORY", "CBOE_QUOTE", "FRED"
_RANK = {CBOE_HISTORY: 3, CBOE_QUOTE: 2, FRED: 1}
CREDIT_SPREAD = "CREDIT_SPREAD"
_FRED_FOR = {CboeIndex.VIX: FredSeries.VIX, CboeIndex.VIX3M: FredSeries.VIX3M}


class CboeSource(Protocol):
    def delayed_quote(self, index: CboeIndex) -> DelayedQuote: ...
    def history(self, index: CboeIndex) -> CboeHistory: ...


class FredSource(Protocol):
    def observations(self, series: FredSeries, start: date, end: date) -> dict[date, float]: ...


@dataclass
class MacroReport:
    session: date
    sources: dict[str, str | None] = field(default_factory=dict)
    written: int = 0
    mismatches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def details(self) -> dict[str, object]:
        return {
            "sources": self.sources,
            "written": self.written,
            "mismatches": self.mismatches,
            "errors": self.errors,
        }

    @property
    def missing(self) -> list[str]:
        return [name for name, source in self.sources.items() if source is None]


def quote_is_final(quote: DelayedQuote, session: date, close_at: datetime, settle: int) -> bool:
    last = quote.last_trade_at.astimezone(NY)
    return last.date() == session and quote.last_trade_at >= close_at + timedelta(minutes=settle)


def _attempt[T](report: MacroReport, what: str, call: Callable[[], T]) -> T | None:
    try:
        return call()
    except Exception as exc:
        report.errors.append(f"{what}: {type(exc).__name__}: {exc}")
        log.warning("macro_source_failed", source=what, error=f"{type(exc).__name__}: {exc}")
        return None


def ingest_macro(
    conn: Connection,
    cboe: CboeSource,
    fred: FredSource | None,
    calendar: MarketCalendar,
    risk: RiskConfig,
    session: date,
) -> MacroReport:
    cfg = risk.ingest.macro
    report = MacroReport(session)
    today = calendar.session_for(session)
    if today is None:
        raise ValueError(f"{session} is not a session")
    lookback = calendar.n_sessions_back(session, cfg.fred_lookback_sessions).day
    recent = {s.day for s in calendar.sessions_between(lookback, session)}
    values: dict[tuple[str, date], tuple[float, str]] = {}

    for index in (CboeIndex.VIX, CboeIndex.VIX3M):
        history = _attempt(report, f"cboe history {index}", partial(cboe.history, index))
        if history is not None:
            for day, value in history.closes.items():
                if day in recent:
                    values[(index.value, day)] = (value, CBOE_HISTORY)
        source = CBOE_HISTORY if (index.value, session) in values else None
        if source is None:
            quote = _attempt(report, f"cboe quote {index}", partial(cboe.delayed_quote, index))
            if quote is not None and quote_is_final(
                quote, session, today.close_at, cfg.quote_settle_minutes
            ):
                values[(index.value, session)] = (quote.close, CBOE_QUOTE)
                source = CBOE_QUOTE
        if source is None and fred is not None:
            observed = _attempt(
                report,
                f"fred {_FRED_FOR[index]}",
                partial(fred.observations, _FRED_FOR[index], lookback, session),
            )
            for day, value in (observed or {}).items():
                values.setdefault((index.value, day), (value, FRED))
            if observed:
                source = FRED
        report.sources[index.value] = source

    if fred is not None:
        spread = _attempt(
            report,
            "fred credit spread",
            partial(fred.observations, FredSeries.CREDIT_SPREAD, lookback, session),
        )
        for day, value in (spread or {}).items():
            values[(CREDIT_SPREAD, day)] = (value, FRED)
        report.sources[CREDIT_SPREAD] = FRED if spread else None

    report.written = _write(conn, values, cfg.history_mismatch_tolerance, report)
    return report


def _write(
    conn: Connection,
    values: Mapping[tuple[str, date], tuple[float, str]],
    tolerance: float,
    report: MacroReport,
) -> int:
    if not values:
        return 0
    series = {k[0] for k in values}
    days = {k[1] for k in values}
    existing = {
        (r.series, r.observation_date): (r.value, r.source)
        for r in conn.execute(
            select(
                MacroObservation.series,
                MacroObservation.observation_date,
                MacroObservation.value,
                MacroObservation.source,
            ).where(
                MacroObservation.series.in_(series),
                MacroObservation.observation_date.in_(days),
            )
        )
    }
    rows = []
    for key, (value, source) in sorted(values.items()):
        old = existing.get(key)
        if old is not None:
            old_value, old_source = old
            if _RANK.get(source, 0) < _RANK.get(old_source, 0):
                continue
            if (value, source) == (old_value, old_source):
                continue
            if (
                old_source == CBOE_QUOTE
                and source == CBOE_HISTORY
                and abs(value - old_value) > tolerance
            ):
                note = f"{key[0]} {key[1]}: quote {old_value} vs history {value}"
                report.mismatches.append(note)
                log.warning("macro_quote_history_mismatch", detail=note)
        rows.append(
            {"series": key[0], "observation_date": key[1], "value": value, "source": source}
        )
    if rows:
        stmt = insert(MacroObservation).values(rows)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["series", "observation_date"],
                set_={
                    "value": stmt.excluded.value,
                    "source": stmt.excluded.source,
                    "fetched_at": stmt.excluded.fetched_at,
                },
            )
        )
    return len(rows)
