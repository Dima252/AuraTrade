"""Reconciliation (README §5.7.1): the broker is the truth and the database follows it.

Run first in every order-touching job, under the lock:
1. Submitted entry signals: a fill opens a position (with plan version 1); an entry closed with
   nothing filled ends the signal.
2. Broker positions: quantities are mirrored; one the database doesn't know (not ours, or
   bought by hand) is a mismatch and an alert, and blocks entries in the symbol.
3. Database positions the broker no longer holds are closed, with a journal row: the exit
   price from the sell fills, the reason from the order that sold (a stop is `HARD_STOP`).
4. Splits on held symbols (#116): the plan is scaled by the ratio, the corporate action is
   marked handled, and a mismatch is opened because paper never applies the split to the
   position.
5. Every order seen is mirrored into `orders`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Engine, select, update
from sqlalchemy.dialects.postgresql import insert

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import EntryPolicy, RiskConfig, StopKind, Strategy, TriggerKind
from auratrade.db.models import EXIT_REASONS, CorporateAction, Position, TradeJournalRow
from auratrade.execution import ids, store
from auratrade.execution.broker import Broker, BrokerOrder, BrokerPosition, Side
from auratrade.execution.store import OpenPosition, SignalRecord
from auratrade.plans.model import EventKind, HoldState, PlanChange, PositionPlan
from auratrade.plans.versioning import scale_for_split

log = structlog.get_logger(__name__)

# Orders are looked up from a little before the earliest position or signal they could belong
# to, so a fill recorded with a slightly earlier broker timestamp isn't missed.
LOOKBACK_MARGIN = timedelta(days=1)
SPLIT_TYPES = ("SPLIT", "REVERSE_SPLIT")
SKELETON_ENTRY = EntryPolicy(trigger=TriggerKind.PIVOT, stop=StopKind.STRUCTURAL, tranches=1)


@dataclass
class Reconciled:
    positions: dict[str, OpenPosition]
    """Open positions after reconciliation, by symbol."""
    levels: dict[str, float]
    """Each open position's hard-stop level."""
    broker_positions: dict[str, BrokerPosition]
    unknown: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    splits: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    def details(self) -> dict[str, Any]:
        return {
            "positions": sorted(self.positions),
            "opened": self.opened,
            "closed": self.closed,
            "unknown": self.unknown,
            "splits": self.splits,
        }


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
    engine: Engine,
    broker: Broker,
    *,
    session: date,
    now: datetime,
    calendar: MarketCalendar,
    risk: RiskConfig,
) -> Reconciled:
    broker_positions = {p.symbol: p for p in broker.positions() if p.qty > 0}
    open_orders = broker.open_orders()
    with engine.begin() as conn:
        db_open = store.open_positions(conn)
        submitted = store.submitted_signals(conn)

    symbols = sorted(set(broker_positions) | set(db_open) | {s.symbol for s in submitted})
    starts = [p.opened_at for p in db_open.values()]
    for signal in submitted:
        signal_session = calendar.session_for(signal.session_date)
        if signal_session is not None:
            starts.append(signal_session.open_at)
    since = min(starts, default=now) - LOOKBACK_MARGIN
    recent = broker.orders_since(since, symbols) if symbols else []
    orders = _merge(open_orders, recent)
    by_client_id = {o.client_order_id: o for o in orders}

    result = Reconciled(positions={}, levels={}, broker_positions=broker_positions)
    closed_ids: dict[str, int] = {}
    with engine.begin() as conn:
        regime = store.effective_regime(conn, session)
        for signal in submitted:
            entry = by_client_id.get(signal.client_order_id or "")
            if entry is None and signal.client_order_id:
                entry = broker.order_by_client_id(signal.client_order_id)
            if entry is None:
                continue
            if entry.filled_qty > 0:
                if signal.symbol not in db_open:
                    db_open[signal.symbol] = _open(
                        conn, signal, entry, broker_positions, session, now, risk
                    )
                    result.opened.append(signal.symbol)
                store.update_signal(conn, signal.id, now, status="FILLED", shares=entry.filled_qty)
            elif not entry.is_open:
                store.update_signal(
                    conn,
                    signal.id,
                    now,
                    status="CANCELED",
                    status_reason=f"entry order {entry.status.value} unfilled",
                )

        for symbol, held in broker_positions.items():
            position = db_open.get(symbol)
            if position is None:
                result.unknown.append(symbol)
                if store.record_mismatch(
                    conn,
                    store.RECONCILIATION,
                    symbol,
                    {"broker_qty": held.qty, "why": "a position the database doesn't know"},
                ):
                    result.alerts.append(
                        f"unknown position at the broker: {held.qty} {symbol}. No new entries "
                        "in it until resolved (auratrade resolve)."
                    )
                continue
            if held.qty != position.shares:
                conn.execute(
                    update(Position)
                    .where(Position.id == position.id)
                    .values(
                        shares=held.qty,
                        max_shares=max(position.max_shares, held.qty),
                        updated_at=now,
                    )
                )
                db_open[symbol] = replace(position, shares=held.qty)

        for symbol, position in list(db_open.items()):
            if symbol in broker_positions:
                continue
            _close(conn, position, orders, session, now, calendar, regime)
            closed_ids[symbol] = position.id
            result.closed.append(symbol)
            del db_open[symbol]

        result.splits = _apply_splits(conn, db_open, broker_positions, session, now, risk, result)
        result.positions = db_open
        result.levels = store.stop_levels(conn, db_open)
        signal_ids = {s.client_order_id: s.id for s in submitted if s.client_order_id}
        position_ids = {**closed_ids, **{s: p.id for s, p in db_open.items()}}
        store.mirror_orders(conn, orders, position_ids=position_ids, signal_ids=signal_ids, now=now)
    for text in result.alerts:
        log.warning("reconcile_alert", text=text)
    return result


