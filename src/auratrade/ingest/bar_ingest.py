"""The bars step of the evening ingest (README §4, §5.1).

1. The tracked set is **the ETF list** (`tracked_symbols`): the portfolio's funds, the
   satellite's list and the regime's index ETFs. Each must be an active row in `tickers`; one
   that isn't fails the step (fail closed).
2. Gap-fill each from its last stored session (which re-fetches session T-1, whose volume grows
   after the evening, #25); one with nothing recent stored gets its whole window.
3. Each fund's bars: raw close attached, sanity-checked, split check on the re-fetched overlap
   bar (a moved close re-fetches the whole window: a held fund can split, SSO has), outliers
   corrected with the stored bar before as context, upserted.
4. Coverage: the funds that traded at T-1 with a bar at T. The missing ones are fetched once
   more; the market fund or an index still missing marks the session `INCOMPLETE` (no increase
   of exposure next session, plus an alert); another fund missing is reported (no buy of it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

import structlog
from sqlalchemy import Connection, select

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.data.alpaca_data import Adjustment, DailyBar
from auratrade.db.models import Ticker
from auratrade.ingest.bar_store import (
    record_corrections,
    replace_window,
    set_data_status,
    stored_tail,
    upsert_bars,
)
from auratrade.ingest.bars import (
    Correction,
    IngestBar,
    apply_corrections,
    combine,
    coverage,
    find_outliers,
    sanity_problem,
    split_changed,
)

log = structlog.get_logger(__name__)

COMPLETE, INCOMPLETE = "COMPLETE", "INCOMPLETE"


class BarSource(Protocol):
    def daily_bars(
        self,
        symbols: Iterable[str],
        start: date,
        end: date,
        *,
        adjustment: Adjustment = Adjustment.SPLIT,
    ) -> dict[str, list[DailyBar]]: ...


class MissingTickerError(RuntimeError):
    """A fund on the ETF list isn't an active row in `tickers` (fail closed)."""


def tracked_symbols(risk: RiskConfig) -> list[str]:
    """The ETF list (README §5.1)."""
    p = risk.portfolio
    return sorted({p.market, p.leveraged, p.cash, *p.satellite_list, *risk.regime.indexes})


def active_ids(conn: Connection, symbols: Iterable[str]) -> dict[str, int]:
    wanted = list(symbols)
    rows = conn.execute(
        select(Ticker.symbol, Ticker.id).where(Ticker.active, Ticker.symbol.in_(wanted))
    ).all()
    return {row.symbol: row.id for row in rows}


def required_symbols(risk: RiskConfig) -> list[str]:
    """Without a bar for these the session is INCOMPLETE."""
    return sorted({risk.portfolio.market, *risk.regime.indexes})


@dataclass
class BarReport:
    session: date
    tracked: int = 0
    fetched_full: int = 0
    rows_written: int = 0
    splits: list[str] = field(default_factory=list)
    corrections: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    expected: int = 0
    received: int = 0
    coverage: float = 1.0
    retried: int = 0
    missing: list[str] = field(default_factory=list)
    """Funds that traded at T-1 without a bar at T."""
    missing_required: list[str] = field(default_factory=list)
    status: str = COMPLETE

    def details(self) -> dict[str, object]:
        return {
            "tracked": self.tracked,
            "fetched_full": self.fetched_full,
            "rows_written": self.rows_written,
            "splits": self.splits,
            "corrections": self.corrections,
            "dropped": self.dropped,
            "coverage": round(self.coverage, 4),
            "expected": self.expected,
            "received": self.received,
            "retried": self.retried,
            "missing": self.missing,
            "missing_required": self.missing_required,
            "data_status": self.status,
        }


@dataclass(frozen=True)
class _Window:
    days: Sequence[date]
    start: date  # first session kept in Postgres
    previous: date  # session T-1


def _window(calendar: MarketCalendar, session: date, sessions: int) -> _Window:
    first = calendar.first
    if first is None:
        raise ValueError("market_sessions is empty: sync the calendar")
    days = [s.day for s in calendar.sessions_between(first, session)]
    if len(days) < 2 or days[-1] != session:
        raise ValueError(f"{session} is not a session with one before it in the calendar")
    return _Window(days, days[max(len(days) - sessions, 0)], days[-2])


def _to_ingest(bars: Sequence[DailyBar]) -> list[IngestBar]:
    # The raw close is attached by `combine`; 0.0 marks "not attached yet".
    return [IngestBar(b.session, b.open, b.high, b.low, b.close, b.volume, 0.0) for b in bars]


