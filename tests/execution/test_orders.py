"""The order desk's sequences against the fake broker (README §5.7)."""

from __future__ import annotations

import pytest

from auratrade.execution import ids
from auratrade.execution.broker import (
    BrokerOrder,
    BrokerUnavailableError,
    InsufficientQtyError,
    LimitBuy,
    LimitSell,
    OrderStatus,
    OrderType,
    OtoBuy,
    Side,
    TimeInForce,
)
from auratrade.execution.orders import SaleStatus
from auratrade.sim.fake_broker import FakeBroker
from tests.execution.helpers import (
    SESSION,
    desk,
    leg_of,
    open_buys,
    oto_buy,
    protected,
    standalone_stop,
)


def sells(fake: FakeBroker, kind: OrderType | None = None) -> list[BrokerOrder]:
    return [
        o
        for o in fake.all_orders()
        if o.side is Side.SELL and not o.is_sell_stop and (kind is None or o.type is kind)
    ]


def fill_sales_on_submit(fake: FakeBroker, *, types: tuple[OrderType, ...]) -> None:
    def hook(order: BrokerOrder) -> None:
        if order.side is Side.SELL and order.type in types:
            fake.fill(order.id, price=fake.prices([order.symbol])[order.symbol])

    fake.on_submit.append(hook)


# --- calls that survive a lost answer -------------------------------------------------------


def test_submit_once_retries_a_500_with_the_same_id() -> None:
    fake = FakeBroker()
    fake.set_price("SSO", 100.0)
    fake.fail_next("submit")
    request = OtoBuy("SSO", 5, 101.0, 95.0, ids.buy_id(SESSION, "SSO"))
    order = desk(fake).submit_once(request)
    assert order.client_order_id == request.client_order_id
    assert len(open_buys(fake, "SSO")) == 1


def test_submit_once_finds_an_order_whose_answer_was_lost() -> None:
    fake = FakeBroker()
    fake.set_price("SSO", 100.0)
    fake.drop_next("submit")
    request = OtoBuy("SSO", 5, 101.0, 95.0, ids.buy_id(SESSION, "SSO"))
    order = desk(fake).submit_once(request)
    assert order.client_order_id == request.client_order_id
    assert len(open_buys(fake, "SSO")) == 1
    assert fake.calls.count("submit") == 1


def test_submit_once_after_a_lost_answer_and_a_failed_lookup() -> None:
    fake = FakeBroker()
    fake.set_price("SSO", 100.0)
    fake.drop_next("submit")
    fake.fail_next("order_by_client_id")
    request = OtoBuy("SSO", 5, 101.0, 95.0, ids.buy_id(SESSION, "SSO"))
    desk(fake).submit_once(request)  # the retry hits the duplicate id and looks it up
    assert len(open_buys(fake, "SSO")) == 1


def test_submit_once_gives_up_after_its_attempts() -> None:
    fake = FakeBroker()
    fake.set_price("SSO", 100.0)
    for _ in range(3):
        fake.fail_next("submit")
    request = OtoBuy("SSO", 5, 101.0, 95.0, "at-x")
    with pytest.raises(BrokerUnavailableError):
        desk(fake).submit_once(request)
    assert not fake.open_orders()


def test_a_retried_job_resubmitting_an_entry_gets_the_first_order() -> None:
    fake = FakeBroker()
    fake.set_price("SSO", 100.0)
    request = OtoBuy("SSO", 5, 101.0, 95.0, ids.buy_id(SESSION, "SSO"))
    first = desk(fake).submit_once(request)
    second = desk(fake).submit_once(request)
    assert first.id == second.id
    assert len(open_buys(fake, "SSO")) == 1


def test_cancel_once_treats_a_closed_order_as_done() -> None:
    fake = FakeBroker()
    parent = oto_buy(fake)
    d = desk(fake)
    d.cancel_once(parent)  # already filled
    leg = leg_of(fake, parent)
    fake.drop_next("cancel")
    d.cancel_once(leg)
    assert fake.get_order(leg.id).status is OrderStatus.CANCELED


def test_replace_once_finds_a_replacement_whose_answer_was_lost() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake))
    fake.drop_next("replace")
    new = desk(fake).replace_once(leg, stop_price=96.0)
    assert new.replaces == leg.id
    assert new.stop_price == 96.0


# --- protect ---------------------------------------------------------------------------------


def test_protect_leaves_a_protected_position_alone() -> None:
    fake = FakeBroker()
    oto_buy(fake)
    result = desk(fake).protect("SSO", 95.0)
    assert not result.changed
    assert fake.calls.count("submit") == 1


