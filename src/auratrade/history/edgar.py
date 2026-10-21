"""Backfill stage: earnings events and fundamentals from SEC EDGAR, point in time (README §5.1).

- **CIK:** SEC's ticker map lists today's tickers only, so it names the securities that still
  trade; a delisted security has no CIK here, and the backtests report that gap.
- **Earnings events:** every 8-K with Item 2.02 since the history start, timed by the earlier of
  its acceptance and the closest earnings headline in the news history (#119), by the same
  functions as the evening ingest.
- **Fundamentals:** every quarter from company facts, keyed on its first `filed` date
  (`ingest.fundamentals.quarters`); a backtest reads a quarter only from the session after.
- **Securities:** CIK, SIC, and the listing date cleared when a periodic report was filed before
  the first bar (an OTC uplisting, #126).

Each company's EDGAR answers are cached in the work folder (submissions as the few fields used,
quarters as computed), so an interrupted run resumes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import structlog

from auratrade.calendar.sessions import MarketCalendar
from auratrade.data.edgar import CompanyFacts, CompanyTicker, Submissions
from auratrade.data.news import NewsItem
from auratrade.history.build import SECURITIES, SECURITY_SCHEMA
from auratrade.history.news import missing_months, read_news
from auratrade.history.store import get_table, put_table, table_key
from auratrade.ingest.companies import PERIODIC_FORMS, sec_symbol
from auratrade.ingest.earnings import EARNINGS_WORDS, closest_headline, moment_timing
from auratrade.ingest.fundamentals import Quarter, quarters
from auratrade.storage import ObjectStore

log = structlog.get_logger(__name__)

EARNINGS = "earnings_events"
FUNDAMENTALS = "fundamentals"
EARNINGS_SCHEMA = pa.schema(
    [
        ("security", pa.string()),
        ("symbol", pa.string()),
        ("cik", pa.int64()),
        ("report_date", pa.date32()),
        ("timing", pa.string()),
        ("accepted_at", pa.timestamp("us", tz="UTC")),
        ("headline_at", pa.timestamp("us", tz="UTC")),
        ("accession", pa.string()),
    ]
)
FUNDAMENTALS_SCHEMA = pa.schema(
    [
        ("security", pa.string()),
        ("cik", pa.int64()),
        ("period_end", pa.date32()),
        ("fiscal_year", pa.int32()),
        ("fiscal_quarter", pa.int32()),
        ("filed_date", pa.date32()),
        ("form", pa.string()),
        ("accession", pa.string()),
        ("eps_diluted", pa.float64()),
        ("revenue", pa.int64()),
        ("eps_growth", pa.float64()),
        ("revenue_growth", pa.float64()),
        ("eps_acceleration", pa.float64()),
        ("revenue_acceleration", pa.float64()),
    ]
)


class EdgarHistorySource(Protocol):
    def company_tickers(self) -> list[CompanyTicker]: ...
    def submissions(self, cik: int, *, since: date | None = ...) -> Submissions | None: ...
    def company_facts(self, cik: int) -> CompanyFacts | None: ...


@dataclass(frozen=True)
class Company:
    """What the history keeps of a company's EDGAR answers."""

    cik: int
    sic: int | None
    # (accession, acceptance time) of each 8-K with Item 2.02.
    releases: list[tuple[str, datetime]]
    first_periodic: date | None  # the earliest 10-K, 10-Q, 20-F or 40-F filing
    quarters: list[Quarter]


def company(cik: int, subs: Submissions | None, facts: CompanyFacts | None) -> Company:
    releases = [(f.accession, f.accepted_at) for f in subs.earnings_releases()] if subs else []
    periodic = [f.filed for f in subs.filings if f.form in PERIODIC_FORMS] if subs else []
    sic = int(subs.sic) if subs and subs.sic and subs.sic.isdigit() else None
    return Company(
        cik,
        sic,
        sorted(releases, key=lambda r: r[1]),
        min(periodic) if periodic else None,
        quarters(facts) if facts is not None else [],
    )


def _to_json(c: Company) -> dict[str, Any]:
    return {
        "cik": c.cik,
        "sic": c.sic,
        "releases": [[a, t.isoformat()] for a, t in c.releases],
        "first_periodic": c.first_periodic.isoformat() if c.first_periodic else None,
        "quarters": [
            asdict(q) | {"period_end": q.period_end.isoformat(), "filed": q.filed.isoformat()}
            for q in c.quarters
        ],
    }


def _from_json(data: Mapping[str, Any]) -> Company:
    return Company(
        int(data["cik"]),
        data["sic"],
        [(a, datetime.fromisoformat(t).astimezone(UTC)) for a, t in data["releases"]],
        date.fromisoformat(data["first_periodic"]) if data["first_periodic"] else None,
        [
            Quarter(
                **(
                    q
                    | {
                        "period_end": date.fromisoformat(q["period_end"]),
                        "filed": date.fromisoformat(q["filed"]),
                    }
                )
            )
            for q in data["quarters"]
        ],
    )


