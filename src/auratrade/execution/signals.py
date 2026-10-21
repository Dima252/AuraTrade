"""The skeleton's evening scan (task 1.7): the universe on session T → ranked entry signals
for the next session, written to the `signals` queue.

Point in time like every scan: the universe filters on raw prices as of T, a ticker without a
bar on T is left out (never forward-filled), and the RS rating is the percentile of the RS
score among T's universe members (README §5.3).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, select, update
from sqlalchemy.dialects.postgresql import insert

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig, Strategy
from auratrade.db.models import DailyBar, Signal, Ticker
from auratrade.execution.skeleton import Levels, entry_levels, passes_scan
from auratrade.indicators.bars import Bar
from auratrade.indicators.relative_strength import percentile_ranks
from auratrade.indicators.universe import universe_flags
from auratrade.strategies.trend_template import rs_scores

SOURCE = "SKELETON"
# The skeleton's entries are pivot breakouts with a structural stop, one tranche: the closest
# strategy label. `source` keeps them apart from every real strategy's trades.
STRATEGY = Strategy.VCP_BREAKOUT


@dataclass(frozen=True, slots=True)
class History:
    ticker_id: int
    symbol: str
    dates: list[date]
    bars: list[Bar]
    raw_closes: list[float]


@dataclass(frozen=True, slots=True)
class Candidate:
    ticker_id: int
    symbol: str
    rs_rating: float
    close: float
    levels: Levels


def lookback_sessions(risk: RiskConfig) -> int:
    """Bars needed for every rule on the last session: the 200 SMA rising for 22 sessions, the
    52-week range, the longest RS lookback, the universe's history and dollar-volume windows."""
    tt, u = risk.trend_template, risk.universe
    return (
        max(
            tt.long_sma + tt.long_sma_rising_sessions,
            tt.range_sessions,
            max(w.lookback for w in tt.rs_weights) + 1,
            u.min_history_sessions,
            u.dollar_volume_sessions,
        )
        + 1
    )


def load_histories(
    conn: Connection, calendar: MarketCalendar, session: date, risk: RiskConfig
) -> list[History]:
    start = calendar.n_sessions_back(session, lookback_sessions(risk)).day
    u = risk.universe
    rows = conn.execute(
        select(
            DailyBar.ticker_id,
            Ticker.symbol,
            DailyBar.session_date,
            DailyBar.open,
            DailyBar.high,
            DailyBar.low,
            DailyBar.close,
            DailyBar.volume,
            DailyBar.raw_close,
        )
        .join(Ticker, Ticker.id == DailyBar.ticker_id)
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
    out = []
    for ticker_id, bars in grouped.items():
        if bars[-1].session_date != session:
            continue  # no bar on T: halted, suspended or delisted
        out.append(
            History(
                ticker_id=ticker_id,
                symbol=bars[-1].symbol,
                dates=[b.session_date for b in bars],
                bars=[Bar(b.open, b.high, b.low, b.close, float(b.volume)) for b in bars],
                raw_closes=[b.raw_close for b in bars],
            )
        )
    return out


def scan(histories: Iterable[History], risk: RiskConfig) -> list[Candidate]:
    """Every universe member passing the skeleton's rules, best RS rating first."""
    u, tt = risk.universe, risk.trend_template
    members: dict[int, History] = {}
    scores: dict[int, float] = {}
    for h in histories:
        closes = [b.close for b in h.bars]
        flags = universe_flags(
            closes,
            h.raw_closes,
            [b.volume for b in h.bars],
            min_price=u.min_price,
            min_dollar_volume=u.min_dollar_volume,
            dollar_volume_sessions=u.dollar_volume_sessions,
            min_history_sessions=u.min_history_sessions,
        )
        if not flags or not flags[-1]:
            continue
        score = rs_scores(closes, tt)[-1]
        if score is not None:
            members[h.ticker_id] = h
            scores[h.ticker_id] = score
    ratings = percentile_ranks(scores) if scores else {}
    out = []
    for ticker_id, h in members.items():
        rating = ratings[ticker_id]
        if not passes_scan(h.bars, rating, risk.skeleton, tt):
            continue
        levels = entry_levels(
            h.bars, risk.skeleton, max_chase=risk.guardrails.max_chase_above_pivot
        )
        if levels is None:
            continue
        out.append(Candidate(ticker_id, h.symbol, rating, h.bars[-1].close, levels))
    out.sort(key=lambda c: (-c.rs_rating, c.symbol))
    return out


def idempotency_key(session: date, symbol: str, tranche: int = 1) -> str:
    return f"{session.isoformat()}:{symbol}:ENTRY:{tranche}"


def write_signals(
    conn: Connection,
    candidates: Iterable[Candidate],
    *,
    trade_session: date,
    decided_session: date,
    risk_fraction: float,
    now: datetime,
) -> int:
    """Insert the candidates as PENDING signals, ranked from 1. A re-run for the same session
    writes nothing new (the idempotency key)."""
    rows = [
        {
            "idempotency_key": idempotency_key(trade_session, c.symbol),
            "session_date": trade_session,
            "decided_session": decided_session,
            "ticker_id": c.ticker_id,
            "symbol": c.symbol,
            "source": SOURCE,
            "strategy": STRATEGY.value,
            "rank": rank,
            "pivot": c.levels.pivot,
            "trigger_price": c.levels.trigger,
            "limit_price": c.levels.limit,
            "stop_price": c.levels.stop,
            "risk_fraction": risk_fraction,
            "details": {"rs_rating": round(c.rs_rating, 2), "adr": c.levels.adr, "close": c.close},
            "created_at": now,
            "updated_at": now,
        }
        for rank, c in enumerate(candidates, start=1)
    ]
    if not rows:
        return 0
    written = conn.execute(
        insert(Signal)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[Signal.idempotency_key])
        .returning(Signal.id)
    ).all()
    return len(written)


def expire_signals(conn: Connection, *, before_or_on: date, now: datetime) -> int:
    """PENDING signals for sessions that are over will never be placed."""
    result = conn.execute(
        update(Signal)
        .where(Signal.status == "PENDING", Signal.session_date <= before_or_on)
        .values(status="EXPIRED", status_reason="session over", updated_at=now)
    )
    return int(result.rowcount or 0)