def test_protect_replaces_a_deleted_stop() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, oto_buy(fake)).id)
    result = desk(fake).protect("SSO", 95.0)
    assert result.changed
    assert protected(fake, {"SSO": 95.0})
    stop = next(o for o in fake.open_orders())
    assert (stop.qty, stop.stop_price, stop.time_in_force) == (10, 95.0, TimeInForce.GTC)


@pytest.mark.parametrize("mode", ["cancel", "activate"])
def test_protect_a_partly_filled_buy(mode: str) -> None:
    fake = FakeBroker(partial_cancel_leg=mode)  # type: ignore[arg-type]
    parent = oto_buy(fake, fill=False)
    fake.fill(parent.id, 4)
    desk(fake).protect("SSO", 95.0)
    assert fake.get_order(parent.id).status is OrderStatus.CANCELED
    assert protected(fake, {"SSO": 95.0})
    assert sum(o.qty for o in fake.open_orders()) == 4


def test_protect_raises_a_stop_below_the_plan_and_replaces_a_day_stop() -> None:
    fake = FakeBroker()
    oto_buy(fake, stop=94.0)
    desk(fake).protect("SSO", 95.0)
    assert [o.stop_price for o in fake.open_orders()] == [95.0]

    fake2 = FakeBroker()
    fake2.set_price("AMD", 50.0)
    parent = fake2.submit(OtoBuy("AMD", 3, 50.0, 46.0, "at-day"))
    fake2._set(parent.id, time_in_force=TimeInForce.DAY)
    fake2.fill(parent.id, price=50.0)
    leg = leg_of(fake2, parent)
    fake2._set(leg.id, time_in_force=TimeInForce.DAY)
    desk(fake2).protect("AMD", 46.0)
    assert protected(fake2, {"AMD": 46.0})
    assert fake2.get_order(leg.id).status is OrderStatus.CANCELED


def test_protect_cancels_a_stray_sale_and_covers_the_shares() -> None:
    fake = FakeBroker()
    fake.cancel(leg_of(fake, oto_buy(fake)).id)
    standalone_stop(fake, "SSO", 4, 95.0)
    stray = fake.submit(LimitSell("SSO", 6, 150.0, ids.exit_id(SESSION, "SSO", "REBALANCE")))
    add = oto_buy(fake, qty=2, fill=False)  # an unfilled add is left alone
    desk(fake).protect("SSO", 95.0)
    assert fake.get_order(stray.id).status is OrderStatus.CANCELED
    assert fake.get_order(add.id).is_open
    assert protected(fake, {"SSO": 95.0})
    stops = [o for o in fake.open_orders() if o.is_sell_stop and not o.is_leg]
    assert sorted(o.qty for o in stops) == [4, 6]


def test_protect_fixes_over_coverage() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake))
    fake.cancel(leg.id)
    standalone_stop(fake, "SSO", 10, 95.0)
    fake._positions["SSO"].qty = 6  # 4 shares sold by hand at the broker
    desk(fake).protect("SSO", 95.0)
    assert [o.qty for o in fake.open_orders()] == [6]


def test_protect_cancels_orphan_stops() -> None:
    fake = FakeBroker()
    leg_of(fake, oto_buy(fake))
    fake._positions["SSO"].qty = 0
    result = desk(fake).protect("SSO", 95.0)
    assert result.changed
    assert not fake.open_orders()


def test_protect_sells_when_the_price_is_through_the_level() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, oto_buy(fake)).id)
    fake.set_price("SSO", 94.0)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    result = desk(fake).protect("SSO", 95.0)
    assert result.sale is not None
    assert result.sale.status is SaleStatus.FILLED
    assert fake.position("SSO") == 0
    assert not fake.open_orders()


def test_protect_sells_when_the_broker_rejects_the_stop_as_through_the_market() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, oto_buy(fake)).id)
    fake.set_price("SSO", 95.005)  # just above the level: our check passes it...
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    real_check = fake._check_stop

    def strict(symbol: str, stop_price: float) -> None:
        real_check(symbol, stop_price + 0.01)  # ...but the broker's own price is lower

    fake._check_stop = strict  # type: ignore[method-assign]
    result = desk(fake).protect("SSO", 95.0)
    assert result.sale is not None
    assert result.sale.sold == 10


def test_protect_raising_a_leg_through_the_market_sells_everything() -> None:
    fake = FakeBroker()
    oto_buy(fake, stop=94.0)
    fake.set_price("SSO", 94.5)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    result = desk(fake).protect("SSO", 95.0)
    assert result.sale is not None
    assert result.sale.sold == 10
    assert fake.position("SSO") == 0
    assert not fake.open_orders()


