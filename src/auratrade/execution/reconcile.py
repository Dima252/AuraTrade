"""Reconciliation (README §5.6.1): the broker is the truth and the database follows it.

Run first in every order-touching job, under the lock:
1. Broker positions: the portfolio's funds (`funds`) are its holdings; any other position (not
   ours, or bought by hand) is a mismatch and an alert, and blocks increases of exposure until
   it is resolved.
2. Every order seen in the held symbols (open, and created in the last `LOOKBACK_MARGIN`) is
   mirrored into `orders`.

The leveraged fund's stop level and the funds' splits are `execution.funds`'.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Engine

from auratrade.execution import store
from auratrade.execution.broker import Broker, BrokerOrder, BrokerPosition

log = structlog.get_logger(__name__)

# The orders mirrored each run: every open one, and those created since a day before now (a
# fill or a cancel since the last run).
LOOKBACK_MARGIN = timedelta(days=1)


@dataclass
class Reconciled:
    levels: dict[str, float]
    """The stop level of each stopped position (the leveraged fund's, `execution.funds`)."""
    broker_positions: dict[str, BrokerPosition]
    unknown: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    def details(self) -> dict[str, Any]:
        return {"positions": sorted(self.broker_positions), "unknown": self.unknown}


def _merge(*groups: Iterable[BrokerOrder]) -> list[BrokerOrder]:
    """One order per id, the latest listing winning, but a leg keeps the parent id a nested
    listing gave it."""
    merged: dict[str, BrokerOrder] = {}
    for group in groups:
        for order in group:
            known = merged.get(order.id)
            if known is not None and known.parent_id is not None and order.parent_id is None:
                order = replace(order, parent_id=known.parent_id)
            merged[order.id] = order
    return list(merged.values())


def reconcile(
    engine: Engine, broker: Broker, *, now: datetime, funds: Collection[str]
) -> Reconciled:
    broker_positions = {p.symbol: p for p in broker.positions() if p.qty > 0}
    open_orders = broker.open_orders()
    symbols = sorted(set(broker_positions) | {o.symbol for o in open_orders})
    recent = broker.orders_since(now - LOOKBACK_MARGIN, symbols) if symbols else []
    orders = _merge(open_orders, recent)

    result = Reconciled(levels={}, broker_positions=broker_positions)
    with engine.begin() as conn:
        for symbol, held in broker_positions.items():
            if symbol in funds:
                continue
            result.unknown.append(symbol)
            if store.record_mismatch(
                conn,
                store.RECONCILIATION,
                symbol,
                {"broker_qty": held.qty, "why": "a position outside the portfolio's funds"},
            ):
                result.alerts.append(
                    f"unknown position at the broker: {held.qty} {symbol}. No increase of "
                    "exposure until resolved (auratrade resolve)."
                )
        store.mirror_orders(conn, orders, now=now)
    for text in result.alerts:
        log.warning("reconcile_alert", text=text)
    return result
