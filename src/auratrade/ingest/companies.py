"""Company data from EDGAR and Finnhub (README §5.1; spike 0.3e): CIK, SIC and industry groups,
SPAC shells, and the listing date.

- **CIK:** SEC's ticker map (class shares written `BRK-B` there, `BRK.B` at Alpaca).
- **SIC:** from EDGAR submissions, for tracked tickers without one and for a rolling slice (each
  tracked ticker once per `industries_refresh_sessions`). SIC 6770 marks a SPAC shell.
- **Industry groups:** a SIC code rolls up to its 3- and then 2-digit prefix until its group has
  `industry_min_group_size` tracked members (102 groups at 15 in the spike). Groups are named by
  their prefix ("SIC 367"), so a group keeps its row (and its RS history) across regroupings; a
  ticker belongs to the most specific group its code matches. Filers without a SIC code fall
  back to Finnhub's profile industry ("Finnhub: Semiconductors"). The rollup is recomputed on the
  first session of each refresh cycle, or when there are no groups yet.
- **Listing date** (#126): the first public trading date. A tracked ticker whose first stored bar
  comes after the start of the bar window listed inside it, unless EDGAR shows a periodic report
  (10-K, 10-Q, 20-F, 40-F) filed before that bar: then it traded publicly before (an OTC
  uplisting such as CELH, or a SPAC that merged), and the date stays unknown (NULL), which makes
  its base count unreliable.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import structlog
from sqlalchemy import Connection, bindparam, func, select, update
from sqlalchemy.dialects.postgresql import insert

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.data.edgar import CompanyTicker, Submissions
from auratrade.data.finnhub import Profile
from auratrade.db.models import DailyBar, IndustryGroup, Ticker
from auratrade.ingest.classify import SecurityType, with_sic

log = structlog.get_logger(__name__)

PERIODIC_FORMS = frozenset({"10-K", "10-Q", "20-F", "40-F", "10-KT", "10-QT", "10-KSB", "10-QSB"})
SIC_PREFIX = "SIC "
FINNHUB_PREFIX = "Finnhub: "


# --- pure rules --------------------------------------------------------------------------


def sec_symbol(symbol: str) -> str:
    return symbol.replace(".", "-")


def rollup(counts: Mapping[int, int], min_size: int) -> dict[int, str]:
    """Each SIC code's group key: the 4-digit code, or its 3- or 2-digit prefix while the group
    is smaller than `min_size`."""
    group = {sic: f"{sic:04d}" for sic in counts}
    for width in (3, 2):
        totals: Counter[str] = Counter()
        for sic, n in counts.items():
            totals[group[sic]] += n
        for sic in counts:
            if totals[group[sic]] < min_size:
                group[sic] = f"{sic:04d}"[:width]
    return group


def sic_range(key: str) -> list[int]:
    """The inclusive SIC range a group key covers ("367" → [3670, 3679])."""
    return [int(key.ljust(4, "0")), int(key.ljust(4, "9"))]


def group_for(sic: int, keys: Iterable[str]) -> str | None:
    """The most specific group key that `sic` falls in."""
    code = f"{sic:04d}"
    present = set(keys)
    return next((code[:w] for w in (4, 3, 2) if code[:w] in present), None)


def traded_before(subs: Submissions, first_bar: date) -> bool:
    """Whether EDGAR shows a periodic report filed before `first_bar`."""
    return any(f.form in PERIODIC_FORMS and f.filed < first_bar for f in subs.filings)


# --- sources -----------------------------------------------------------------------------


class SubmissionsCache:
    """One EDGAR submissions fetch per CIK per run; only filing pages reaching `since`."""

    def __init__(self, fetch: SubmissionsFetch, since: date) -> None:
        self._fetch = fetch
        self._since = since
        self._cache: dict[int, Submissions | None] = {}
        self.requests = 0

    def __call__(self, cik: int) -> Submissions | None:
        if cik not in self._cache:
            self.requests += 1
            self._cache[cik] = self._fetch.submissions(cik, since=self._since)
        return self._cache[cik]


class SubmissionsFetch(Protocol):
    def submissions(self, cik: int, *, since: date | None = ...) -> Submissions | None: ...


class CompanyTickersFetch(Protocol):
    def company_tickers(self) -> list[CompanyTicker]: ...


class ProfileFetch(Protocol):
    def profile(self, symbol: str) -> Profile | None: ...


@dataclass
class CompaniesReport:
    ciks_set: int = 0
    sic_checked: int = 0
    sic_changed: int = 0
    spacs: list[str] = field(default_factory=list)
    listed_at_set: list[str] = field(default_factory=list)
    traded_before_listing: list[str] = field(default_factory=list)
    profiles: int = 0
    regrouped: bool = False
    groups: int = 0
    assigned: int = 0
    industry_coverage: float = 1.0
    errors: list[str] = field(default_factory=list)

    def details(self) -> dict[str, object]:
        return {
            "ciks_set": self.ciks_set,
            "sic_checked": self.sic_checked,
            "sic_changed": self.sic_changed,
            "spacs": self.spacs,
            "listed_at_set": self.listed_at_set,
            "traded_before_listing": self.traded_before_listing,
            "profiles": self.profiles,
            "regrouped": self.regrouped,
            "groups": self.groups,
            "assigned": self.assigned,
            "industry_coverage": round(self.industry_coverage, 4),
            "errors": self.errors[:20],
        }


@dataclass(frozen=True)
class _Row:
    id: int
    symbol: str
    cik: int | None
    sic: int | None
    finnhub_industry: str | None
    security_type: str | None
    listed_at: date | None
    industry_group_id: int | None
    first_bar: date | None


def session_index(calendar: MarketCalendar, session: date) -> int:
    """The session's position in the synced calendar (for rolling refresh slices)."""
    if calendar.first is None:
        raise ValueError("market_sessions is empty: sync the calendar")
    return len(calendar.sessions_between(calendar.first, session)) - 1