def test_protect_before_the_open_defers_the_sale() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, oto_buy(fake)).id)
    fake.close_market()
    fake.set_price("SSO", 94.0)
    alerts: list[str] = []
    result = desk(fake, alerts=alerts).protect("SSO", 95.0)
    assert result.sale is not None
    assert result.sale.status is SaleStatus.DEFERRED
    assert fake.position("SSO") == 10
    assert alerts


def test_protect_doesnt_sell_a_symbol_blocked_by_a_split() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, oto_buy(fake)).id)
    fake.set_price("SSO", 34.0)
    d = desk(fake)
    d.blocked_sales.add("SSO")
    result = d.protect("SSO", 95.0)
    assert result.sale is not None
    assert result.sale.status is SaleStatus.DEFERRED
    assert fake.position("SSO") == 10


def test_protect_alerts_when_the_stop_cant_be_placed() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, oto_buy(fake)).id)
    fake.fail_next("submit", InsufficientQtyError("insufficient qty", status=403))
    alerts: list[str] = []
    result = desk(fake, alerts=alerts).protect("SSO", 95.0)
    assert result.failed
    assert alerts


# --- the sell sequence --------------------------------------------------------------------


def test_a_full_exit_cancels_the_stop_and_sells() -> None:
    fake = FakeBroker()
    oto_buy(fake)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    result = desk(fake).sell("SSO", 10, "REBALANCE", 95.0)
    assert result.status is SaleStatus.FILLED
    assert fake.position("SSO") == 0
    assert not fake.open_orders()
    sale = sells(fake)[0]
    assert ids.parse(sale.client_order_id).detail == "REBALANCE"  # type: ignore[union-attr]
    assert sale.limit_price == 100.49  # 0.5% below 101, rounded down to a cent


def test_a_partial_sale_swaps_the_legs_for_one_standalone_stop() -> None:
    fake = FakeBroker()
    oto_buy(fake, qty=6)
    oto_buy(fake, qty=6)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    result = desk(fake).sell("SSO", 4, "REBALANCE", 95.0)
    assert result.status is SaleStatus.FILLED
    assert fake.position("SSO") == 8
    stops = list(fake.open_orders())
    assert [(o.is_leg, o.qty, o.stop_price) for o in stops] == [(False, 8, 95.0)]


def test_a_partial_sale_reduces_a_standalone_stop_in_place() -> None:
    fake = FakeBroker()
    fake.cancel(leg_of(fake, oto_buy(fake)).id)
    stop = standalone_stop(fake, "SSO", 10, 95.0)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    desk(fake).sell("SSO", 3, "REBALANCE", 95.0)
    assert fake.get_order(stop.id).status is OrderStatus.REPLACED
    assert [o.qty for o in fake.open_orders()] == [7]


def test_an_unfilled_sale_restores_the_stop() -> None:
    fake = FakeBroker()
    oto_buy(fake)
    result = desk(fake).sell("SSO", 10, "REBALANCE", 95.0)
    assert result.status is SaleStatus.UNFILLED
    assert result.owed == 10
    assert fake.position("SSO") == 10
    assert protected(fake, {"SSO": 95.0})
    assert all(o.status is OrderStatus.CANCELED for o in sells(fake))


def test_a_partly_filled_sale_restores_the_stop_on_the_rest() -> None:
    fake = FakeBroker()
    oto_buy(fake)

    def part(order: BrokerOrder) -> None:
        if order.side is Side.SELL and order.type is OrderType.LIMIT:
            fake.fill(order.id, 4)

    fake.on_submit.append(part)
    result = desk(fake).sell("SSO", 10, "REBALANCE", 95.0)
    assert (result.status, result.sold) == (SaleStatus.PARTIAL, 4)
    assert fake.position("SSO") == 6
    assert protected(fake, {"SSO": 95.0})


def test_a_rejected_sale_restores_the_stop() -> None:
    fake = FakeBroker()
    oto_buy(fake)
    alerts: list[str] = []
    fake.fail_next("submit", InsufficientQtyError("insufficient qty", status=403))
    result = desk(fake, alerts=alerts).sell("SSO", 10, "REBALANCE", 95.0)
    assert result.status is SaleStatus.UNFILLED
    assert alerts
    assert protected(fake, {"SSO": 95.0})


