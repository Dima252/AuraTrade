"""The order desk's sequences against the fake broker (README §5.7)."""

from __future__ import annotations

import pytest

from auratrade.execution import ids
from auratrade.execution.broker import (
    BrokerOrder,
    BrokerUnavailableError,
    InsufficientQtyError,
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
    entry,
    leg_of,
    open_entries,
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
    fake.set_price("NVDA", 100.0)
    fake.fail_next("submit")
    request = OtoBuy("NVDA", 5, 101.0, 95.0, ids.entry_id(SESSION, "NVDA"), trigger=100.5)
    order = desk(fake).submit_once(request)
    assert order.client_order_id == request.client_order_id
    assert len(open_entries(fake, "NVDA")) == 1


def test_submit_once_finds_an_order_whose_answer_was_lost() -> None:
    fake = FakeBroker()
    fake.set_price("NVDA", 100.0)
    fake.drop_next("submit")
    request = OtoBuy("NVDA", 5, 101.0, 95.0, ids.entry_id(SESSION, "NVDA"), trigger=100.5)
    order = desk(fake).submit_once(request)
    assert order.client_order_id == request.client_order_id
    assert len(open_entries(fake, "NVDA")) == 1
    assert fake.calls.count("submit") == 1


def test_submit_once_after_a_lost_answer_and_a_failed_lookup() -> None:
    fake = FakeBroker()
    fake.set_price("NVDA", 100.0)
    fake.drop_next("submit")
    fake.fail_next("order_by_client_id")
    request = OtoBuy("NVDA", 5, 101.0, 95.0, ids.entry_id(SESSION, "NVDA"), trigger=100.5)
    desk(fake).submit_once(request)  # the retry hits the duplicate id and looks it up
    assert len(open_entries(fake, "NVDA")) == 1


def test_submit_once_gives_up_after_its_attempts() -> None:
    fake = FakeBroker()
    fake.set_price("NVDA", 100.0)
    for _ in range(3):
        fake.fail_next("submit")
    request = OtoBuy("NVDA", 5, 101.0, 95.0, "at-x", trigger=100.5)
    with pytest.raises(BrokerUnavailableError):
        desk(fake).submit_once(request)
    assert not fake.open_orders()


def test_a_retried_job_resubmitting_an_entry_gets_the_first_order() -> None:
    fake = FakeBroker()
    fake.set_price("NVDA", 100.0)
    request = OtoBuy("NVDA", 5, 101.0, 95.0, ids.entry_id(SESSION, "NVDA"), trigger=100.5)
    first = desk(fake).submit_once(request)
    second = desk(fake).submit_once(request)
    assert first.id == second.id
    assert len(open_entries(fake, "NVDA")) == 1


def test_cancel_once_treats_a_closed_order_as_done() -> None:
    fake = FakeBroker()
    parent = entry(fake)
    d = desk(fake)
    d.cancel_once(parent)  # already filled
    leg = leg_of(fake, parent)
    fake.drop_next("cancel")
    d.cancel_once(leg)
    assert fake.get_order(leg.id).status is OrderStatus.CANCELED


def test_replace_once_finds_a_replacement_whose_answer_was_lost() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, entry(fake))
    fake.drop_next("replace")
    new = desk(fake).replace_once(leg, stop_price=96.0)
    assert new.replaces == leg.id
    assert new.stop_price == 96.0


# --- protect ---------------------------------------------------------------------------------


def test_protect_leaves_a_protected_position_alone() -> None:
    fake = FakeBroker()
    entry(fake)
    result = desk(fake).protect("NVDA", 95.0)
    assert not result.changed
    assert fake.calls.count("submit") == 1


def test_protect_replaces_a_deleted_stop() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, entry(fake)).id)
    result = desk(fake).protect("NVDA", 95.0)
    assert result.changed
    assert protected(fake, {"NVDA": 95.0})
    stop = next(o for o in fake.open_orders())
    assert (stop.qty, stop.stop_price, stop.time_in_force) == (10, 95.0, TimeInForce.GTC)