def ingest_companies(
    conn: Connection,
    company_tickers: CompanyTickersFetch,
    submissions: SubmissionsCache,
    profiles: ProfileFetch | None,
    calendar: MarketCalendar,
    risk: RiskConfig,
    session: date,
) -> CompaniesReport:
    cfg = risk.ingest
    report = CompaniesReport()
    index = session_index(calendar, session)
    window_start = calendar.n_sessions_back(session, cfg.bar_history_sessions - 1).day

    _set_ciks(conn, company_tickers, risk, report)
    rows = _tracked(conn, risk, session)

    n = cfg.industries_refresh_sessions
    for row in rows:
        due_sic = row.cik is not None and (row.sic is None or row.id % n == index % n)
        listing = (
            row.listed_at is None and row.first_bar is not None and row.first_bar > window_start
        )
        if not (due_sic or (listing and row.cik is not None)):
            continue
        try:
            subs = submissions(row.cik) if row.cik is not None else None
        except Exception as exc:
            report.errors.append(f"submissions {row.symbol}: {type(exc).__name__}: {exc}")
            continue
        if subs is None:
            continue
        values: dict[str, object] = {}
        if due_sic:
            report.sic_checked += 1
            sic = int(subs.sic) if subs.sic and subs.sic.isdigit() else None
            if sic != row.sic:
                values["sic"] = sic
                report.sic_changed += 1
            kind = with_sic(SecurityType(row.security_type or "COMMON"), sic)
            if row.security_type and kind.value != row.security_type:
                values["security_type"] = kind.value
                report.spacs.append(row.symbol)
        if listing and row.first_bar is not None:
            if traded_before(subs, row.first_bar):
                report.traded_before_listing.append(row.symbol)
            else:
                values["listed_at"] = row.first_bar
                report.listed_at_set.append(row.symbol)
        if values:
            conn.execute(update(Ticker).where(Ticker.id == row.id).values(**values))

    if profiles is not None:
        _profiles(conn, profiles, risk, session, report)
    _industries(conn, risk, session, index, report)
    return report


def _set_ciks(
    conn: Connection, source: CompanyTickersFetch, risk: RiskConfig, report: CompaniesReport
) -> None:
    missing = conn.execute(
        select(Ticker.id, Ticker.symbol).where(
            Ticker.active,
            Ticker.cik.is_(None),
            Ticker.security_type.in_(risk.universe.security_types),
        )
    ).all()
    if not missing:
        return
    try:
        ciks = {t.ticker: t.cik for t in source.company_tickers()}
    except Exception as exc:
        report.errors.append(f"company_tickers: {type(exc).__name__}: {exc}")
        return
    params = [
        {"p_id": r.id, "p_cik": ciks[sec_symbol(r.symbol)]}
        for r in missing
        if sec_symbol(r.symbol) in ciks
    ]
    if params:
        conn.execute(
            update(Ticker).where(Ticker.id == bindparam("p_id")).values(cik=bindparam("p_cik")),
            params,
        )
    report.ciks_set = len(params)