def _open(
    conn: Any,
    signal: SignalRecord,
    entry: BrokerOrder,
    broker_positions: Mapping[str, BrokerPosition],
    session: date,
    now: datetime,
    risk: RiskConfig,
) -> OpenPosition:
    held = broker_positions.get(signal.symbol)
    shares = held.qty if held is not None else entry.filled_qty
    price = entry.filled_avg_price or (held.avg_entry_price if held else signal.limit_price)
    regime = store.effective_regime(conn, signal.session_date)
    row_id = conn.execute(
        insert(Position)
        .values(
            ticker_id=signal.ticker_id,
            symbol=signal.symbol,
            source=signal.source,
            strategy=signal.strategy,
            signal_id=signal.id,
            shares=shares,
            max_shares=max(shares, entry.filled_qty),
            entry_price=price,
            initial_stop=signal.stop_price,
            opened_session=signal.session_date,
            regime_at_entry=regime,
            opened_at=entry.created_at or now,
            updated_at=now,
        )
        .returning(Position.id)
    ).scalar_one()
    position = OpenPosition(
        id=row_id,
        symbol=signal.symbol,
        ticker_id=signal.ticker_id,
        source=signal.source,
        strategy=signal.strategy,
        signal_id=signal.id,
        shares=shares,
        max_shares=max(shares, entry.filled_qty),
        entry_price=price,
        initial_stop=signal.stop_price,
        opened_session=signal.session_date,
        opened_at=entry.created_at or now,
        regime_at_entry=regime,
        exit_pending=None,
    )
    if signal.stop_price < price:
        plan = PositionPlan(
            version=1,
            change=PlanChange.ENTRY,
            strategy=Strategy(signal.strategy),
            entry=SKELETON_ENTRY,
            exits=store.skeleton_exit_plan(risk.skeleton.trail),
            pivot=signal.pivot,
            entry_price=price,
            initial_stop=signal.stop_price,
            hard_stop=signal.stop_price,
            hold=HoldState.OFF,
        )
        store.write_plan(
            conn,
            position,
            plan,
            session=session,
            config=risk.plans,
            risk_hash=risk.config_hash,
        )
    # Otherwise the fill came at or below the stop: no plan can be written, the level falls
    # back to the initial stop, and the invariant sells at once (the price is through it).
    log.info("position_opened", symbol=signal.symbol, shares=shares, price=price)
    return position


def _exit_reason(order: BrokerOrder) -> str:
    if order.is_sell_stop:
        return "HARD_STOP"
    parsed = ids.parse(order.client_order_id)
    if parsed is None or parsed.purpose is not ids.Purpose.EXIT:
        return "MANUAL"
    reason = "HARD_STOP" if parsed.detail == "STOP" else parsed.detail
    return reason if reason in EXIT_REASONS else "MANUAL"