@pytest.mark.parametrize("mode", ["cancel", "activate"])
def test_protect_a_partly_filled_entry(mode: str) -> None:
    fake = FakeBroker(partial_cancel_leg=mode)  # type: ignore[arg-type]
    parent = entry(fake, fill=False)
    fake.fill(parent.id, 4)
    desk(fake).protect("NVDA", 95.0)
    assert fake.get_order(parent.id).status is OrderStatus.CANCELED
    assert protected(fake, {"NVDA": 95.0})
    assert sum(o.qty for o in fake.open_orders()) == 4


def test_protect_raises_a_stop_below_the_plan_and_replaces_a_day_stop() -> None:
    fake = FakeBroker()
    entry(fake, stop=94.0)
    desk(fake).protect("NVDA", 95.0)
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
    fake.cancel(leg_of(fake, entry(fake)).id)
    standalone_stop(fake, "NVDA", 4, 95.0)
    stray = fake.submit(LimitSell("NVDA", 6, 150.0, ids.exit_id(SESSION, "NVDA", "TRAIL")))
    add = entry(fake, qty=2, tranche=2, fill=False)  # an unfilled add is left alone
    desk(fake).protect("NVDA", 95.0)
    assert fake.get_order(stray.id).status is OrderStatus.CANCELED
    assert fake.get_order(add.id).is_open
    assert protected(fake, {"NVDA": 95.0})
    stops = [o for o in fake.open_orders() if o.is_sell_stop and not o.is_leg]
    assert sorted(o.qty for o in stops) == [4, 6]


def test_protect_fixes_over_coverage() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, entry(fake))
    fake.cancel(leg.id)
    standalone_stop(fake, "NVDA", 10, 95.0)
    fake._positions["NVDA"].qty = 6  # 4 shares sold by hand at the broker
    desk(fake).protect("NVDA", 95.0)
    assert [o.qty for o in fake.open_orders()] == [6]


def test_protect_cancels_orphan_stops() -> None:
    fake = FakeBroker()
    leg_of(fake, entry(fake))
    fake._positions["NVDA"].qty = 0
    result = desk(fake).protect("NVDA", 95.0)
    assert result.changed
    assert not fake.open_orders()


def test_protect_sells_when_the_price_is_through_the_level() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, entry(fake)).id)
    fake.set_price("NVDA", 94.0)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    result = desk(fake).protect("NVDA", 95.0)
    assert result.sale is not None
    assert result.sale.status is SaleStatus.FILLED
    assert fake.position("NVDA") == 0
    assert not fake.open_orders()


def test_protect_sells_when_the_broker_rejects_the_stop_as_through_the_market() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, entry(fake)).id)
    fake.set_price("NVDA", 95.005)  # just above the level: our check passes it...
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    real_check = fake._check_stop

    def strict(symbol: str, stop_price: float) -> None:
        real_check(symbol, stop_price + 0.01)  # ...but the broker's own price is lower

    fake._check_stop = strict  # type: ignore[method-assign]
    result = desk(fake).protect("NVDA", 95.0)
    assert result.sale is not None
    assert result.sale.sold == 10


def test_protect_raising_a_leg_through_the_market_sells_everything() -> None:
    fake = FakeBroker()
    entry(fake, stop=94.0)
    fake.set_price("NVDA", 94.5)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    result = desk(fake).protect("NVDA", 95.0)
    assert result.sale is not None
    assert result.sale.sold == 10
    assert fake.position("NVDA") == 0
    assert not fake.open_orders()


def test_protect_before_the_open_defers_the_sale() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, entry(fake)).id)
    fake.close_market()
    fake.set_price("NVDA", 94.0)
    alerts: list[str] = []
    result = desk(fake, alerts=alerts).protect("NVDA", 95.0)
    assert result.sale is not None
    assert result.sale.status is SaleStatus.DEFERRED
    assert fake.position("NVDA") == 10
    assert alerts


def test_protect_doesnt_sell_a_symbol_blocked_by_a_split() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, entry(fake)).id)
    fake.set_price("NVDA", 34.0)
    d = desk(fake)
    d.blocked_sales.add("NVDA")
    result = d.protect("NVDA", 95.0)
    assert result.sale is not None
    assert result.sale.status is SaleStatus.DEFERRED
    assert fake.position("NVDA") == 10


