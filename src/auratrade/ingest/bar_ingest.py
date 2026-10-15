"""The bars step of the evening ingest (README §4, §5.1).

1. Screen the candidates on raw bars of the last `dollar_volume_sessions` sessions; the tracked
   set is the ones passing plus the regime's index ETFs.
2. Gap-fill each tracked ticker from its last stored session (which re-fetches session T-1, whose
   volume grows after the evening, #118); a ticker with nothing recent stored gets its whole
   window.
3. Each ticker's bars: raw close attached, sanity-checked, split check on the re-fetched overlap
   bar (a moved close re-fetches the whole window), outliers corrected with the stored bar before
   as context, upserted.
4. Coverage: the share of tickers that traded at T-1 with a bar at T. Under `data.min_coverage`
   the missing ones are fetched once more; still under it, or an index missing, marks the session
   `INCOMPLETE` (no new entries next session, plus an alert).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

import structlog
from sqlalchemy import Connection

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.data.alpaca_data import Adjustment, DailyBar
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
from auratrade.ingest.universe import active_ids, candidates, tracked

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


class MissingIndexError(RuntimeError):
    """An index ETF the regime needs isn't in `tickers` (fail closed)."""


@dataclass
class BarReport:
    session: date
    candidates: int = 0
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
    missing_indexes: list[str] = field(default_factory=list)
    status: str = COMPLETE

    def details(self) -> dict[str, object]:
        return {
            "candidates": self.candidates,
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
            "missing_indexes": self.missing_indexes,
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
    u, cfg = risk.universe, risk.ingest
    window = _window(calendar, session, cfg.bar_history_sessions)
    report = BarReport(session)

    pool = candidates(conn, u)
    report.candidates = len(pool)
    screen_start = window.days[max(len(window.days) - u.dollar_volume_sessions, 0)]
    recent_raw = source.daily_bars(
        [c.symbol for c in pool], screen_start, session, adjustment=Adjustment.RAW
    )
    targets = {c.ticker_id: c.symbol for c in tracked(pool, recent_raw, u, margin=cfg.track_margin)}
    indexes = active_ids(conn, risk.regime.indexes)
    absent = sorted(set(risk.regime.indexes) - indexes.keys())
    if absent:
        raise MissingIndexError(f"index ETFs not in tickers: {absent}")
    targets |= {ticker_id: symbol for symbol, ticker_id in indexes.items()}
    report.tracked = len(targets)

    # The overlap re-fetch starts at T-1 at the latest; the outlier check needs the stored bars
    # before it for the left neighbour and the average daily range.
    tails = stored_tail(conn, targets, cfg.outliers.adr_sessions + 2)
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

    # Coverage: tickers that traded at T-1 (in tonight's screen data, or stored) and the indexes.
    traded_before = {
        ticker_id
        for ticker_id, symbol in targets.items()
        if any(b.session == window.previous for b in recent_raw.get(symbol, ()))
        or any(b.session == window.previous for b in tails.get(ticker_id, ()))
    } | set(indexes.values())
    received = written.get(session, set())
    if coverage(traded_before, received) < risk.data.min_coverage:
        missing = {t: targets[t] for t in traded_before - received}
        report.retried = len(missing)
        log.warning("ingest_short_count_retry", missing=len(missing), expected=len(traded_before))
        retry_starts = {t: min(starts[t], window.previous) for t in missing}
        again = _fetch_and_store(
            conn, source, missing, retry_starts, tails, window, risk, session, corrections, report
        )
        received = received | again.get(session, set())
    report.expected, report.received = len(traded_before), len(traded_before & received)
    report.coverage = coverage(traded_before, received)
    report.missing_indexes = sorted(s for s, t in indexes.items() if t not in received)
    record_corrections(conn, corrections, now)
    report.corrections = sum(len(found) for found in corrections.values())
    if report.coverage < risk.data.min_coverage or report.missing_indexes:
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
