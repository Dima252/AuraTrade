"""The fake broker reproduces what spike 0.3b found on Alpaca paper (README #18-116, #21)."""

from __future__ import annotations

import pytest

from auratrade.execution import ids
from auratrade.execution.broker import (
    AdvancedOrderChangeError,
    BrokerError,
    BrokerUnavailableError,
    ChainPendingError,
    DuplicateClientOrderIdError,
    InsufficientQtyError,
    LimitBuy,
    LimitSell,
    MarketSell,
    OrderNotFoundError,
    OrderStatus,
    OrderType,
    OtoBuy,
    StopThroughMarketError,
    TimeInForce,
    WashTradeError,
)
from auratrade.sim.fake_broker import FakeBroker, ProcessKilled
from tests.execution.helpers import SESSION, leg_of, oto_buy, standalone_stop


def test_an_oto_leg_takes_the_parents_gtc_and_quantity_and_waits_held() -> None:
    fake = FakeBroker()
    parent = oto_buy(fake, fill=False)
    leg = leg_of(fake, parent)
    assert parent.time_in_force is TimeInForce.GTC
    assert parent.type is OrderType.LIMIT
    assert (leg.time_in_force, leg.qty, leg.status) == (TimeInForce.GTC, 10, OrderStatus.HELD)
    assert leg.is_leg
    assert leg.client_order_id != parent.client_order_id


def test_a_leg_stays_held_while_the_parent_is_partly_filled() -> None:
    fake = FakeBroker()
    parent = oto_buy(fake, fill=False)
    fake.fill(parent.id, 4)
    assert fake.get_order(parent.id).status is OrderStatus.PARTIALLY_FILLED
    assert leg_of(fake, parent).status is OrderStatus.HELD
    assert fake.position("SSO") == 4
    fake.fill(parent.id)
    assert leg_of(fake, parent).status is OrderStatus.NEW
    assert fake.position("SSO") == 10


def test_a_legs_quantity_cant_change_but_its_price_can() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake))
    with pytest.raises(AdvancedOrderChangeError):
        fake.replace(leg.id, qty=12)
    raised = fake.replace(leg.id, stop_price=96.0)
    assert raised.id != leg.id
    assert raised.replaces == leg.id
    assert fake.get_order(leg.id).status is OrderStatus.REPLACED
    assert leg_of(fake, fake.get_order(raised.parent_id or "")).id == raised.id


def test_a_held_legs_price_can_change_while_held() -> None:
    fake = FakeBroker()
    parent = oto_buy(fake, fill=False)
    raised = fake.replace(leg_of(fake, parent).id, stop_price=96.0)
    assert raised.status is OrderStatus.HELD


def test_cancelling_a_held_leg_cancels_the_whole_oto() -> None:
    fake = FakeBroker()
    parent = oto_buy(fake, fill=False)
    fake.cancel(leg_of(fake, parent).id)
    assert fake.get_order(parent.id).status is OrderStatus.CANCELED
    assert not fake.open_orders()


def test_cancelling_a_live_leg_leaves_the_position() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake))
    fake.cancel(leg.id)
    assert fake.position("SSO") == 10
    assert not fake.open_orders()


@pytest.mark.parametrize(("mode", "leg_status"), [("cancel", None), ("activate", "new")])
def test_cancelling_a_partly_filled_parent(mode: str, leg_status: str | None) -> None:
    fake = FakeBroker(partial_cancel_leg=mode)  # type: ignore[arg-type]
    parent = oto_buy(fake, fill=False)
    fake.fill(parent.id, 4)
    fake.cancel(parent.id)
    legs = [o for o in fake.open_orders() if o.is_leg]
    if leg_status is None:
        assert not legs
    else:
        assert [(o.qty, o.status.value) for o in legs] == [(4, leg_status)]
    assert fake.position("SSO") == 4


def test_a_resting_stop_reserves_the_shares() -> None:
    fake = FakeBroker()
    oto_buy(fake)
    with pytest.raises(InsufficientQtyError):
        fake.submit(LimitSell("SSO", 1, 100.0, ids.exit_id(SESSION, "SSO", "REBALANCE")))


def test_a_plain_buy_is_a_wash_trade_while_a_stop_rests_and_an_oto_is_not() -> None:
    fake = FakeBroker()
    oto_buy(fake)
    with pytest.raises(WashTradeError):
        fake.submit(LimitBuy("SSO", 1, 102.0, "plain-buy"))
    add = oto_buy(fake, qty=5, fill=False)
    assert add.status is OrderStatus.NEW


def test_a_sell_stop_at_or_above_the_market_is_rejected() -> None:
    fake = FakeBroker()
    fake.set_price("SSO", 100.0)
    oto_buy(fake)
    fake.cancel(next(o.id for o in fake.open_orders()))
    with pytest.raises(StopThroughMarketError):
        standalone_stop(fake, "SSO", 10, 100.0)
    assert standalone_stop(fake, "SSO", 10, 99.99).status is OrderStatus.NEW


def test_client_order_ids_stay_unique_after_a_cancel() -> None:
    fake = FakeBroker()
    fake.set_price("F", 12.0)
    first = fake.submit(OtoBuy("F", 1, 12.1, 11.0, "at-dup"))
    fake.cancel(first.id)
    with pytest.raises(DuplicateClientOrderIdError):
        fake.submit(OtoBuy("F", 1, 12.1, 11.0, "at-dup"))
    assert fake.order_by_client_id("at-dup") == fake.get_order(first.id)


