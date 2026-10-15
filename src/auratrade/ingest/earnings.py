"""The earnings step (README §5.1, #119; spike 0.3e).

- **Upcoming dates:** Finnhub's calendar, one request per day from session T through
  `earnings_horizon_sessions` ahead. A blank timing (61% of reports) becomes the company's usual
  timing over its last `earnings_usual_timing_reports` EDGAR events, else before the open (the
  cushion runs a session earlier), marked `timing_inferred`. When every day was fetched, a
  stored upcoming date that is no longer listed (the date moved) is removed.
- **Surprises:** Finnhub's actual vs estimate for reports in the last `earnings_recent_sessions`
  sessions that have no actual yet, at most `earnings_surprise_max_requests` a run.
- **EDGAR events** (the history the usual timing reads, and the record backtests use): the 8-K
  carrying Item 2.02, timed by the earlier of its acceptance and the closest earnings headline.
  A report on a closed day counts as before the next session's open.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

import structlog
from sqlalchemy import Connection, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from auratrade.calendar.sessions import MarketCalendar
from auratrade.calendar.sources import NY
from auratrade.config import RiskConfig
from auratrade.data.edgar import Submissions
from auratrade.data.finnhub import CalendarEntry, EarningsTiming, Surprise
from auratrade.data.news import NewsItem
from auratrade.db.models import EarningsEvent, Ticker

log = structlog.get_logger(__name__)

FINNHUB, EDGAR = "FINNHUB", "EDGAR"
BEFORE_OPEN: EarningsTiming = "BEFORE_OPEN"
DURING_MARKET: EarningsTiming = "DURING_MARKET"
AFTER_CLOSE: EarningsTiming = "AFTER_CLOSE"
UNKNOWN: EarningsTiming = "UNKNOWN"
# The headline match of spike 0.3e: an earnings word, from 3 days before the 8-K to 1 day after.
# (Clock time around a filing, not a market window.)
EARNINGS_WORDS = re.compile(
    r"\b(Q[1-4]|quarter|quarterly|EPS|earnings|results|revenue|sales)\b", re.IGNORECASE
)
HEADLINE_BEFORE = timedelta(days=3)
HEADLINE_AFTER = timedelta(days=1)
# Ties in the usual timing go to the conservative side: the cushion acts earlier.
_TIE_ORDER: tuple[EarningsTiming, ...] = (BEFORE_OPEN, DURING_MARKET, AFTER_CLOSE)


# --- pure rules --------------------------------------------------------------------------


def moment_timing(moment: datetime, calendar: MarketCalendar) -> tuple[date, EarningsTiming]:
    """The report date and timing of a release at `moment`. On a closed day it counts as
    before the next session's open, which reacts to it the same way."""
    day = moment.astimezone(NY).date()
    session = calendar.session_for(day)
    if session is None:
        return calendar.next_session(day).day, BEFORE_OPEN
    if moment < session.open_at:
        return day, BEFORE_OPEN
    if moment >= session.close_at:
        return day, AFTER_CLOSE
    return day, DURING_MARKET


def closest_headline(items: Iterable[NewsItem], accepted_at: datetime) -> datetime | None:
    """The earnings headline closest to an 8-K's acceptance, within 3 days before to 1 after
    (the release itself, not a preview piece)."""
    times = [
        n.created_at
        for n in items
        if EARNINGS_WORDS.search(n.headline)
        and accepted_at - HEADLINE_BEFORE <= n.created_at <= accepted_at + HEADLINE_AFTER
    ]
    if not times:
        return None
    return min(times, key=lambda t: (abs((t - accepted_at).total_seconds()), t))


def usual_timing(history: Sequence[EarningsTiming]) -> EarningsTiming | None:
    """The most common known timing in `history` (ties → the earlier one)."""
    counts = Counter(t for t in history if t != UNKNOWN)
    if not counts:
        return None
    top = max(counts.values())
    return next(t for t in _TIE_ORDER if counts.get(t) == top)


