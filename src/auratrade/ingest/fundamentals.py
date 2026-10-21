"""Quarterly EPS and revenue from EDGAR company facts (README §5.1, #99; spike 0.3e).

- **Point in time:** each quarter keeps its **first-filed** value and is keyed on that filing's
  date; a backtest may read it only from the session after. Restatements in later filings are
  ignored, as the market saw the first number.
- **Tags:** companies switch revenue tags over time (ASC 606 in 2018), so they are merged by
  period, in the order below; EPS falls back to continuing-operations EPS (GD from 2018).
- **Q4** is seldom tagged: it is the 10-K's annual value minus the three quarters inside it,
  filed with the 10-K.
- **Growth** is year over year against the quarter ending about a year earlier, and only from a
  positive base; **acceleration** is this quarter's growth minus the previous quarter's.
  Foreign filers file no quarterly XBRL and get no rows: the quality score treats them as
  missing, never as zero (spike 0.3e).
- **Refresh:** tracked tickers in a rolling slice (each once per `fundamentals_refresh_sessions`)
  plus the ones that reported in the last `earnings_recent_sessions`, at most
  `fundamentals_max_requests` a run, recent reporters first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Protocol

from sqlalchemy import Connection, delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.data.edgar import CompanyFacts, FactValue
from auratrade.db.models import DailyBar, EarningsEvent, Fundamental, Ticker
from auratrade.ingest.bar_store import WRITE_CHUNK
from auratrade.ingest.companies import session_index

REVENUE_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "RevenuesNetOfInterestExpense",
)
EPS_TAGS = (
    "EarningsPerShareDiluted",
    "EarningsPerShareBasicAndDiluted",
    "IncomeLossFromContinuingOperationsPerDilutedShare",
)
QUARTERLY_FORMS = frozenset({"10-Q", "10-K", "10-Q/A", "10-K/A"})
# Period lengths in days (a fiscal quarter is 13 weeks give or take; a year 52-53 weeks).
QUARTER_DAYS = range(80, 101)
YEAR_DAYS = range(350, 381)
YEAR_AGO_DAYS = range(350, 381)
QUARTER_AGO_DAYS = range(80, 101)
# A first filing this long after the period end is a comparative figure that first appeared
# in a later report: its fiscal labels are the later report's, so the quarter is skipped.
MAX_FILING_LAG_DAYS = 120
_FISCAL_QUARTER = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}


@dataclass(frozen=True, slots=True)
class _Value:
    value: float
    start: date
    filed: date
    form: str
    accession: str
    fiscal_year: int | None
    fiscal_quarter: int | None


@dataclass(frozen=True, slots=True)
class Quarter:
    period_end: date
    fiscal_year: int
    fiscal_quarter: int
    filed: date
    form: str
    accession: str
    eps: float | None
    revenue: int | None
    eps_growth: float | None = None
    revenue_growth: float | None = None
    eps_acceleration: float | None = None
    revenue_acceleration: float | None = None


def _first_filed(values: Sequence[FactValue], best: dict[date, _Value], days: range) -> None:
    for v in values:
        if v.start is None or v.form not in QUARTERLY_FORMS:
            continue
        if (v.end - v.start).days not in days:
            continue
        current = best.get(v.end)
        if current is None or v.filed < current.filed:
            best[v.end] = _Value(
                v.value,
                v.start,
                v.filed,
                v.form,
                v.accession,
                v.fiscal_year,
                _FISCAL_QUARTER.get(v.fiscal_period or ""),
            )


def quarterly(facts: CompanyFacts, tags: Sequence[str], unit: str) -> dict[date, _Value]:
    """Period end → the first-filed 3-month value, with Q4 derived from the annual figure."""
    quarters: dict[date, _Value] = {}
    annual: dict[date, _Value] = {}
    for tag in tags:
        values = facts.values(tag, unit)
        _first_filed(values, quarters, QUARTER_DAYS)
        _first_filed(values, annual, YEAR_DAYS)
    for end, year in annual.items():
        if end in quarters:
            continue
        inside = [q for q in quarters if year.start < q < end and (end - q).days > 20]
        if len(inside) == 3:
            total = sum(quarters[q].value for q in inside)
            quarters[end] = replace(year, value=year.value - total, fiscal_quarter=4)
    return quarters


def _growth(now: float | None, before: float | None) -> float | None:
    if now is None or before is None or before <= 0:
        return None
    return now / before - 1


def _ago(ends: Sequence[date], end: date, days: range) -> date | None:
    return next((e for e in reversed(ends) if e < end and (end - e).days in days), None)


def quarters(facts: CompanyFacts) -> list[Quarter]:
    """Every quarter with EPS or revenue, oldest first, with growth and acceleration."""
    eps = quarterly(facts, EPS_TAGS, "USD/shares")
    revenue = quarterly(facts, REVENUE_TAGS, "USD")
    ends = sorted(eps.keys() | revenue.keys())
    rows: dict[date, Quarter] = {}
    for end in ends:
        e, r = eps.get(end), revenue.get(end)
        latest = max((v for v in (e, r) if v is not None), key=lambda v: v.filed)
        if (latest.filed - end).days > MAX_FILING_LAG_DAYS:
            continue
        if latest.fiscal_year is None or latest.fiscal_quarter is None:
            continue
        rows[end] = Quarter(
            end,
            latest.fiscal_year,
            latest.fiscal_quarter,
            latest.filed,
            latest.form,
            latest.accession,
            e.value if e is not None else None,
            round(r.value) if r is not None else None,
        )
    out: list[Quarter] = []
    kept = sorted(rows)
    for end in kept:
        q = rows[end]
        year_ago = _ago(kept, end, YEAR_AGO_DAYS)
        prior = rows.get(year_ago) if year_ago else None
        q = replace(
            q,
            eps_growth=_growth(q.eps, prior.eps if prior else None),
            revenue_growth=_growth(
                float(q.revenue) if q.revenue is not None else None,
                float(prior.revenue) if prior and prior.revenue is not None else None,
            ),
        )
        before = _ago(kept, end, QUARTER_AGO_DAYS)
        previous = rows.get(before) if before else None
        if previous is not None:
            q = replace(
                q,
                eps_acceleration=_diff(q.eps_growth, previous.eps_growth),
                revenue_acceleration=_diff(q.revenue_growth, previous.revenue_growth),
            )
        rows[end] = q
        out.append(q)
    return out


def _diff(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


# --- the step ----------------------------------------------------------------------------


class FactsSource(Protocol):
    def company_facts(self, cik: int) -> CompanyFacts | None: ...


@dataclass
class FundamentalsReport:
    due: int = 0
    fetched: int = 0
    backlog: int = 0
    rows_written: int = 0
    no_facts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def details(self) -> dict[str, object]:
        return {
            "due": self.due,
            "fetched": self.fetched,
            "backlog": self.backlog,
            "rows_written": self.rows_written,
            "no_facts": len(self.no_facts),
            "errors": self.errors[:20],
        }


def ingest_fundamentals(
    conn: Connection,
    source: FactsSource,
    calendar: MarketCalendar,
    risk: RiskConfig,
    session: date,
) -> FundamentalsReport:
    cfg = risk.ingest
    report = FundamentalsReport()
    n, index = cfg.fundamentals_refresh_sessions, session_index(calendar, session)
    recent_start = calendar.n_sessions_back(session, cfg.earnings_recent_sessions - 1).day
    reported = set(
        conn.execute(
            select(EarningsEvent.ticker_id).where(
                EarningsEvent.report_date.between(recent_start, session)
            )
        ).scalars()
    )
    tonight = select(DailyBar.ticker_id).where(DailyBar.session_date == session)
    tracked = conn.execute(
        select(Ticker.id, Ticker.symbol, Ticker.cik)
        .where(
            Ticker.active,
            Ticker.cik.is_not(None),
            Ticker.security_type.in_(risk.universe.security_types),
            Ticker.id.in_(tonight),
        )
        .order_by(Ticker.id)
    ).all()
    due = [r for r in tracked if r.id in reported] + [
        r for r in tracked if r.id not in reported and r.id % n == index % n
    ]
    report.due = len(due)
    report.backlog = max(len(due) - cfg.fundamentals_max_requests, 0)
    batch = due[: cfg.fundamentals_max_requests]

    def load(cik: int) -> CompanyFacts | Exception | None:
        try:
            return source.company_facts(cik)
        except Exception as exc:
            return exc

    # Company facts are megabytes each: fetched concurrently, EDGAR's limiter still applies.
    with ThreadPoolExecutor(max_workers=cfg.http_workers) as pool:
        fetched = list(pool.map(load, [row.cik for row in batch]))
    written: dict[int, list[Quarter]] = {}
    for row, facts in zip(batch, fetched, strict=True):
        if isinstance(facts, Exception):
            report.errors.append(f"facts {row.symbol}: {type(facts).__name__}: {facts}")
            continue
        report.fetched += 1
        if facts is None:
            report.no_facts.append(row.symbol)
            continue
        written[row.id] = quarters(facts)[-cfg.fundamentals_quarters :]
    report.rows_written = write_quarters(conn, written)
    return report


_VALUES = (
    "fiscal_year",
    "fiscal_quarter",
    "form",
    "accession",
    "eps_diluted",
    "revenue",
    "eps_growth",
    "revenue_growth",
    "eps_acceleration",
    "revenue_acceleration",
)


def write_quarters(conn: Connection, quarters_by_ticker: Mapping[int, Sequence[Quarter]]) -> int:
    """Upsert each ticker's quarters and drop its older ones; returns the rows changed."""
    rows = [
        {
            "ticker_id": ticker_id,
            "period_end": q.period_end,
            "filed_date": q.filed,
            "fiscal_year": q.fiscal_year,
            "fiscal_quarter": q.fiscal_quarter,
            "form": q.form,
            "accession": q.accession,
            "eps_diluted": q.eps,
            "revenue": q.revenue,
            "eps_growth": q.eps_growth,
            "revenue_growth": q.revenue_growth,
            "eps_acceleration": q.eps_acceleration,
            "revenue_acceleration": q.revenue_acceleration,
        }
        for ticker_id, qs in quarters_by_ticker.items()
        for q in qs
    ]
    written = 0
    # In slices: one statement carries at most 65,535 parameters (the backfill writes ~45k rows).
    for i in range(0, len(rows), WRITE_CHUNK):
        t = Fundamental
        stmt = insert(t).values(rows[i : i + WRITE_CHUNK])
        new = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["ticker_id", "period_end", "filed_date"],
            set_={c: getattr(new, c) for c in _VALUES},
            where=tuple_(*(getattr(t, c) for c in _VALUES)).is_distinct_from(
                tuple_(*(getattr(new, c) for c in _VALUES))
            ),
        )
        written += len(conn.execute(stmt.returning(t.id)).all())
    # Keep only the latest quarters per refreshed ticker (Neon's 0.5 GB).
    for ticker_id, qs in quarters_by_ticker.items():
        if qs:
            conn.execute(
                delete(Fundamental).where(
                    Fundamental.ticker_id == ticker_id, Fundamental.period_end < qs[0].period_end
                )
            )
    return written
