"""Database I/O for the evening's full scan: its inputs from Postgres, ``daily_watchlist`` out."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, delete, insert, select
from sqlalchemy.dialects.postgresql import distinct_on

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import Regime, RiskConfig
from auratrade.db.models import (
    DailyBar,
    EarningsEvent,
    Fundamental,
    IndustryGroup,
    MarketRegimeRow,
    Ticker,
    WatchlistRow,
)
from auratrade.indicators.bars import Bar
from auratrade.indicators.earnings import EarningsTiming
from auratrade.regime.inputs import MissingDataError
from auratrade.strategies.ranking import Fundamentals
from auratrade.strategies.verdict_cache import structure
from auratrade.strategies.watchlist import SymbolHistory, WatchItem

BENCHMARK = "SPY"
WRITE_CHUNK = 500


@dataclass(frozen=True, slots=True)
class ScanInputs:
    histories: list[SymbolHistory]
    benchmark: dict[date, float]
    regimes: dict[date, Regime]
    """The computed regime of each loaded session that has one."""
    regime: Regime
    """Session T's computed regime: it selects tonight's list."""


def load_inputs(
    conn: Connection, calendar: MarketCalendar, session: date, risk: RiskConfig
) -> ScanInputs:
    """Everything the full scan reads for session ``session``: the universe's bars over the
    window Postgres holds, SPY's closes, the regimes, earnings dates, the latest fundamentals
    filed before the session and each ticker's industry group. The window is cut short where
    the calendar begins."""
    if calendar.first is None:
        raise MissingDataError("market_sessions is empty: sync the calendar")
    days = calendar.sessions_between(calendar.first, session)
    start = days[max(len(days) - risk.ingest.bar_history_sessions, 0)].day
    u = risk.universe
    rows = conn.execute(
        select(
            DailyBar.ticker_id,
            Ticker.symbol,
            IndustryGroup.name,
            DailyBar.session_date,
            DailyBar.open,
            DailyBar.high,
            DailyBar.low,
            DailyBar.close,
            DailyBar.volume,
            DailyBar.raw_close,
        )
        .join(Ticker, Ticker.id == DailyBar.ticker_id)
        .outerjoin(IndustryGroup, IndustryGroup.id == Ticker.industry_group_id)
        .where(
            DailyBar.session_date.between(start, session),
            Ticker.active,
            Ticker.security_type.in_(u.security_types),
            Ticker.exchange.is_(None) | Ticker.exchange.in_(u.exchanges),
        )
        .order_by(DailyBar.ticker_id, DailyBar.session_date)
    ).all()
    grouped: dict[int, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[row.ticker_id].append(row)
    ids = [t for t, bars in grouped.items() if bars[-1].session_date == session]
    earnings = _earnings(conn, ids, start, session)
    fundamentals = _fundamentals(conn, ids, session)
    histories = []
    for ticker_id in ids:
        bars = grouped[ticker_id]
        histories.append(
            SymbolHistory(
                ticker_id=ticker_id,
                symbol=bars[-1].symbol,
                dates=[b.session_date for b in bars],
                bars=[Bar(b.open, b.high, b.low, b.close, float(b.volume)) for b in bars],
                raw_closes=[b.raw_close for b in bars],
                industry=bars[-1].name,
                earnings=earnings.get(ticker_id, ()),
                fundamentals=fundamentals.get(ticker_id),
            )
        )
    benchmark = dict(
        conn.execute(
            select(DailyBar.session_date, DailyBar.close)
            .join(Ticker, Ticker.id == DailyBar.ticker_id)
            .where(Ticker.symbol == BENCHMARK, Ticker.active)
            .where(DailyBar.session_date.between(start, session))
        ).all()
    )
    regimes = {
        day: Regime(label)
        for day, label in conn.execute(
            select(MarketRegimeRow.session_date, MarketRegimeRow.computed_regime).where(
                MarketRegimeRow.session_date.between(start, session)
            )
        ).all()
    }
    if session not in regimes:
        raise MissingDataError(f"no computed regime for {session}: run the regime job first")
    return ScanInputs(histories, benchmark, regimes, regimes[session])


def _earnings(
    conn: Connection, ids: Sequence[int], start: date, session: date
) -> dict[int, list[tuple[date, EarningsTiming]]]:
    out: dict[int, list[tuple[date, EarningsTiming]]] = defaultdict(list)
    rows = conn.execute(
        select(EarningsEvent.ticker_id, EarningsEvent.report_date, EarningsEvent.timing)
        .where(
            EarningsEvent.ticker_id.in_(ids),
            EarningsEvent.report_date.between(start, session),
            EarningsEvent.timing != "UNKNOWN",
        )
        .order_by(EarningsEvent.ticker_id, EarningsEvent.report_date)
        .distinct()
    ).all()
    for ticker_id, day, timing in rows:
        out[ticker_id].append((day, EarningsTiming(timing.lower())))
    return dict(out)


def _fundamentals(conn: Connection, ids: Sequence[int], session: date) -> dict[int, Fundamentals]:
    """The latest quarter filed before ``session`` (archived README §15: readable from the next
    one)."""
    rows = conn.execute(
        select(Fundamental.ticker_id, Fundamental.eps_growth, Fundamental.eps_acceleration)
        .where(Fundamental.ticker_id.in_(ids), Fundamental.filed_date < session)
        .order_by(
            Fundamental.ticker_id,
            Fundamental.filed_date.desc(),
            Fundamental.period_end.desc(),
        )
        .ext(distinct_on(Fundamental.ticker_id))
    ).all()
    return {t: Fundamentals(growth, acceleration) for t, growth, acceleration in rows}


def _json(value: Any) -> Any:
    """A fact as JSON: floats rounded, enums by value."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    return getattr(value, "value", value)


def watchlist_rows(
    items: Sequence[WatchItem], *, session: date, regime: Regime, risk_hash: str
) -> list[dict[str, Any]]:
    return [
        {
            "session_date": session,
            "ticker_id": item.ticker_id,
            "symbol": item.symbol,
            "strategy": item.setup.strategy.value,
            "pivot": item.setup.pivot,
            "structural_stop": item.setup.structural_stop,
            "structure": float(structure(item.setup)),
            "score": item.score,
            "rank": item.rank,
            "regime": regime.value,
            "close": item.close,
            "atr": item.atr,
            "adr": item.adr,
            "sma_50": item.sma_50,
            "dollar_volume": item.dollar_volume,
            "base_stage": item.features.base_stage,
            "base_reliable": item.features.base_reliable,
            "industry": item.industry,
            "components": _json(dict(item.components)),
            "facts": _json({**asdict(item.features), **item.setup.detail.facts()}),
            "risk_hash": risk_hash,
        }
        for item in items
    ]


def write_watchlist(
    conn: Connection,
    items: Sequence[WatchItem],
    *,
    session: date,
    regime: Regime,
    risk_hash: str,
) -> int:
    """Replace session ``session``'s watchlist (a re-run writes the same rows)."""
    conn.execute(delete(WatchlistRow).where(WatchlistRow.session_date == session))
    rows = watchlist_rows(items, session=session, regime=regime, risk_hash=risk_hash)
    for i in range(0, len(rows), WRITE_CHUNK):
        conn.execute(insert(WatchlistRow), rows[i : i + WRITE_CHUNK])
    return len(rows)