def resolve_timing(
    vendor: EarningsTiming, history: Sequence[EarningsTiming]
) -> tuple[EarningsTiming, bool]:
    """(timing, inferred): the vendor's when given, else the usual one, else before the open."""
    if vendor != UNKNOWN:
        return vendor, False
    return usual_timing(history) or BEFORE_OPEN, True


def match_surprise(
    surprises: Sequence[Surprise], fiscal_year: int | None, fiscal_quarter: int | None, report: date
) -> Surprise | None:
    """The surprise for this report: by fiscal period when known, else the latest period that
    ended before the report."""
    if fiscal_year is not None and fiscal_quarter is not None:
        for s in surprises:
            if (s.fiscal_year, s.fiscal_quarter) == (fiscal_year, fiscal_quarter):
                return s
    earlier = [s for s in surprises if s.period < report]
    return max(earlier, key=lambda s: s.period) if earlier else None


# --- sources -----------------------------------------------------------------------------


class FinnhubSource(Protocol):
    def earnings_calendar(self, day: date) -> list[CalendarEntry]: ...
    def earnings_surprises(self, symbol: str) -> list[Surprise]: ...


class NewsSource(Protocol):
    def news(self, symbols: Iterable[str], start: datetime, end: datetime) -> list[NewsItem]: ...


# A CIK's EDGAR submissions (cached per run by the caller), None when EDGAR has no such CIK.
SubmissionsLookup = Callable[[int], Submissions | None]


@dataclass
class EarningsReport:
    horizon: tuple[date, date]
    days_fetched: int = 0
    days_failed: list[str] = field(default_factory=list)
    upcoming: int = 0
    inferred: int = 0
    moved: int = 0
    surprises: int = 0
    surprise_backlog: int = 0
    edgar_events: int = 0
    errors: list[str] = field(default_factory=list)

    def details(self) -> dict[str, object]:
        return {
            "horizon": [d.isoformat() for d in self.horizon],
            "days_fetched": self.days_fetched,
            "days_failed": self.days_failed,
            "upcoming": self.upcoming,
            "inferred": self.inferred,
            "moved": self.moved,
            "surprises": self.surprises,
            "surprise_backlog": self.surprise_backlog,
            "edgar_events": self.edgar_events,
            "errors": self.errors[:20],
        }


# --- the step ----------------------------------------------------------------------------


def ingest_earnings(
    conn: Connection,
    finnhub: FinnhubSource,
    submissions: SubmissionsLookup,
    news: NewsSource,
    calendar: MarketCalendar,
    risk: RiskConfig,
    session: date,
    now: datetime,
) -> EarningsReport:
    cfg = risk.ingest
    horizon_end = session
    for _ in range(cfg.earnings_horizon_sessions):
        horizon_end = calendar.next_session(horizon_end).day
    report = EarningsReport((session, horizon_end))
    tickers = _universe_ids(conn, risk)

    _upcoming(conn, finnhub, tickers, risk, session, horizon_end, report)
    recent_start = calendar.n_sessions_back(session, cfg.earnings_recent_sessions - 1).day
    _surprises(conn, finnhub, recent_start, session, cfg.earnings_surprise_max_requests, report)
    _edgar_events(conn, submissions, news, calendar, recent_start, session, now, report)
    return report


def _universe_ids(conn: Connection, risk: RiskConfig) -> dict[str, int]:
    rows = conn.execute(
        select(Ticker.symbol, Ticker.id).where(
            Ticker.active, Ticker.security_type.in_(risk.universe.security_types)
        )
    ).all()
    return {r.symbol: r.id for r in rows}


