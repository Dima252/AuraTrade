"""Database I/O for the regime: load bars and macro data, read and write ``market_regime``."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import Connection, and_, case, select
from sqlalchemy.dialects.postgresql import insert

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RegimeConfig
from auratrade.db.models import DailyBar, MacroObservation, MarketRegimeRow, Ticker
from auratrade.regime.classifier import RegimeResult
from auratrade.regime.inputs import (
    VIX,
    VIX3M,
    LoadedData,
    MacroSeries,
    MissingDataError,
    TickerHistory,
)
from auratrade.regime.state import RegimeState

# Sessions of macro history loaded before the first target: enough to measure staleness.
MACRO_LOOKBACK_SESSIONS = 10
WRITE_CHUNK = 500


def lookback_sessions(cfg: RegimeConfig) -> int:
    """Sessions of bars needed before the first target for every window to be full."""
    return cfg.index_trend.long_sma + cfg.index_trend.slope_sessions


def _histories(
    rows: Sequence[tuple[int, date, float, float, float, float, int]],
) -> dict[int, TickerHistory]:
    grouped: dict[int, list[tuple[date, float, float, float, float, int]]] = defaultdict(list)
    for ticker_id, day, high, low, close, raw_close, volume in rows:
        grouped[ticker_id].append((day, high, low, close, raw_close, volume))
    out = {}
    for ticker_id, bars in grouped.items():
        bars.sort()
        out[ticker_id] = TickerHistory(
            dates=[b[0] for b in bars],
            highs=[b[1] for b in bars],
            lows=[b[2] for b in bars],
            closes=[b[3] for b in bars],
            raw_closes=[b[4] for b in bars],
            volumes=[float(b[5]) for b in bars],
        )
    return out


_BAR_COLUMNS = (
    DailyBar.ticker_id,
    DailyBar.session_date,
    DailyBar.high,
    DailyBar.low,
    DailyBar.close,
    DailyBar.raw_close,
    DailyBar.volume,
)


def load_data(
    conn: Connection,
    calendar: MarketCalendar,
    first: date,
    last: date,
    cfg: RegimeConfig,
) -> LoadedData:
    """Bars and macro data for classifying ``first`` .. ``last``, with the look-back before
    ``first`` (cut short where the calendar or the stored bars begin)."""
    if calendar.first is None:
        raise MissingDataError("market_sessions is empty: sync the calendar")
    all_sessions = calendar.sessions_between(calendar.first, last)
    days = [s.day for s in all_sessions]
    start = days[max(days.index(first) - lookback_sessions(cfg), 0)]
    sessions = days[days.index(start) :]
    minutes = {s.day: s.minutes for s in all_sessions if s.day >= start}

    in_range = DailyBar.session_date.between(start, last)
    rows = conn.execute(
        select(*_BAR_COLUMNS, Ticker.symbol)
        .join(Ticker, Ticker.id == DailyBar.ticker_id)
        .where(in_range, Ticker.symbol.in_(cfg.indexes))
    ).all()
    by_symbol: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        by_symbol[row.symbol].add(row.ticker_id)
    histories = _histories([tuple(r)[:7] for r in rows])  # type: ignore[misc]
    indexes = {}
    for symbol, ids in by_symbol.items():
        if len(ids) > 1:
            raise MissingDataError(f"{symbol} maps to several securities in range: {sorted(ids)}")
        indexes[symbol] = histories[next(iter(ids))]

    macro_start = days[max(days.index(first) - MACRO_LOOKBACK_SESSIONS, 0)]
    macro_rows = conn.execute(
        select(MacroObservation.series, MacroObservation.observation_date, MacroObservation.value)
        .where(
            and_(
                MacroObservation.series.in_((VIX, VIX3M)),
                MacroObservation.observation_date.between(macro_start, last),
            )
        )
        .order_by(MacroObservation.observation_date)
    ).all()
    macro_points: dict[str, tuple[list[date], list[float]]] = {VIX: ([], []), VIX3M: ([], [])}
    for series, day, value in macro_rows:
        macro_points[series][0].append(day)
        macro_points[series][1].append(value)
    macro = {name: MacroSeries(d, v) for name, (d, v) in macro_points.items()}
    return LoadedData(
        sessions=sessions,
        session_minutes=minutes,
        indexes=indexes,
        macro=macro,
    )


def read_state(conn: Connection, session: date) -> RegimeState | None:
    data = conn.execute(
        select(MarketRegimeRow.state).where(MarketRegimeRow.session_date == session)
    ).scalar_one_or_none()
    return None if data is None else RegimeState.from_json(data)


def write_results(conn: Connection, results: Sequence[RegimeResult], risk_hash: str) -> None:
    """Upsert one row per result. The effective regime is the computed one until the
    Portfolio Manager adds its view (Phase 4)."""
    if not results:
        return
    rows = [
        {
            "session_date": r.session,
            "index_trend": r.pillars.index_trend,
            "breadth": r.pillars.breadth,
            "volatility": r.pillars.volatility,
            "distribution": r.pillars.distribution,
            "leadership": r.pillars.leadership,
            "score": r.score,
            "computed_regime": r.regime.value,
            "llm_regime": None,
            "effective_regime": r.regime.value,
            "crisis": r.crisis.active,
            "follow_through_day": r.follow_through is not None,
            "no_ftd": r.state.no_ftd,
            "sessions_in_state": r.state.sessions_in_state,
            "expiration_session": bool(r.features["expiration_session"]),
            "features": dict(r.features),
            "state": r.state.to_json(),
            "risk_hash": risk_hash,
        }
        for r in results
    ]
    for i in range(0, len(rows), WRITE_CHUNK):
        stmt = insert(MarketRegimeRow).values(rows[i : i + WRITE_CHUNK])
        keep = {"session_date", "llm_regime", "effective_regime"}
        updated: dict[str, Any] = {c: stmt.excluded[c] for c in rows[0] if c not in keep}
        # A recompute never erases the Portfolio Manager's view; once there is one, the
        # effective regime is the Phase 4 code's to maintain.
        updated["effective_regime"] = case(
            (MarketRegimeRow.llm_regime.is_(None), stmt.excluded.effective_regime),
            else_=MarketRegimeRow.effective_regime,
        )
        conn.execute(stmt.on_conflict_do_update(index_elements=["session_date"], set_=updated))
