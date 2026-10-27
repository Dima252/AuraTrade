"""Database reads and writes for the order layer: orders and mismatches.

Each function takes a connection; the caller owns the transaction. Order sections keep their
transactions short (one per step), because Neon ends a transaction idle for 5 minutes and an
order section waits on fills. (The setup system's signals, positions and plans went in task
5.2, #161; their tables are kept, no longer written.)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Select, func, orm, select
from sqlalchemy.dialects.postgresql import insert

from auratrade.db.models import Mismatch, OrderRow
from auratrade.execution import ids
from auratrade.execution.broker import BrokerOrder, Side

RECONCILIATION = "RECONCILIATION"
CORPORATE_ACTION = "CORPORATE_ACTION"


def entities[T](conn: Connection, query: Select[T]) -> list[T]:
    """ORM objects for `query` on a plain connection (a Core `execute` would return rows)."""
    return list(orm.Session(conn).scalars(query).all())


# --- mismatches ------------------------------------------------------------------------------


def open_mismatches(conn: Connection) -> list[tuple[str, str | None]]:
    """(kind, symbol) of every unresolved mismatch; a NULL symbol blocks every increase."""
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


# --- the order mirror -----------------------------------------------------------------------


def order_purpose(order: BrokerOrder) -> str:
    if order.is_leg:
        return "STOP_LEG"
    parsed = ids.parse(order.client_order_id)
    if parsed is not None:
        return {
            ids.Purpose.ENTRY: "ENTRY",
            ids.Purpose.BUY: "ENTRY",
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
    now: datetime,
) -> int:
    rows = [
        {
            "broker_id": o.id,
            "client_order_id": o.client_order_id,
            "parent_broker_id": o.parent_id,
            "replaces": o.replaces,
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
        # A parent found once is never lost on a later sync (a leg listed without it).
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