def test_protect_alerts_when_the_stop_cant_be_placed() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, entry(fake)).id)
    fake.fail_next("submit", InsufficientQtyError("insufficient qty", status=403))
    alerts: list[str] = []
    result = desk(fake, alerts=alerts).protect("NVDA", 95.0)
    assert result.failed
    assert alerts


# --- the sell sequence --------------------------------------------------------------------


def test_a_full_exit_cancels_the_stop_and_sells() -> None:
    fake = FakeBroker()
    entry(fake)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    result = desk(fake).sell("NVDA", 10, "TRAIL", 95.0)
    assert result.status is SaleStatus.FILLED
    assert fake.position("NVDA") == 0
    assert not fake.open_orders()
    sale = sells(fake)[0]
    assert ids.parse(sale.client_order_id).detail == "TRAIL"  # type: ignore[union-attr]
    assert sale.limit_price == 100.49  # 0.5% below 101, rounded down to a cent


def test_a_partial_sale_swaps_the_legs_for_one_standalone_stop() -> None:
    fake = FakeBroker()
    entry(fake, qty=6)
    entry(fake, qty=6, tranche=2)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    result = desk(fake).sell("NVDA", 4, "TRAIL", 95.0)
    assert result.status is SaleStatus.FILLED
    assert fake.position("NVDA") == 8
    stops = list(fake.open_orders())
    assert [(o.is_leg, o.qty, o.stop_price) for o in stops] == [(False, 8, 95.0)]


def test_a_partial_sale_reduces_a_standalone_stop_in_place() -> None:
    fake = FakeBroker()
    fake.cancel(leg_of(fake, entry(fake)).id)
    stop = standalone_stop(fake, "NVDA", 10, 95.0)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    desk(fake).sell("NVDA", 3, "TRAIL", 95.0)
    assert fake.get_order(stop.id).status is OrderStatus.REPLACED
    assert [o.qty for o in fake.open_orders()] == [7]


def test_an_unfilled_sale_restores_the_stop() -> None:
    fake = FakeBroker()
    entry(fake)
    result = desk(fake).sell("NVDA", 10, "TRAIL", 95.0)
    assert result.status is SaleStatus.UNFILLED
    assert result.owed == 10
    assert fake.position("NVDA") == 10
    assert protected(fake, {"NVDA": 95.0})
    assert all(o.status is OrderStatus.CANCELED for o in sells(fake))


def test_a_partly_filled_sale_restores_the_stop_on_the_rest() -> None:
    fake = FakeBroker()
    entry(fake)

    def part(order: BrokerOrder) -> None:
        if order.side is Side.SELL and order.type is OrderType.LIMIT:
            fake.fill(order.id, 4)

    fake.on_submit.append(part)
    result = desk(fake).sell("NVDA", 10, "TRAIL", 95.0)
    assert (result.status, result.sold) == (SaleStatus.PARTIAL, 4)
    assert fake.position("NVDA") == 6
    assert protected(fake, {"NVDA": 95.0})


def test_a_rejected_sale_restores_the_stop() -> None:
    fake = FakeBroker()
    entry(fake)
    alerts: list[str] = []
    fake.fail_next("submit", InsufficientQtyError("insufficient qty", status=403))
    result = desk(fake, alerts=alerts).sell("NVDA", 10, "TRAIL", 95.0)
    assert result.status is SaleStatus.UNFILLED
    assert alerts
    assert protected(fake, {"NVDA": 95.0})


def test_a_sale_without_a_price_waits() -> None:
    fake = FakeBroker()
    entry(fake)
    fake.set_price("NVDA", None)
    result = desk(fake).sell("NVDA", 10, "TRAIL", 95.0)
    assert result.status is SaleStatus.UNFILLED
    assert protected(fake)