def load_companies(
    source: EdgarHistorySource, ciks: Iterable[int], since: date, work: Path, *, workers: int
) -> tuple[dict[int, Company], list[str]]:
    folder = work / "edgar"
    folder.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    def one(cik: int) -> Company | None:
        path = folder / f"{cik}.json"
        if path.exists():
            return _from_json(json.loads(path.read_text(encoding="utf-8")))
        try:
            found = company(cik, source.submissions(cik, since=since), source.company_facts(cik))
        except Exception as exc:
            errors.append(f"{cik}: {type(exc).__name__}: {exc}")
            return None
        path.write_text(json.dumps(_to_json(found)), encoding="utf-8")
        return found

    wanted = sorted(set(ciks))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        loaded = dict(zip(wanted, pool.map(one, wanted), strict=True))
    return {cik: c for cik, c in loaded.items() if c is not None}, errors


def headline_index(items: Iterable[NewsItem]) -> dict[str, list[NewsItem]]:
    """Earnings-word headlines by tagged symbol."""
    out: dict[str, list[NewsItem]] = {}
    for item in items:
        if EARNINGS_WORDS.search(item.headline):
            for symbol in item.symbols:
                out.setdefault(symbol, []).append(item)
    return out


def earnings_rows(
    security: str,
    symbol: str,
    c: Company,
    headlines: Sequence[NewsItem],
    calendar: MarketCalendar,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    rows = []
    for accession, accepted in c.releases:
        if not start <= accepted <= end:
            continue
        headline = closest_headline(headlines, accepted)
        moment = min(accepted, headline) if headline is not None else accepted
        report_date, timing = moment_timing(moment, calendar)
        rows.append(
            {
                "security": security,
                "symbol": symbol,
                "cik": c.cik,
                "report_date": report_date,
                "timing": timing,
                "accepted_at": accepted,
                "headline_at": headline,
                "accession": accession,
            }
        )
    return rows


def fundamental_rows(security: str, c: Company) -> list[dict[str, Any]]:
    return [
        {
            "security": security,
            "cik": c.cik,
            "period_end": q.period_end,
            "fiscal_year": q.fiscal_year,
            "fiscal_quarter": q.fiscal_quarter,
            "filed_date": q.filed,
            "form": q.form,
            "accession": q.accession,
            "eps_diluted": q.eps,
            "revenue": q.revenue,
            "eps_growth": q.eps_growth,
            "revenue_growth": q.revenue_growth,
            "eps_acceleration": q.eps_acceleration,
            "revenue_acceleration": q.revenue_acceleration,
        }
        for q in c.quarters
    ]


@dataclass
class EdgarReport:
    securities_with_cik: int = 0
    companies: int = 0
    earnings_events: int = 0
    with_headline: int = 0
    fundamentals: int = 0
    listed_at_cleared: int = 0
    errors: list[str] = field(default_factory=list)

    def details(self) -> dict[str, object]:
        return {
            "securities_with_cik": self.securities_with_cik,
            "companies": self.companies,
            "earnings_events": self.earnings_events,
            "with_headline": self.with_headline,
            "fundamentals": self.fundamentals,
            "listed_at_cleared": self.listed_at_cleared,
            "errors": self.errors[:20],
        }


class NewsNotFetchedError(RuntimeError):
    """The earnings timing needs the news history first."""


def edgar_history(
    store: ObjectStore,
    source: EdgarHistorySource,
    calendar: MarketCalendar,
    start: date,
    end: date,
    work: Path,
    *,
    workers: int,
) -> EdgarReport:
    report = EdgarReport()
    table = get_table(store, table_key(SECURITIES))
    if table is None:
        raise FileNotFoundError("no securities table: run the bars stage first")
    securities = table.to_pylist()
    ciks = {t.ticker: t.cik for t in source.company_tickers()}
    for s in securities:
        s["cik"] = ciks.get(sec_symbol(s["symbol"])) if s["active"] else None
    named = [s for s in securities if s["cik"] is not None]
    report.securities_with_cik = len(named)
    companies, report.errors = load_companies(
        source, (s["cik"] for s in named), start, work, workers=workers
    )
    report.companies = len(companies)
    # Checked after the downloads, so this stage can fill its cache while the news is fetched.
    missing = missing_months(store, start, end)
    if missing:
        raise NewsNotFetchedError(f"news history missing for {missing[:6]} ({len(missing)})")

    headlines = headline_index(read_news(store, start, end))
    first = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    last = datetime.combine(end, datetime.max.time(), tzinfo=UTC)
    events: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    for s in named:
        c = companies.get(s["cik"])
        if c is None:
            continue
        s["sic"] = c.sic
        if s["listed_at"] is not None and c.first_periodic and c.first_periodic < s["listed_at"]:
            s["listed_at"] = None
            report.listed_at_cleared += 1
        events += earnings_rows(
            s["security"], s["symbol"], c, headlines.get(s["symbol"], ()), calendar, first, last
        )
        facts += fundamental_rows(s["security"], c)
    report.earnings_events = len(events)
    report.with_headline = sum(e["headline_at"] is not None for e in events)
    report.fundamentals = len(facts)
    put_table(store, table_key(EARNINGS), pa.Table.from_pylist(events, EARNINGS_SCHEMA))
    put_table(store, table_key(FUNDAMENTALS), pa.Table.from_pylist(facts, FUNDAMENTALS_SCHEMA))
    put_table(store, table_key(SECURITIES), pa.Table.from_pylist(securities, SECURITY_SCHEMA))
    return report