def test_a_replace_on_a_closed_market_stays_pending_and_holds_the_shares() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake, qty=11))
    fake.cancel(leg.id)
    stop = standalone_stop(fake, "SSO", 11, 95.0)
    fake.close_market()
    new = fake.replace(stop.id, qty=10)
    assert fake.get_order(stop.id).status is OrderStatus.PENDING_REPLACE
    assert new.status is OrderStatus.ACCEPTED
    with pytest.raises(InsufficientQtyError):
        fake.submit(MarketSell("SSO", 1, "at-opg"))
    with pytest.raises(ChainPendingError):
        fake.replace(new.id, qty=11)
    with pytest.raises(ChainPendingError):
        fake.replace(stop.id, qty=11)
    fake.open_market()
    assert fake.get_order(stop.id).status is OrderStatus.REPLACED
    assert fake.get_order(new.id).status is OrderStatus.NEW


def test_an_order_placed_while_closed_is_accepted_and_cant_be_replaced() -> None:
    fake = FakeBroker(market_open=False)
    fake.set_price("SSO", 101.0)
    parent = fake.submit(OtoBuy("SSO", 1, 101.0, 95.0, "at-x"))
    assert parent.status is OrderStatus.ACCEPTED
    with pytest.raises(BrokerError, match="accepted"):
        fake.replace(parent.id, limit_price=100.0)


def test_day_orders_expire_at_the_close_and_gtc_orders_rest() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake))
    fake.cancel(leg.id)
    stop = standalone_stop(fake, "SSO", 9, 95.0)
    sale = fake.submit(LimitSell("SSO", 1, 150.0, ids.exit_id(SESSION, "SSO", "REBALANCE")))
    pending = oto_buy(fake, "AMD", 3, limit=50.0, stop=46.0, fill=False)
    fake.close_market()
    assert fake.get_order(sale.id).status is OrderStatus.EXPIRED
    assert fake.get_order(stop.id).status is OrderStatus.NEW
    assert fake.get_order(pending.id).status is OrderStatus.NEW  # a GTC parent rests


def test_a_split_ex_date_rejects_stops_and_leaves_the_position() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake))
    fake.split_ex_date("SSO")
    assert fake.get_order(leg.id).status is OrderStatus.REJECTED
    assert fake.position("SSO") == 10


def test_fault_injection() -> None:
    fake = FakeBroker()
    fake.set_price("SSO", 101.0)
    fake.fail_next("submit")
    with pytest.raises(BrokerUnavailableError):
        fake.submit(OtoBuy("SSO", 1, 101.0, 95.0, "at-a"))
    assert fake.order_by_client_id("at-a") is None  # never reached the broker

    fake.drop_next("submit")
    with pytest.raises(BrokerUnavailableError):
        fake.submit(OtoBuy("SSO", 1, 101.0, 95.0, "at-b"))
    assert fake.order_by_client_id("at-b") is not None  # accepted, answer lost

    fake.kill_after("cancel", skip=1)
    fake.cancel(fake.order_by_client_id("at-b").id)  # type: ignore[union-attr]
    parent = fake.submit(OtoBuy("SSO", 1, 101.0, 95.0, "at-c"))
    with pytest.raises(ProcessKilled):
        fake.cancel(parent.id)
    assert fake.get_order(parent.id).status is OrderStatus.CANCELED  # done before the death


def test_an_order_deleted_at_the_broker() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake))
    fake.delete(leg.id)
    assert fake.get_order(leg.id).status is OrderStatus.CANCELED
    assert not fake.open_orders()
    assert fake.position("SSO") == 10
    with pytest.raises(OrderNotFoundError):
        fake.get_order("nope")


def test_fills_move_the_position_and_cash() -> None:
    fake = FakeBroker(cash=5000.0)
    parent = oto_buy(fake, qty=10, limit=100.0, stop=95.0)
    assert fake.account().cash == pytest.approx(4000.0)
    fake.fill(leg_of(fake, parent).id, price=94.0)
    assert fake.position("SSO") == 0
    assert fake.account().cash == pytest.approx(4940.0)
    assert [o.status for o in fake.open_orders()] == []


def test_fills_and_cash_entries_are_the_accounts_activities() -> None:
    fake = FakeBroker(cash=5000.0)
    parent = oto_buy(fake, qty=10, limit=100.0, stop=95.0, fill=False)
    fake.fill(parent.id, qty=4)
    fake.fill(parent.id)
    fake.distribute("SSO", 1.5)
    fake.distribute(None, 0.2, kind="INT")
    buys = fake.activities()
    assert [(a.kind, a.qty, a.price, a.order_id) for a in buys[:2]] == [
        ("FILL", 4.0, 100.0, parent.id),
        ("FILL", 6.0, 100.0, parent.id),
    ]
    assert [(a.kind, a.symbol, a.amount) for a in buys[2:]] == [
        ("DIV", "SSO", 1.5),
        ("INT", None, 0.2),
    ]
    assert [a.id for a in buys] == sorted(a.id for a in buys)
    assert {a.day for a in buys} == {SESSION}
    assert fake.account().cash == pytest.approx(4000.0 + 1.7)


def test_sleep_runs_the_tests_hooks() -> None:
    fake = FakeBroker()
    seen: list[float] = []
    fake.on_sleep.append(seen.append)
    before = fake.now
    fake.sleep(2)
    assert seen == [2]
    assert (fake.now - before).total_seconds() == 2