def test_no_sale_on_a_closed_market() -> None:
    fake = FakeBroker()
    entry(fake)
    fake.close_market()
    result = desk(fake).sell("NVDA", 10, "TRAIL", 95.0)
    assert result.status is SaleStatus.DEFERRED
    assert fake.calls.count("cancel") == 0


def test_selling_nothing() -> None:
    fake = FakeBroker()
    fake.set_price("NVDA", 100.0)
    assert desk(fake).sell("NVDA", 10, "TRAIL", 95.0).status is SaleStatus.NOTHING
    assert desk(fake).sell_now("NVDA", 0, "STOP").status is SaleStatus.NOTHING


def test_a_sale_whose_restore_finds_the_price_through_the_stop_sells_at_once() -> None:
    fake = FakeBroker()
    entry(fake)

    def drop(seconds: float) -> None:
        fake.set_price("NVDA", 94.0)

    fake.on_sleep.append(drop)
    calls = {"n": 0}

    def fill_market(order: BrokerOrder) -> None:
        if order.type is OrderType.MARKET:
            fake.fill(order.id, price=93.9)
        calls["n"] += 1

    fake.on_submit.append(fill_market)
    result = desk(fake).sell("NVDA", 10, "TRAIL", 95.0)
    assert result.status is SaleStatus.FILLED
    assert fake.position("NVDA") == 0


# --- selling at once ------------------------------------------------------------------------


def test_sell_now_escalates_limit_reprice_market() -> None:
    fake = FakeBroker()
    entry(fake)
    fake.delete(leg_of(fake, fake.get_order(fake.all_orders()[0].id)).id)
    fake.set_price("NVDA", 94.0)
    fill_sales_on_submit(fake, types=(OrderType.MARKET,))
    result = desk(fake).sell_now("NVDA", 10, "STOP")
    assert result.status is SaleStatus.FILLED
    kinds = [o.type for o in sells(fake)]
    assert kinds == [OrderType.LIMIT, OrderType.LIMIT, OrderType.MARKET]
    limit, repriced, _ = sells(fake)
    assert repriced.replaces == limit.id
    assert [o.status for o in sells(fake)[:2]] == [OrderStatus.REPLACED, OrderStatus.CANCELED]


def test_sell_now_stops_at_the_first_fill() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, entry(fake)).id)
    fake.set_price("NVDA", 94.0)
    fill_sales_on_submit(fake, types=(OrderType.LIMIT,))
    assert desk(fake).sell_now("NVDA", 10, "STOP").status is SaleStatus.FILLED
    assert len(sells(fake)) == 1


def test_sell_now_without_a_price_goes_to_market() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, entry(fake)).id)
    fake.set_price("NVDA", None)

    def fill_market(order: BrokerOrder) -> None:
        if order.type is OrderType.MARKET:
            fake.fill(order.id, price=90.0)

    fake.on_submit.append(fill_market)
    assert desk(fake).sell_now("NVDA", 10, "STOP").sold == 10


def test_sell_now_unfilled_alerts_and_leaves_it_owed() -> None:
    fake = FakeBroker()
    fake.delete(leg_of(fake, entry(fake)).id)
    fake.set_price("NVDA", 94.0)
    alerts: list[str] = []
    result = desk(fake, alerts=alerts).sell_now("NVDA", 10, "STOP")
    assert result.status is SaleStatus.UNFILLED
    assert result.owed == 10
    assert alerts
    assert not [o for o in fake.open_orders() if o.side is Side.SELL]


# --- entries ----------------------------------------------------------------------------------


def test_cancel_entries_cancels_only_unfilled_parents() -> None:
    fake = FakeBroker()
    waiting = entry(fake, "AMD", 3, limit=50.0, stop=46.0, fill=False)
    partial = entry(fake, "NVDA", fill=False)
    fake.fill(partial.id, 4)
    entry(fake, "MSFT", 2, limit=400.0, stop=380.0)
    count = desk(fake).cancel_entries(why="EOD")
    assert count == 1
    assert fake.get_order(waiting.id).status is OrderStatus.CANCELED
    assert fake.get_order(partial.id).is_open
    assert desk(fake).cancel_entries(["NVDA"], why="kill") == 0