def ingest_bars(
    conn: Connection,
    source: BarSource,
    calendar: MarketCalendar,
    risk: RiskConfig,
    session: date,
    now: datetime,
) -> BarReport:
    cfg = risk.ingest
    window = _window(calendar, session, cfg.bar_history_sessions)
    report = BarReport(session)

    wanted = tracked_symbols(risk)
    ids = active_ids(conn, wanted)
    absent = sorted(set(wanted) - ids.keys())
    if absent:
        raise MissingTickerError(f"funds on the ETF list not in tickers: {absent}")
    targets = {ticker_id: symbol for symbol, ticker_id in ids.items()}
    report.tracked = len(targets)
    # The stored bars serve the outlier check's range and left neighbour, and the overlap
    # re-fetch.
    tails = stored_tail(conn, list(targets), cfg.outliers.adr_sessions + 2)

    starts: dict[int, date] = {}
    for ticker_id in targets:
        tail = tails.get(ticker_id)
        if not tail or tail[-1].session < window.start:
            starts[ticker_id] = window.start
        else:
            starts[ticker_id] = min(tail[-1].session, window.previous)

    corrections: dict[int, list[Correction]] = {}
    written = _fetch_and_store(
        conn, source, targets, starts, tails, window, risk, session, corrections, report
    )

    # Coverage: the funds that traded at T-1 (stored, or in tonight's fetch).
    expected = {
        ticker_id
        for ticker_id in targets
        if ticker_id in written.get(window.previous, set())
        or any(b.session == window.previous for b in tails.get(ticker_id, ()))
    }
    received = written.get(session, set())
    missing = {t: targets[t] for t in expected - received}
    if missing:
        report.retried = len(missing)
        log.warning("ingest_short_count_retry", missing=sorted(missing.values()))
        retry_starts = {t: min(starts[t], window.previous) for t in missing}
        again = _fetch_and_store(
            conn, source, missing, retry_starts, tails, window, risk, session, corrections, report
        )
        received = received | again.get(session, set())
    report.expected, report.received = len(expected), len(expected & received)
    report.coverage = coverage(expected, received)
    report.missing = sorted(targets[t] for t in expected - received)
    report.missing_required = sorted(s for s in required_symbols(risk) if ids[s] not in received)
    record_corrections(conn, corrections, now)
    report.corrections = sum(len(found) for found in corrections.values())
    if report.missing_required:
        report.status = INCOMPLETE
    set_data_status(conn, session, report.status, report.coverage, now)
    return report


def _fetch_and_store(
    conn: Connection,
    source: BarSource,
    targets: Mapping[int, str],
    starts: Mapping[int, date],
    tails: Mapping[int, Sequence[IngestBar]],
    window: _Window,
    risk: RiskConfig,
    session: date,
    corrections: dict[int, list[Correction]],
    report: BarReport,
) -> dict[date, set[int]]:
    """Fetch, check and store; returns the tickers stored per session."""
    cfg = risk.ingest
    groups: dict[date, list[int]] = {}
    for ticker_id, start in starts.items():
        groups.setdefault(start, []).append(ticker_id)
    stored: dict[date, set[int]] = {}
    refetch: list[int] = []
    for start, ids in sorted(groups.items()):
        symbols = [targets[t] for t in ids]
        adjusted = source.daily_bars(symbols, start, session, adjustment=Adjustment.SPLIT)
        raw = source.daily_bars(symbols, start, session, adjustment=Adjustment.RAW)
        full = start == window.start
        report.fetched_full += len(ids) if full else 0
        batch: dict[int, list[IngestBar]] = {}
        for ticker_id in ids:
            symbol = targets[ticker_id]
            bars = _checked(symbol, adjusted.get(symbol, ()), raw.get(symbol, ()), report)
            tail = [] if full else list(tails.get(ticker_id, ()))
            if _split_moved(tail, bars, cfg.split_close_tolerance):
                report.splits.append(symbol)
                refetch.append(ticker_id)
                continue
            batch[ticker_id] = _corrected(ticker_id, tail, bars, risk, corrections)
            for b in batch[ticker_id]:
                stored.setdefault(b.session, set()).add(ticker_id)
        report.rows_written += upsert_bars(conn, batch)
    if refetch:
        symbols = [targets[t] for t in refetch]
        adjusted = source.daily_bars(symbols, window.start, session, adjustment=Adjustment.SPLIT)
        raw = source.daily_bars(symbols, window.start, session, adjustment=Adjustment.RAW)
        report.fetched_full += len(refetch)
        for ticker_id in refetch:
            symbol = targets[ticker_id]
            bars = _checked(symbol, adjusted.get(symbol, ()), raw.get(symbol, ()), report)
            fixed = _corrected(ticker_id, [], bars, risk, corrections)
            report.rows_written += replace_window(conn, ticker_id, fixed)
            log.info("ingest_split_refetched", symbol=symbol, bars=len(fixed))
            for b in fixed:
                stored.setdefault(b.session, set()).add(ticker_id)
    return stored


def _checked(
    symbol: str, adjusted: Sequence[DailyBar], raw: Sequence[DailyBar], report: BarReport
) -> list[IngestBar]:
    bars, no_raw = combine(_to_ingest(adjusted), {b.session: b.close for b in raw})
    if no_raw:
        report.dropped["no raw bar"] = report.dropped.get("no raw bar", 0) + len(no_raw)
    out = []
    for bar in bars:
        problem = sanity_problem(bar)
        if problem is None:
            out.append(bar)
        else:
            report.dropped[problem] = report.dropped.get(problem, 0) + 1
            if problem != "zero volume":
                log.warning(
                    "ingest_bar_dropped", symbol=symbol, session=str(bar.session), why=problem
                )
    return out


def _split_moved(tail: Sequence[IngestBar], bars: Sequence[IngestBar], tolerance: float) -> bool:
    stored = {b.session: b.close for b in tail}
    return any(
        split_changed(stored[b.session], b.close, tolerance) for b in bars if b.session in stored
    )


def _corrected(
    ticker_id: int,
    tail: Sequence[IngestBar],
    bars: Sequence[IngestBar],
    risk: RiskConfig,
    corrections: dict[int, list[Correction]],
) -> list[IngestBar]:
    """Outlier-corrected `bars`, with the stored bars before them as context."""
    if not bars:
        return []
    before = [b for b in tail if b.session < bars[0].session]
    found = [
        c
        for c in find_outliers([*before, *bars], risk.ingest.outliers)
        if c.session >= bars[0].session
    ]
    if found:
        corrections.setdefault(ticker_id, []).extend(found)
    return apply_corrections(bars, found)