def _upcoming(
    conn: Connection,
    finnhub: FinnhubSource,
    tickers: Mapping[str, int],
    risk: RiskConfig,
    start: date,
    end: date,
    report: EarningsReport,
) -> None:
    entries: list[tuple[int, CalendarEntry]] = []
    day = start
    while day <= end:
        try:
            listed = finnhub.earnings_calendar(day)
        except Exception as exc:
            report.days_failed.append(day.isoformat())
            report.errors.append(f"calendar {day}: {type(exc).__name__}: {exc}")
        else:
            report.days_fetched += 1
            entries += [(tickers[e.symbol], e) for e in listed if e.symbol in tickers]
        day += timedelta(days=1)  # Finnhub's calendar is by calendar day, weekends included

    history = _timing_history(
        conn, {t for t, _ in entries}, risk.ingest.earnings_usual_timing_reports
    )
    rows = {}
    for ticker_id, e in entries:
        timing, inferred = resolve_timing(e.timing, history.get(ticker_id, ()))
        report.inferred += inferred
        rows[(ticker_id, e.report_date)] = {
            "ticker_id": ticker_id,
            "report_date": e.report_date,
            "timing": timing,
            "timing_inferred": inferred,
            "fiscal_year": e.fiscal_year,
            "fiscal_quarter": e.fiscal_quarter,
            "eps_estimate": e.eps_estimate,
            "eps_actual": e.eps_actual,
            "revenue_estimate": e.revenue_estimate,
            "revenue_actual": e.revenue_actual,
            "source": FINNHUB,
        }
    report.upcoming = len(rows)
    if rows:
        t = EarningsEvent
        stmt = insert(t).values(list(rows.values()))
        new = stmt.excluded
        # Actuals arrive later through the surprise endpoint; a calendar row without them
        # never erases them.
        kept = {"eps_actual": t.eps_actual, "revenue_actual": t.revenue_actual}
        values = {
            c: (func.coalesce(getattr(new, c), kept[c]) if c in kept else getattr(new, c))
            for c in (
                "timing",
                "timing_inferred",
                "fiscal_year",
                "fiscal_quarter",
                "eps_estimate",
                "eps_actual",
                "revenue_estimate",
                "revenue_actual",
            )
        }
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["ticker_id", "report_date", "source"],
                set_=values | {"fetched_at": new.fetched_at},
                where=tuple_(*(getattr(t, c) for c in values)).is_distinct_from(
                    tuple_(*values.values())
                ),
            )
        )
    if not report.days_failed:
        keys = list(rows)
        stale = delete(EarningsEvent).where(
            EarningsEvent.source == FINNHUB,
            EarningsEvent.report_date.between(start, end),
        )
        if keys:
            stale = stale.where(
                tuple_(EarningsEvent.ticker_id, EarningsEvent.report_date).not_in(keys)
            )
        report.moved = len(conn.execute(stale.returning(EarningsEvent.id)).all())


def _timing_history(
    conn: Connection, ticker_ids: Iterable[int], n: int
) -> dict[int, list[EarningsTiming]]:
    ids = list(ticker_ids)
    if not ids:
        return {}
    ranked = (
        select(
            EarningsEvent.ticker_id,
            EarningsEvent.timing,
            func.row_number()
            .over(partition_by=EarningsEvent.ticker_id, order_by=EarningsEvent.report_date.desc())
            .label("rank"),
        )
        .where(
            EarningsEvent.source == EDGAR,
            EarningsEvent.ticker_id.in_(ids),
            EarningsEvent.timing != UNKNOWN,
        )
        .subquery()
    )
    out: dict[int, list[EarningsTiming]] = {}
    for r in conn.execute(select(ranked.c.ticker_id, ranked.c.timing).where(ranked.c.rank <= n)):
        out.setdefault(r.ticker_id, []).append(r.timing)
    return out


