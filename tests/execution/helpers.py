"""Shared setup for the order-layer tests: a fake broker with filled entries, and a desk."""

from __future__ import annotations

from datetime import date

from auratrade.config import ExecutionConfig, load_risk_config
from auratrade.execution import ids
from auratrade.execution.broker import BrokerOrder, OtoBuy, StopSell
from auratrade.execution.invariant import check
from auratrade.execution.orders import OrderDesk
from auratrade.sim.fake_broker import FakeBroker

SESSION = date(2026, 10, 22)
CFG: ExecutionConfig = load_risk_config().execution


def entry(
    fake: FakeBroker,
    symbol: str = "NVDA",
    qty: int = 10,
    *,
    limit: float = 101.0,
    stop: float = 95.0,
    fill: bool = True,
    price: float | None = None,
    tranche: int = 1,
) -> BrokerOrder:
    """An OTO entry (GTC parent and leg), filled in full at the limit unless `fill` is False."""
    if fake.prices([symbol])[symbol] is None:
        fake.set_price(symbol, price or limit)
    parent = fake.submit(
        OtoBuy(
            symbol,
            qty,
            limit_price=limit,
            stop_loss=stop,
            client_order_id=ids.entry_id(SESSION, symbol, tranche=tranche),
            trigger=limit - 1,
        )
    )
    if fill:
        fake.fill(parent.id, price=limit)
    return fake.get_order(parent.id)


def leg_of(fake: FakeBroker, parent: BrokerOrder) -> BrokerOrder:
    return next(o for o in fake.all_orders() if o.parent_id == parent.id and o.is_open)


def standalone_stop(fake: FakeBroker, symbol: str, qty: int, stop: float) -> BrokerOrder:
    return fake.submit(StopSell(symbol, qty, stop, ids.stop_id(SESSION, symbol)))


def desk(
    fake: FakeBroker,
    *,
    alerts: list[str] | None = None,
    market_open: bool | None = None,
    cfg: ExecutionConfig = CFG,
) -> OrderDesk:
    sink = alerts if alerts is not None else []
    return OrderDesk(
        broker=fake,
        prices=fake,
        cfg=cfg,
        session=SESSION,
        market_open=fake.market_open if market_open is None else market_open,
        sleep=fake.sleep,
        alert=sink.append,
    )


def protected(fake: FakeBroker, levels: dict[str, float] | None = None) -> bool:
    """Every position's live GTC stops cover it exactly (no partial entry, no stray sale)."""
    return all(p.ok for p in check(fake.positions(), fake.open_orders(), levels).values())


def open_entries(fake: FakeBroker, symbol: str) -> list[BrokerOrder]:
    return [o for o in fake.open_orders() if o.symbol == symbol and o.side.value == "buy"]