def test_a_sale_without_a_price_waits() -> None:
    fake = FakeBroker()
    oto_buy(fake)
    fake.set_price("SSO", None)
    result = desk(fake).sell("SSO", 10, "REBALANCE", 95.0)
    assert result.status is SaleStatus.UNFILLED
    assert protected(fake)


def test_no_sale_on_a_closed_market() -> None:
    fake = FakeBroker()
    oto_buy(fake)
    fake.close_market()
    result = desk(fake).sell("SSO", 10, "REBALANCE", 95.0)
    assert result.status is SaleStatus.DEFERRED
    assert fake.calls.count("cancel") == 0


def test_selling_nothing() -> None:
    fake = FakeBroker()
    fake.set_price("SSO", 100.0)
    assert desk(fake).sell("SSO", 10, "REBALANCE", 95.0).status is SaleStatus.NOTHING
    assert desk(fake).sell_now("SSO", 0, "STOP").status is SaleStatus.NOTHING


def test_a_sale_whose_restore_finds_the_price_through_the_stop_sells_at_once() -> None:
    fake = FakeBroker()
    oto_buy(fake)

    def drop(seconds: float) -> None:
        fake.set_price("SSO", 94.0)

    fake.on_sleep.append(drop)
    calls = {"n": 0}

    def fill_market(order: BrokerOrder) -> None:
        if order.type is OrderType.MARKET:
            fake.fill(order.id, price=93.9)
        calls["n"] += 1

    fake.on_submit.append(fill_market)
    result = desk(fake).sell("SSO", 10, "REBALANCE", 95.0)
    assert result.status is SaleStatus.FILLED
    assert fake.position("SSO") == 0


# --- selling at once ------------------------------------------------------------------------


def test_sell_now_escalates_limit_reprice_market() -> None:
    fake = FakeBroker()
    oto_buy(fake)
    fake.delete(leg_of(fake, fake.get_order(fake.all_orders()[0].id)).id)
    fake.set_price("SSO", 94.0)
    fill_sales_on_submit(fake, types=(OrderType.MARKET,))
    result = desk(fake).sell_now("SSO", 10, "STOP")
    assert result.status is SaleStatus.FILLED
    kinds = [o.type for o in sells(fake)]
    assert kinds == [OrderType.LIMIT, OrderType.LIMIT, OrderType.MARKET]
    limit, repriced, _ = sells(fake)
    assert repriced.replaces == limit.id
    assert [o.status for o in sells(fake)[:2]] == [OrderStatus.REPLACED, OrderStatus.CANCELED]


def test_sell_now_stops_at_the_first_fill() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, oto_buy(fake)).id)
    fake.set_price("SSO", 94.0)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    assert desk(fake).sell_now("SSO", 10, "STOP").status is SaleStatus.FILLED
    assert len(sells(fake)) == 1


def test_sell_now_without_a_price_goes_to_market() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, oto_buy(fake)).id)
    fake.set_price("SSO", None)

    def fill_market(order: BrokerOrder) -> None:
        if order.type is OrderType.MARKET:
            fake.fill(order.id, price=90.0)

    fake.on_submit.append(fill_market)
    assert desk(fake).sell_now("SSO", 10, "STOP").sold == 10


def test_sell_now_unfilled_alerts_and_leaves_it_owed() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, oto_buy(fake)).id)
    fake.set_price("SSO", 94.0)
    alerts: list[str] = []
    result = desk(fake, alerts=alerts).sell_now("SSO", 10, "STOP")
    assert result.status is SaleStatus.UNFILLED
    assert result.owed == 10
    assert alerts
    assert not [o for o in fake.open_orders() if o.side is Side.SELL]


# --- entries ----------------------------------------------------------------------------------


def test_cancel_entries_cancels_only_unfilled_parents() -> None:
    fake = FakeBroker()
    waiting = oto_buy(fake, "AMD", 3, limit=50.0, stop=46.0, fill=False)
    partial = oto_buy(fake, "SSO", fill=False)
    fake.fill(partial.id, 4)
    oto_buy(fake, "MSFT", 2, limit=400.0, stop=380.0)
    count = desk(fake).cancel_entries(why="EOD")
    assert count == 1
    assert fake.get_order(waiting.id).status is OrderStatus.CANCELED
    assert fake.get_order(partial.id).is_open
    assert desk(fake).cancel_entries(["SSO"], why="kill") == 0


# --- the portfolio's sale and buy (task 5.1) -----------------------------------------------


