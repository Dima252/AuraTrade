"""The fake broker's orders filled by the fill model, one bar at a time (README §5.12)."""

from auratrade.execution.broker import (
    LimitSell,
    MarketSell,
    OrderStatus,
    OtoBuy,
    StopSell,
)
from auratrade.indicators.bars import Bar
from auratrade.sim.fake_broker import FakeBroker

SLIP = 0.002


def b(o: float, h: float, low: float, c: float) -> Bar:
    return Bar(o, h, low, c, 1000)


def breakout(fake: FakeBroker, *, trigger: float | None = 50.0) -> str:
    fake.set_price("ABC", 49.8)
    parent = fake.submit(OtoBuy("ABC", 10, 51.0, 47.0, "e-1", trigger=trigger))
    return parent.id


def leg(fake: FakeBroker, parent_id: str) -> str:
    return next(o.id for o in fake.all_orders() if o.parent_id == parent_id)


def test_a_buy_stop_limit_waits_for_its_trigger_then_fills_with_slippage() -> None:
    fake = FakeBroker()
    parent = breakout(fake)
    assert fake.play("ABC", b(49.8, 49.9, 49.5, 49.7), slip=SLIP) == []
    assert fake.play("ABC", b(49.8, 50.4, 49.7, 50.3), slip=SLIP) == [parent]
    order = fake.get_order(parent)
    assert (order.status, order.filled_avg_price) == (OrderStatus.FILLED, 50.10)
    assert fake.get_order(leg(fake, parent)).status is OrderStatus.NEW
    assert fake.position("ABC") == 10
    assert fake.prices(["ABC"]) == {"ABC": 50.3}


def test_a_held_leg_doesnt_stop_out_before_its_parent_fills() -> None:
    fake = FakeBroker()
    parent = breakout(fake)
    fake.play("ABC", b(48, 48, 46, 46.5), slip=SLIP)
    assert fake.get_order(leg(fake, parent)).status is OrderStatus.HELD
    assert fake.position("ABC") == 0


def test_a_bar_that_fills_the_entry_can_stop_it_out_too() -> None:
    fake = FakeBroker()
    parent = breakout(fake)
    stop = leg(fake, parent)
    assert fake.play("ABC", b(49.9, 50.4, 46.8, 47.0), slip=SLIP) == [parent, stop]
    assert fake.get_order(stop).filled_avg_price == 46.90
    assert fake.position("ABC") == 0


def test_a_jump_past_the_limit_rests_then_fills_at_the_limit() -> None:
    fake = FakeBroker()
    parent = breakout(fake)
    assert fake.play("ABC", b(51.5, 52, 51.2, 51.8), slip=SLIP) == []
    assert fake.play("ABC", b(51.8, 52.5, 51.4, 52.2), slip=SLIP) == []
    assert fake.play("ABC", b(51.8, 51.9, 50.8, 51.0), slip=SLIP) == [parent]
    assert fake.get_order(parent).filled_avg_price == 51.0


def test_a_limit_buy_is_marketable_on_the_next_bar() -> None:
    fake = FakeBroker()
    parent = breakout(fake, trigger=None)
    fake.play("ABC", b(50.5, 50.8, 50.3, 50.6), slip=SLIP)
    assert fake.get_order(parent).filled_avg_price == 50.61


def test_a_stop_gapped_through_fills_at_the_open() -> None:
    fake = FakeBroker()
    fake.set_price("ABC", 50)
    fake.fill(fake.submit(OtoBuy("ABC", 10, 51.0, 47.0, "e-1")).id, price=50)
    fake.cancel(next(o.id for o in fake.open_orders()))
    stop = fake.submit(StopSell("ABC", 10, 47.0, "s-1"))
    fake.play("ABC", b(45, 45.5, 44, 44.5), slip=SLIP)
    assert fake.get_order(stop.id).filled_avg_price == 44.91
    assert fake.cash == 5000 - 500 + 449.1


def test_sells_fill_after_stops_at_market_or_their_limit() -> None:
    fake = FakeBroker()
    fake.set_price("ABC", 50)
    fake.fill(fake.submit(OtoBuy("ABC", 10, 51.0, 47.0, "e-1")).id, price=50)
    fake.cancel(next(o.id for o in fake.open_orders()))
    at_market = fake.submit(MarketSell("ABC", 4, "x-1"))
    limit = fake.submit(LimitSell("ABC", 6, 50.5, "x-2"))
    assert fake.play("ABC", b(50.2, 50.4, 49.9, 50.1), slip=SLIP) == [at_market.id]
    assert fake.get_order(at_market.id).filled_avg_price == 50.09
    assert fake.play("ABC", b(50.1, 50.6, 50.0, 50.5), slip=SLIP) == [limit.id]
    assert fake.get_order(limit.id).filled_avg_price == 50.5
    assert fake.position("ABC") == 0