def _tracked(conn: Connection, risk: RiskConfig, session: date) -> list[_Row]:
    """Active universe-type tickers with a bar tonight, with their first stored bar."""
    first_bar = (
        select(DailyBar.ticker_id, func.min(DailyBar.session_date).label("first_bar"))
        .group_by(DailyBar.ticker_id)
        .subquery()
    )
    tonight = select(DailyBar.ticker_id).where(DailyBar.session_date == session)
    rows = conn.execute(
        select(
            Ticker.id,
            Ticker.symbol,
            Ticker.cik,
            Ticker.sic,
            Ticker.finnhub_industry,
            Ticker.security_type,
            Ticker.listed_at,
            Ticker.industry_group_id,
            first_bar.c.first_bar,
        )
        .join(first_bar, first_bar.c.ticker_id == Ticker.id)
        .where(
            Ticker.active,
            Ticker.security_type.in_(risk.universe.security_types),
            Ticker.id.in_(tonight),
        )
        .order_by(Ticker.id)
    ).all()
    return [_Row(*r) for r in rows]


def _profiles(
    conn: Connection,
    profiles: ProfileFetch,
    risk: RiskConfig,
    session: date,
    report: CompaniesReport,
) -> None:
    """Finnhub's industry for tracked tickers that have no SIC code."""
    for row in _tracked(conn, risk, session):
        if row.sic is not None or row.finnhub_industry is not None:
            continue
        try:
            profile = profiles.profile(row.symbol)
        except Exception as exc:
            report.errors.append(f"profile {row.symbol}: {type(exc).__name__}: {exc}")
            continue
        report.profiles += 1
        if profile is not None and profile.industry:
            conn.execute(
                update(Ticker).where(Ticker.id == row.id).values(finnhub_industry=profile.industry)
            )


def _industries(
    conn: Connection, risk: RiskConfig, session: date, index: int, report: CompaniesReport
) -> None:
    rows = _tracked(conn, risk, session)
    existing = _groups(conn)
    n = risk.ingest.industries_refresh_sessions
    keys = {name.removeprefix(SIC_PREFIX) for name in existing if name.startswith(SIC_PREFIX)}
    if index % n == 0 or not keys:
        counts = Counter(r.sic for r in rows if r.sic is not None)
        keys = set(rollup(counts, risk.ingest.industry_min_group_size).values())
        report.regrouped = True
    wanted = {f"{SIC_PREFIX}{k}": [sic_range(k)] for k in keys}
    wanted |= {
        f"{FINNHUB_PREFIX}{r.finnhub_industry}": []
        for r in rows
        if r.sic is None and r.finnhub_industry
    }
    new = [
        {"name": name, "sic_ranges": ranges}
        for name, ranges in wanted.items()
        if name not in existing
    ]
    if new:
        conn.execute(insert(IndustryGroup).values(new).on_conflict_do_nothing())
        existing = _groups(conn)
    report.groups = len(wanted)

    params = []
    grouped = 0
    for r in rows:
        name = None
        if r.sic is not None:
            key = group_for(r.sic, keys)
            name = f"{SIC_PREFIX}{key}" if key is not None else None
        elif r.finnhub_industry:
            name = f"{FINNHUB_PREFIX}{r.finnhub_industry}"
        group_id = existing.get(name) if name is not None else None
        grouped += group_id is not None
        if group_id != r.industry_group_id:
            params.append({"p_id": r.id, "p_group": group_id})
    if params:
        conn.execute(
            update(Ticker)
            .where(Ticker.id == bindparam("p_id"))
            .values(industry_group_id=bindparam("p_group")),
            params,
        )
    report.assigned = len(params)
    report.industry_coverage = grouped / len(rows) if rows else 1.0


def _groups(conn: Connection) -> dict[str, int]:
    return {r.name: r.id for r in conn.execute(select(IndustryGroup.name, IndustryGroup.id))}