def holding(fake: FakeBroker, symbol: str = "SPY", qty: int = 10, price: float = 500.0) -> None:
    """A fund bought earlier, with no stop (#155)."""
    fake.set_price(symbol, price)
    order = fake.submit(LimitBuy(symbol, qty, price, ids.buy_id(SESSION, symbol)))
    fake.fill(order.id, price=price)


def test_a_plain_sale_fills_at_a_marketable_limit() -> None:
    fake = FakeBroker(cash=100_000.0)
    holding(fake)
    fake.on_submit.append(lambda o: fake.fill(o.id) if o.side is Side.SELL else None)
    result = desk(fake).sell_plain("SPY", 4, "REBALANCE")
    assert (result.sold, result.status) == (4, SaleStatus.FILLED)
    sale = next(o for o in fake.all_orders() if o.side is Side.SELL)
    assert (sale.type, sale.limit_price) == (OrderType.LIMIT, 497.5)
    assert fake.position("SPY") == 6


def test_a_plain_sale_that_doesnt_fill_is_cancelled_and_owed() -> None:
    fake = FakeBroker(cash=100_000.0)
    holding(fake)
    result = desk(fake).sell_plain("SPY", 4, "REBALANCE")
    assert (result.sold, result.status) == (0, SaleStatus.UNFILLED)
    assert fake.open_orders() == []


@pytest.mark.parametrize(
    ("setup", "status", "alerted"),
    [
        ("closed", SaleStatus.DEFERRED, False),
        ("blocked", SaleStatus.DEFERRED, False),
        ("nothing", SaleStatus.NOTHING, False),
        ("no_price", SaleStatus.UNFILLED, True),
        ("rejected", SaleStatus.UNFILLED, True),
    ],
)
def test_a_plain_sale_that_cant_go_in(setup: str, status: SaleStatus, alerted: bool) -> None:
    fake = FakeBroker(cash=100_000.0)
    alerts: list[str] = []
    if setup != "nothing":
        holding(fake)
    d = desk(fake, alerts=alerts, market_open=setup != "closed")
    if setup == "blocked":
        d.blocked_sales.add("SPY")
    if setup == "no_price":
        fake.set_price("SPY", None)
    if setup == "rejected":
        fake.fail_next("submit", InsufficientQtyError("insufficient qty", status=403))
    assert d.sell_plain("SPY", 4, "REBALANCE").status is status
    assert bool(alerts) is alerted


def test_a_buy_is_a_marketable_limit_and_an_oto_for_a_stopped_fund() -> None:
    fake = FakeBroker(cash=100_000.0)
    fake.set_price("SPY", 500.0)
    fake.set_price("SSO", 100.0)
    fake.on_submit.append(lambda o: fake.fill(o.id) if o.side is Side.BUY else None)
    d = desk(fake)
    plain = d.buy("SPY", 10, 502.5, stop=None)
    oto = d.buy("SSO", 20, 100.5, stop=70.0)
    assert (plain.bought, oto.bought) == (10, 20)
    assert [o.symbol for o in fake.open_orders() if o.is_sell_stop] == ["SSO"]
    buys = {o.symbol: o for o in fake.all_orders() if o.side is Side.BUY}
    assert (buys["SPY"].type, buys["SPY"].limit_price) == (OrderType.LIMIT, 502.5)
    assert (buys["SSO"].type, buys["SSO"].time_in_force) == (OrderType.LIMIT, TimeInForce.GTC)
    assert ids.parse(buys["SPY"].client_order_id).purpose is ids.Purpose.BUY  # type: ignore[union-attr]


def test_a_buy_that_doesnt_fill_is_cancelled() -> None:
    fake = FakeBroker(cash=100_000.0)
    fake.set_price("SSO", 100.0)
    result = desk(fake).buy("SSO", 20, 100.5, stop=70.0)
    assert (result.bought, result.owed) == (0, 20)
    assert fake.open_orders() == []


def test_a_buy_refused_by_the_broker_alerts() -> None:
    fake = FakeBroker(cash=100_000.0)
    fake.set_price("SSO", 100.0)
    alerts: list[str] = []
    for _ in range(3):  # every attempt (execution.call_attempts)
        fake.fail_next("submit", BrokerUnavailableError("500"))
    result = desk(fake, alerts=alerts).buy("SSO", 20, 100.5, stop=70.0)
    assert result.failed is not None
    assert any("buy of 20 SSO" in a for a in alerts)


def test_no_buy_while_the_market_is_closed() -> None:
    fake = FakeBroker(cash=100_000.0, market_open=False)
    assert desk(fake).buy("SPY", 1, 500.0, stop=None).failed == "market closed"
    assert fake.all_orders() == []