def _close(
    conn: Any,
    position: OpenPosition,
    orders: Iterable[BrokerOrder],
    session: date,
    now: datetime,
    calendar: MarketCalendar,
    regime: str | None,
) -> None:
    sells = sorted(
        (
            o
            for o in orders
            if o.symbol == position.symbol
            and o.side is Side.SELL
            and o.filled_qty > 0
            and o.created_at is not None
            and o.created_at >= position.opened_at - LOOKBACK_MARGIN
        ),
        key=lambda o: o.filled_at or o.created_at or now,
    )
    sold = sum(o.filled_qty for o in sells)
    proceeds = sum(o.filled_qty * (o.filled_avg_price or 0.0) for o in sells)
    exit_price = proceeds / sold if sold else None
    reason = _exit_reason(sells[-1]) if sells else "MANUAL"
    pnl = proceeds - position.entry_price * sold if sold else None
    risk = (position.entry_price - position.initial_stop) * position.max_shares
    r_multiple = pnl / risk if pnl is not None and risk > 0 else None
    holding = len(calendar.sessions_between(position.opened_session, session))
    conn.execute(
        update(Position)
        .where(Position.id == position.id)
        .values(
            status="CLOSED",
            shares=0,
            closed_session=session,
            closed_at=now,
            exit_reason=reason,
            exit_pending=None,
            exit_pending_since=None,
            updated_at=now,
        )
    )
    conn.execute(
        insert(TradeJournalRow).values(
            position_id=position.id,
            ticker_id=position.ticker_id,
            symbol=position.symbol,
            source=position.source,
            strategy=position.strategy,
            entry_session=position.opened_session,
            exit_session=session,
            holding_sessions=max(holding, 1),
            shares=position.max_shares,
            entry_price=position.entry_price,
            exit_price=exit_price,
            initial_stop=position.initial_stop,
            pnl=pnl,
            r_multiple=r_multiple,
            exit_reason=reason,
            regime_at_entry=position.regime_at_entry,
            regime_at_exit=regime,
            details={"sell_orders": [o.id for o in sells], "shares_sold": sold},
        )
    )
    store.write_event(
        conn,
        position,
        session=session,
        kind=EventKind.EXIT.value,
        rule=reason if reason != "MANUAL" else None,
        shares=sold,
        details={"exit_price": exit_price, "r_multiple": r_multiple},
    )
    log.info("position_closed", symbol=position.symbol, reason=reason, r=r_multiple)


def _apply_splits(
    conn: Any,
    positions: Mapping[str, OpenPosition],
    broker_positions: Mapping[str, BrokerPosition],
    session: date,
    now: datetime,
    risk: RiskConfig,
    result: Reconciled,
) -> list[str]:
    if not positions:
        return []
    actions = store.entities(
        conn,
        select(CorporateAction).where(
            CorporateAction.symbol.in_(list(positions)),
            CorporateAction.action_type.in_(SPLIT_TYPES),
            CorporateAction.ex_date <= session,
            CorporateAction.handled.is_(False),
        ),
    )
    applied: list[str] = []
    for action in actions:
        position = positions[action.symbol]
        if action.ex_date <= position.opened_session or not action.ratio:
            continue
        plans = store.latest_plans(conn, [position.id])
        if position.id in plans:
            scaled = scale_for_split(plans[position.id], action.ratio)
            store.write_plan(
                conn,
                position,
                scaled,
                session=session,
                config=risk.plans,
                risk_hash=risk.config_hash,
            )
        conn.execute(
            update(CorporateAction)
            .where(CorporateAction.id == action.id)
            .values(handled=True, handled_at=now)
        )
        held = broker_positions.get(action.symbol)
        broker_qty = held.qty if held is not None else 0
        details = {
            "ratio": action.ratio,
            "ex_date": action.ex_date.isoformat(),
            "broker_qty": broker_qty,
            "expected_qty": int(position.shares * action.ratio),
        }
        store.record_mismatch(conn, store.CORPORATE_ACTION, action.symbol, details)
        result.alerts.append(
            f"split {action.ratio:g}:1 in {action.symbol} (ex {action.ex_date}): plan levels "
            f"scaled; the broker shows {broker_qty} shares, {details['expected_qty']} expected. "
            "Paper doesn't apply splits (#116): check and resolve by hand."
        )
        applied.append(action.symbol)
    return applied
