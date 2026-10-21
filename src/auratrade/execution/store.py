"""Database reads and writes for the order layer: signals, positions, plans, orders, mismatches.

Each function takes a connection; the caller owns the transaction. Order sections keep their
transactions short (one per step), because Neon ends a transaction idle for 5 minutes and an
order section waits on fills.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, Select, func, orm, select, update
from sqlalchemy.dialects.postgresql import insert

from auratrade.config import ExitPlan, PlansConfig, TrailMA
from auratrade.db.models import (
    MarketRegimeRow,
    Mismatch,
    OrderRow,
    Position,
    PositionEventRow,
    PositionPlanRow,
    Signal,
    Ticker,
)
from auratrade.execution import ids
from auratrade.execution.broker import BrokerOrder, Side
from auratrade.plans.levels import plan_levels
from auratrade.plans.model import PositionPlan

RECONCILIATION = "RECONCILIATION"
CORPORATE_ACTION = "CORPORATE_ACTION"


@dataclass(frozen=True, slots=True)
class OpenPosition:
    id: int
    symbol: str
    ticker_id: int | None
    source: str
    strategy: str
    signal_id: int | None
    shares: int
    max_shares: int
    entry_price: float
    initial_stop: float
    opened_session: date
    opened_at: datetime
    regime_at_entry: str | None
    exit_pending: str | None


@dataclass(frozen=True, slots=True)
class SignalRecord:
    id: int
    session_date: date
    decided_session: date
    ticker_id: int
    symbol: str
    source: str
    strategy: str
    tranche: int
    rank: int
    pivot: float
    trigger_price: float
    limit_price: float
    stop_price: float
    risk_fraction: float
    shares: int | None
    client_order_id: str | None
    status: str
    details: dict[str, Any]


def entities[T](conn: Connection, query: Select[T]) -> list[T]:
    """ORM objects for `query` on a plain connection (a Core `execute` would return rows)."""
    return list(orm.Session(conn).scalars(query).all())


def ticker_ids(conn: Connection, symbols: Iterable[str]) -> dict[str, int]:
    wanted = sorted(set(symbols))
    if not wanted:
        return {}
    query = select(Ticker.symbol, Ticker.id).where(Ticker.symbol.in_(wanted), Ticker.active)
    return dict(conn.execute(query).all())


def _position(row: Position) -> OpenPosition:
    return OpenPosition(
        id=row.id,
        symbol=row.symbol,
        ticker_id=row.ticker_id,
        source=row.source,
        strategy=row.strategy,
        signal_id=row.signal_id,
        shares=row.shares,
        max_shares=row.max_shares,
        entry_price=row.entry_price,
        initial_stop=row.initial_stop,
        opened_session=row.opened_session,
        opened_at=row.opened_at,
        regime_at_entry=row.regime_at_entry,
        exit_pending=row.exit_pending,
    )


def open_positions(conn: Connection) -> dict[str, OpenPosition]:
    rows = entities(conn, select(Position).where(Position.status == "OPEN"))
    return {row.symbol: _position(row) for row in rows}


def latest_plans(conn: Connection, position_ids: Iterable[int]) -> dict[int, PositionPlan]:
    wanted = list(position_ids)
    if not wanted:
        return {}
    latest = (
        select(PositionPlanRow.position_id, func.max(PositionPlanRow.version).label("version"))
        .where(PositionPlanRow.position_id.in_(wanted))
        .group_by(PositionPlanRow.position_id)
        .subquery()
    )
    rows = conn.execute(
        select(PositionPlanRow.position_id, PositionPlanRow.plan).join(
            latest,
            (latest.c.position_id == PositionPlanRow.position_id)
            & (latest.c.version == PositionPlanRow.version),
        )
    ).all()
    return {pid: PositionPlan.model_validate(plan) for pid, plan in rows}


def stop_levels(conn: Connection, positions: Mapping[str, OpenPosition]) -> dict[str, float]:
    """Each open position's hard-stop level: its latest plan's, else its initial stop."""
    plans = latest_plans(conn, (p.id for p in positions.values()))
    return {
        symbol: plans[p.id].hard_stop if p.id in plans else p.initial_stop
        for symbol, p in positions.items()
    }


def write_plan(
    conn: Connection,
    position: OpenPosition,
    plan: PositionPlan,
    *,
    session: date,
    config: PlansConfig,
    risk_hash: str,
) -> None:
    if position.ticker_id is None:
        return
    levels = plan_levels(plan, dict.fromkeys(TrailMA), config)
    conn.execute(
        insert(PositionPlanRow).values(
            position_id=position.id,
            version=plan.version,
            ticker_id=position.ticker_id,
            strategy=plan.strategy.value,
            change=plan.change.value,
            session_date=session,
            pivot=plan.pivot,
            entry_price=plan.entry_price,
            initial_stop=plan.initial_stop,
            hard_stop=plan.hard_stop,
            trail=plan.trail.value,
            hold_state=plan.hold.value,
            hold_until_session=plan.hold_until_session,
            plan=plan.model_dump(mode="json"),
            levels=asdict(levels),
            risk_hash=risk_hash,
        )
    )


def write_event(
    conn: Connection,
    position: OpenPosition,
    *,
    session: date,
    kind: str,
    rule: str | None = None,
    shares: int | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    if position.ticker_id is None:
        return
    conn.execute(
        insert(PositionEventRow).values(
            position_id=position.id,
            ticker_id=position.ticker_id,
            session_date=session,
            kind=kind,
            rule=rule,
            shares=shares if shares else None,
            details=dict(details or {}),
        )
    )


def set_exit_pending(conn: Connection, position_id: int, reason: str | None, now: datetime) -> None:
    conn.execute(
        update(Position)
        .where(Position.id == position_id)
        .values(
            exit_pending=reason,
            exit_pending_since=None if reason is None else now,
            updated_at=now,
        )
    )


# --- signals ---------------------------------------------------------------------------------


def _signal(row: Signal) -> SignalRecord:
    return SignalRecord(
        id=row.id,
        session_date=row.session_date,
        decided_session=row.decided_session,
        ticker_id=row.ticker_id,
        symbol=row.symbol,
        source=row.source,
        strategy=row.strategy,
        tranche=row.tranche,
        rank=row.rank,
        pivot=row.pivot,
        trigger_price=row.trigger_price,
        limit_price=row.limit_price,
        stop_price=row.stop_price,
        risk_fraction=row.risk_fraction,
        shares=row.shares,
        client_order_id=row.client_order_id,
        status=row.status,
        details=dict(row.details),
    )


def signals_for(
    conn: Connection, session: date, statuses: Iterable[str] | None = None
) -> list[SignalRecord]:
    query = select(Signal).where(Signal.session_date == session)
    if statuses is not None:
        query = query.where(Signal.status.in_(list(statuses)))
    return [_signal(r) for r in entities(conn, query.order_by(Signal.rank))]


def signal_by_client_id(conn: Connection, client_order_id: str) -> SignalRecord | None:
    rows = entities(conn, select(Signal).where(Signal.client_order_id == client_order_id))
    return _signal(rows[0]) if rows else None


def update_signal(conn: Connection, signal_id: int, now: datetime, **values: Any) -> None:
    conn.execute(update(Signal).where(Signal.id == signal_id).values(updated_at=now, **values))


def submitted_signals(conn: Connection) -> list[SignalRecord]:
    """Signals whose entry may be at the broker: submitted, or given their client_order_id
    just before a submission that may have got through before the job died."""
    rows = entities(
        conn,
        select(Signal).where(
            Signal.client_order_id.is_not(None), Signal.status.in_(("PENDING", "SUBMITTED"))
        ),
    )
    return [_signal(r) for r in rows]


# --- mismatches and the regime --------------------------------------------------------------


def open_mismatches(conn: Connection) -> list[tuple[str, str | None]]:
    """(kind, symbol) of every unresolved mismatch; a NULL symbol blocks all entries."""
    rows = conn.execute(
        select(Mismatch.kind, Mismatch.symbol).where(Mismatch.resolved_at.is_(None))
    ).all()
    return [(kind, symbol) for kind, symbol in rows]


def record_mismatch(
    conn: Connection, kind: str, symbol: str | None, details: Mapping[str, Any]
) -> bool:
    """Open a mismatch unless one of the same kind is already open for the symbol."""
    existing = conn.execute(
        select(Mismatch.id).where(
            Mismatch.kind == kind,
            Mismatch.symbol.is_(None) if symbol is None else Mismatch.symbol == symbol,
            Mismatch.resolved_at.is_(None),
        )
    ).first()
    if existing is not None:
        return False
    conn.execute(insert(Mismatch).values(kind=kind, symbol=symbol, details=dict(details)))
    return True


def effective_regime(conn: Connection, session: date) -> str | None:
    """The regime that governs `session`: the label written at the previous close."""
    row = conn.execute(
        select(MarketRegimeRow.effective_regime)
        .where(MarketRegimeRow.session_date < session)
        .order_by(MarketRegimeRow.session_date.desc())
        .limit(1)
    ).first()
    return None if row is None else str(row[0])


# --- the order mirror -----------------------------------------------------------------------


def order_purpose(order: BrokerOrder) -> str:
    if order.is_leg:
        return "STOP_LEG"
    parsed = ids.parse(order.client_order_id)
    if parsed is not None:
        return {
            ids.Purpose.ENTRY: "ENTRY",
            ids.Purpose.STOP: "STOP",
            ids.Purpose.EXIT: "EXIT",
        }[parsed.purpose]
    if order.is_sell_stop:
        return "STOP"  # a replaced standalone stop gets a broker-assigned id
    return "ENTRY" if order.side is Side.BUY else "OTHER"


def mirror_orders(
    conn: Connection,
    orders: Iterable[BrokerOrder],
    *,
    position_ids: Mapping[str, int],
    signal_ids: Mapping[str, int],
    now: datetime,
) -> int:
    rows = [
        {
            "broker_id": o.id,
            "client_order_id": o.client_order_id,
            "parent_broker_id": o.parent_id,
            "replaces": o.replaces,
            "position_id": position_ids.get(o.symbol),
            "signal_id": signal_ids.get(o.client_order_id),
            "symbol": o.symbol,
            "purpose": order_purpose(o),
            "side": o.side.value,
            "order_type": o.type.value,
            "order_class": o.order_class.value,
            "time_in_force": o.time_in_force.value,
            "qty": o.qty,
            "filled_qty": o.filled_qty,
            "filled_avg_price": o.filled_avg_price,
            "limit_price": o.limit_price,
            "stop_price": o.stop_price,
            "status": o.status.value,
            "created_at": o.created_at,
            "filled_at": o.filled_at,
            "synced_at": now,
        }
        for o in {o.id: o for o in orders}.values()
    ]
    if not rows:
        return 0
    statement = insert(OrderRow).values(rows)
    keep_links = {
        # A link found once (a position, a signal) is never lost on a later sync.
        "position_id": func.coalesce(OrderRow.position_id, statement.excluded.position_id),
        "signal_id": func.coalesce(OrderRow.signal_id, statement.excluded.signal_id),
        "parent_broker_id": func.coalesce(
            statement.excluded.parent_broker_id, OrderRow.parent_broker_id
        ),
    }
    changing = {
        name: statement.excluded[name]
        for name in (
            "qty",
            "filled_qty",
            "filled_avg_price",
            "limit_price",
            "stop_price",
            "status",
            "filled_at",
            "synced_at",
        )
    }
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[OrderRow.broker_id], set_={**changing, **keep_links}
        )
    )
    return len(rows)


def skeleton_exit_plan(trail: TrailMA) -> ExitPlan:
    """The skeleton's plan: the hard stop and one soft rule, the trail (task 1.7)."""
    return ExitPlan(trail=trail, earnings_cushion=False, regime_tightening=False)
