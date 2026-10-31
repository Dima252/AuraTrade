"""Shared setup for the portfolio's order tests : fund prices, decided targets, a
broker that fills marketable orders at once, and the checks every scenario ends with."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import insert, select

from auratrade.db.models import FundStop, PortfolioTarget
from auratrade.execution import funds
from auratrade.execution.broker import BrokerOrder, OrderStatus, OrderType, Side
from auratrade.execution.invariant import check
from auratrade.execution.store import entities
from auratrade.sim.fake_broker import FakeBroker
from tests.execution.conftest import TODAY, World, ny

UP = {"SPY": 0.7, "SSO": 0.3}
PRICES = {"SPY": 500.0, "SSO": 100.0, "BIL": 91.5, "QQQ": 400.0}


def at_close(world: World, day: date = TODAY) -> None:
    """15:40 on `day`, the market open."""
    world.clock.now = ny(day, 15, 40)
    world.fake.now = world.clock.now
    world.fake.open_market()
    for symbol, price in PRICES.items():
        if world.fake.prices([symbol])[symbol] is None:
            world.fake.set_price(symbol, price)


def decided(
    world: World,
    targets: Mapping[str, float] = UP,
    *,
    execution: date = TODAY,
    status: str = "DECIDED",
) -> int:
    """A rebalance's row as `portfolio_manager` leaves it."""
    with world.engine.begin() as conn:
        return int(
            conn.execute(
                insert(PortfolioTarget)
                .values(
                    kind="REBALANCE",
                    ranking_session=execution,
                    execution_session=execution,
                    status=status,
                    trend="UP",
                    capped=False,
                    halted=False,
                    rule_level=1.3,
                    twin_targets=dict(UP),
                    facts={},
                    targets=None if status != "DECIDED" else dict(targets),
                    risk_hash=world.risk.config_hash,
                )
                .returning(PortfolioTarget.id)
            ).scalar_one()
        )


def target_row(world: World, row_id: int) -> PortfolioTarget:
    with world.engine.connect() as conn:
        [row] = entities(conn, select(PortfolioTarget).where(PortfolioTarget.id == row_id))
    return row


def stop_levels(world: World) -> list[tuple[str, float]]:
    with world.engine.connect() as conn:
        rows = conn.execute(select(FundStop.change, FundStop.level).order_by(FundStop.id)).all()
    return [(change, level) for change, level in rows]


@dataclass
class Market:
    """Marketable orders fill at once at the fund's price: a buy limit at or above it, a sell
    limit at or below it, a market sale always. `partial` fills only that many shares of a
    symbol's buys; `off` leaves a symbol's orders unfilled."""

    fake: FakeBroker
    partial: dict[str, int] = field(default_factory=dict)
    off: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.fake.on_submit.append(self._fill)

    def _fill(self, order: BrokerOrder) -> None:
        price = self.fake.prices([order.symbol])[order.symbol]
        if order.symbol in self.off or price is None or order.status is not OrderStatus.NEW:
            return
        if order.type is OrderType.MARKET:
            self.fake.fill(order.id, price=price)
        elif order.type is OrderType.LIMIT and order.limit_price is not None:
            buy = order.side is Side.BUY
            if (buy and price <= order.limit_price) or (not buy and price >= order.limit_price):
                qty = self.partial.get(order.symbol) if buy else None
                self.fake.fill(order.id, qty=qty, price=price)


def protected(world: World, symbols: Iterable[str] = ("SSO",)) -> bool:
    """The leveraged fund's live GTC stops cover it exactly at or above its level, nothing is
    partly filled or working, and no other fund carries a stop. Positions outside the funds
    aren't the portfolio's."""
    fake = world.fake
    with world.engine.connect() as conn:
        found = {s: funds.stop_level(conn, s) for s in symbols}
    levels = {s: f.level for s, f in found.items() if f is not None}
    unstopped = funds.unstopped(world.risk.portfolio)
    names = funds.funds(world.risk.portfolio)
    positions = [p for p in fake.positions() if p.symbol in names]
    orders = [o for o in fake.open_orders() if o.symbol in names]
    result = check(positions, orders, levels, unstopped=unstopped)
    ok = all(p.ok for p in result.values())
    stops_elsewhere = [o for o in fake.open_orders() if o.is_sell_stop and o.symbol in unstopped]
    return (
        ok
        and not stops_elsewhere
        and set(levels) >= {p.symbol for p in positions if p.symbol not in unstopped}
    )


def no_duplicates(fake: FakeBroker) -> bool:
    """Nothing left working but stops, and no symbol with stops for more than it holds."""
    working = [o for o in fake.open_orders() if not o.is_sell_stop]
    over = [
        p
        for p in check(fake.positions(), fake.open_orders()).values()
        if "OVERCOVERED" in {x.value for x in p.problems}
    ]
    return not working and not over


def stops(fake: FakeBroker, symbol: str = "SSO") -> list[BrokerOrder]:
    return [o for o in fake.open_orders() if o.symbol == symbol and o.is_sell_stop]