def _surprises(
    conn: Connection,
    finnhub: FinnhubSource,
    start: date,
    end: date,
    cap: int,
    report: EarningsReport,
) -> None:
    due = conn.execute(
        select(
            EarningsEvent.id,
            EarningsEvent.report_date,
            EarningsEvent.fiscal_year,
            EarningsEvent.fiscal_quarter,
            EarningsEvent.eps_estimate,
            Ticker.symbol,
        )
        .join(Ticker, Ticker.id == EarningsEvent.ticker_id)
        .where(
            EarningsEvent.source == FINNHUB,
            EarningsEvent.report_date.between(start, end),
            EarningsEvent.eps_actual.is_(None),
            Ticker.active,
        )
        .order_by(EarningsEvent.report_date, Ticker.symbol)
    ).all()
    report.surprise_backlog = max(len(due) - cap, 0)
    for row in due[:cap]:
        try:
            found = finnhub.earnings_surprises(row.symbol)
        except Exception as exc:
            report.errors.append(f"surprise {row.symbol}: {type(exc).__name__}: {exc}")
            continue
        s = match_surprise(found, row.fiscal_year, row.fiscal_quarter, row.report_date)
        if s is None or s.actual is None:
            continue
        conn.execute(
            update(EarningsEvent)
            .where(EarningsEvent.id == row.id)
            .values(
                eps_actual=s.actual,
                eps_estimate=row.eps_estimate if row.eps_estimate is not None else s.estimate,
                surprise_pct=s.surprise_pct,
            )
        )
        report.surprises += 1


def _edgar_events(
    conn: Connection,
    submissions: SubmissionsLookup,
    news: NewsSource,
    calendar: MarketCalendar,
    start: date,
    end: date,
    now: datetime,
    report: EarningsReport,
) -> None:
    """EDGAR events for tickers with a report in [start, end] and none recorded from EDGAR."""
    recorded = aliased(EarningsEvent)
    has_edgar = (
        select(recorded.id)
        .where(
            recorded.ticker_id == Ticker.id,
            recorded.source == EDGAR,
            recorded.report_date >= start,
        )
        .exists()
    )
    due = conn.execute(
        select(Ticker.id, Ticker.symbol, Ticker.cik)
        .join(EarningsEvent, EarningsEvent.ticker_id == Ticker.id)
        .where(
            EarningsEvent.source == FINNHUB,
            EarningsEvent.report_date.between(start, end),
            Ticker.active,
            Ticker.cik.is_not(None),
            ~has_edgar,
        )
        .distinct()
    ).all()
    if not due:
        return
    first = datetime.combine(start, datetime.min.time(), tzinfo=NY)
    filings: dict[int, list[datetime]] = {}
    for row in due:
        try:
            subs = submissions(row.cik)
        except Exception as exc:
            report.errors.append(f"submissions {row.symbol}: {type(exc).__name__}: {exc}")
            continue
        if subs is None:
            continue
        filings[row.id] = [
            f.accepted_at for f in subs.earnings_releases() if first <= f.accepted_at <= now
        ]
    symbols = {r.id: r.symbol for r in due if filings.get(r.id)}
    if not symbols:
        return
    try:
        items = news.news(symbols.values(), first - HEADLINE_BEFORE, now)
    except Exception as exc:
        report.errors.append(f"news: {type(exc).__name__}: {exc}")
        items = []
    by_symbol: dict[str, list[NewsItem]] = {}
    for item in items:
        for symbol in item.symbols:
            by_symbol.setdefault(symbol, []).append(item)
    rows = []
    for ticker_id, symbol in symbols.items():
        for accepted in filings[ticker_id]:
            headline = closest_headline(by_symbol.get(symbol, ()), accepted)
            moment = min(accepted, headline) if headline is not None else accepted
            report_date, timing = moment_timing(moment, calendar)
            rows.append(
                {
                    "ticker_id": ticker_id,
                    "report_date": report_date,
                    "timing": timing,
                    "accepted_at": accepted,
                    "headline_at": headline,
                    "source": EDGAR,
                }
            )
    if rows:
        stmt = insert(EarningsEvent).values(rows)
        result = conn.execute(
            stmt.on_conflict_do_nothing(
                index_elements=["ticker_id", "report_date", "source"]
            ).returning(EarningsEvent.id)
        )
        report.edgar_events = len(result.all())
