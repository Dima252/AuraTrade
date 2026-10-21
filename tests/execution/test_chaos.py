"""Chaos tests of the order layer (execution plan task 1.7, "Accept (automated)").

Each scenario breaks something on purpose (a process killed mid-sequence, a retried job, two
jobs at once, a 500, a dropped connection, a deleted stop, a gap through the stop, a partial
fill) and ends with the two properties the order layer exists for:

- **no duplicate orders**: at most one entry per signal, no symbol with stops for more shares
  than it holds, no sale left working
- **no position without a full-quantity GTC stop** at or above its plan level
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from auratrade.db.models import Position, Signal
from auratrade.execution.broker import BrokerOrder, OrderStatus, OrderType, Side
from auratrade.execution.invariant import check
from auratrade.execution.lock import Holder
from auratrade.execution.runtime import order_section
from auratrade.execution.store import entities
from auratrade.jobs import eod_review, execute_entries, execute_open, post_market, stop_guard
from auratrade.sim.fake_broker import FakeBroker, ProcessKilled
from tests.execution.conftest import World


def assert_no_duplicates(world: World) -> None:
    fake = world.fake
    entries: dict[str, int] = {}
    for o in fake.all_orders():
        if o.side is Side.BUY and o.status is not OrderStatus.REJECTED and o.replaces is None:
            entries[o.client_order_id] = entries.get(o.client_order_id, 0) + 1
    assert all(n == 1 for n in entries.values()), entries
    per_symbol: dict[str, int] = {}
    for o in fake.open_orders():
        if o.side is Side.BUY:
            per_symbol[o.symbol] = per_symbol.get(o.symbol, 0) + 1
    assert all(n <= 1 for n in per_symbol.values()), per_symbol
    working_sales = [o for o in fake.open_orders() if o.side is Side.SELL and not o.is_sell_stop]
    assert not working_sales
    for protection in check(fake.positions(), fake.open_orders()).values():
        assert "OVERCOVERED" not in {p.value for p in protection.problems}


def assert_protected(world: World) -> None:
    with world.engine.connect() as conn:
        levels = {
            p.symbol: p.initial_stop
            for p in entities(conn, select(Position).where(Position.status == "OPEN"))
        }
    result = check(world.fake.positions(), world.fake.open_orders(), levels)
    assert all(p.ok for p in result.values()), {s: p.problems for s, p in result.items()}
    assert set(levels) >= {p.symbol for p in world.fake.positions()}


def enter(world: World, symbol: str = "NVDA") -> BrokerOrder:
    world.signal(symbol)
    world.run(execute_entries.make_job)
    parent = next(o for o in world.fake.all_orders() if o.symbol == symbol and o.side is Side.BUY)
    world.fake.fill(parent.id, price=100.5)
    world.run(execute_open.make_job)
    return parent


def sell_fills_at_market(fake: FakeBroker) -> None:
    fake.on_submit.append(
        lambda o: fake.fill(o.id, price=93.0) if o.type is OrderType.MARKET else None
    )


# --- a job killed between "reduce the stop" and "sell" -------------------------------------------


def test_a_job_killed_after_cancelling_the_stop_is_repaired_by_stop_guard(world: World) -> None:
    world.bars("NVDA", [110.0] * 120)
    enter(world)
    world.fake.set_price("NVDA", 100.0)  # below the 21 EMA: the EOD review exits
    world.fake.kill_after("cancel")
    with pytest.raises(ProcessKilled):
        world.run(eod_review.make_job)
    assert world.fake.position("NVDA") == 142
    assert not check(world.fake.positions(), world.fake.open_orders())["NVDA"].ok

    out = world.run(stop_guard.make_job)
    assert out["acted"] is True
    assert out["repaired"] == ["NVDA"]
    assert_protected(world)
    assert_no_duplicates(world)


def test_a_job_killed_after_reducing_the_stop_is_repaired_by_stop_guard(world: World) -> None:
    parent = enter(world)
    leg = next(o for o in world.fake.open_orders() if o.parent_id == parent.id)
    with order_section(world.context("eod_review"), world.deps()) as section:
        section.desk.protect("NVDA", 95.0)  # nothing to do
        world.fake.cancel(leg.id)
        section.desk.protect("NVDA", 95.0)  # one standalone stop now
    world.fake.kill_after("replace")
    with (
        pytest.raises(ProcessKilled),
        order_section(world.context("eod_review"), world.deps()) as section,
    ):
        section.desk.sell("NVDA", 40, "TRAIL", 95.0)
    assert [o.qty for o in world.fake.open_orders()] == [102]

    world.run(stop_guard.make_job)
    assert sorted(o.qty for o in world.fake.open_orders()) == [40, 102]
    assert_protected(world)
    assert_no_duplicates(world)


# --- the same order job twice (a Lambda retry) ---------------------------------------------


def test_the_same_entry_job_twice_places_one_order(world: World) -> None:
    world.signal("NVDA")
    world.run(execute_entries.make_job)
    world.run(execute_entries.make_job)
    assert len([o for o in world.fake.all_orders() if o.side is Side.BUY]) == 1
    assert_no_duplicates(world)


def test_an_entry_job_killed_after_submitting_and_retried_places_one_order(world: World) -> None:
    world.signal("NVDA")
    world.fake.kill_after("submit")
    with pytest.raises(ProcessKilled):
        world.run(execute_entries.make_job)
    with world.engine.connect() as conn:
        assert conn.execute(select(Signal.status)).scalar_one() == "PENDING"
    world.run(execute_entries.make_job)
    with world.engine.connect() as conn:
        assert conn.execute(select(Signal.status)).scalar_one() == "SUBMITTED"
    (parent,) = [o for o in world.fake.all_orders() if o.side is Side.BUY]
    world.fake.fill(parent.id, price=100.5)
    world.run(execute_open.make_job)
    assert_protected(world)
    assert_no_duplicates(world)


def test_a_filled_entry_whose_job_died_before_recording_it_becomes_a_position(
    world: World,
) -> None:
    world.signal("NVDA")
    world.fake.kill_after("submit")
    with pytest.raises(ProcessKilled):
        world.run(execute_entries.make_job)
    (parent,) = [o for o in world.fake.all_orders() if o.side is Side.BUY]
    world.fake.fill(parent.id, price=100.5)
    out = world.run(execute_open.make_job)
    assert out["opened"] == ["NVDA"]
    assert out["unknown"] == []
    assert_protected(world)
    assert_no_duplicates(world)


def test_the_eod_review_twice_sells_once(world: World) -> None:
    world.bars("NVDA", [110.0] * 120)
    enter(world)
    world.fake.set_price("NVDA", 100.0)
    world.fake.on_submit.append(
        lambda o: world.fake.fill(o.id, price=99.9) if o.side is Side.SELL else None
    )
    world.run(eod_review.make_job)
    world.run(eod_review.make_job)
    sales = [o for o in world.fake.all_orders() if o.side is Side.SELL and not o.is_sell_stop]
    assert len(sales) == 1
    assert world.fake.position("NVDA") == 0
    assert_no_duplicates(world)


# --- two order jobs at the same time ---------------------------------------------------------


def test_a_second_order_job_waits_for_the_lock(world: World) -> None:
    enter(world)
    finished: list[dict[str, object]] = []
    with world.lock.hold("eod_review"):
        calls = len(world.fake.calls)
        runner = threading.Thread(
            target=lambda: finished.append(world.run(execute_open.make_job)), daemon=True
        )
        runner.start()
        time.sleep(0.6)
        assert runner.is_alive()
        assert not finished
        assert len(world.fake.calls) == calls  # it hasn't touched the broker
    runner.join(timeout=10)
    assert finished
    assert finished[0]["problems"] == {}


def test_stop_guard_skips_while_another_job_holds_the_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = enter(world)
    world.fake.delete(next(o.id for o in world.fake.open_orders() if o.parent_id == parent.id))
    with world.lock.hold("eod_review"):
        out = world.run(stop_guard.make_job)
        assert out["skipped"] == "order lock held"
        holder = world.lock.holder()
        assert holder is not None
        assert holder.application_name == "auratrade-lock:eod_review"
        assert not [t for t in world.alerts.texts if "has been held" in t]
        stuck = Holder(holder.application_name, timedelta(minutes=30))
        monkeypatch.setattr(world.lock, "holder", lambda: stuck)
        world.run(stop_guard.make_job)
        assert [t for t in world.alerts.texts if "has been held" in t]
    assert world.lock.__class__.holder(world.lock) is None
    world.run(stop_guard.make_job)
    assert_protected(world)


# --- a 500, and a dropped connection after the broker accepted --------------------------------


def test_a_500_on_an_entry_submit_is_retried_once_with_the_same_id(world: World) -> None:
    world.signal("NVDA")
    world.fake.fail_next("submit")
    world.run(execute_entries.make_job)
    assert len([o for o in world.fake.all_orders() if o.side is Side.BUY]) == 1
    assert_no_duplicates(world)


def test_a_dropped_connection_after_the_broker_accepted_the_entry(world: World) -> None:
    world.signal("NVDA")
    world.fake.drop_next("submit")
    world.run(execute_entries.make_job)
    assert len([o for o in world.fake.all_orders() if o.side is Side.BUY]) == 1
    with world.engine.connect() as conn:
        assert conn.execute(select(Signal.status)).scalar_one() == "SUBMITTED"
    assert_no_duplicates(world)


def test_a_dropped_connection_on_a_repair_stop_places_one_stop(world: World) -> None:
    parent = enter(world)
    world.fake.delete(next(o.id for o in world.fake.open_orders() if o.parent_id == parent.id))
    world.fake.drop_next("submit")
    world.run(stop_guard.make_job)
    assert [o.qty for o in world.fake.open_orders()] == [142]
    assert_protected(world)
    assert_no_duplicates(world)


def test_a_500_on_every_attempt_fails_the_run_and_the_next_one_repairs(world: World) -> None:
    parent = enter(world)
    world.fake.delete(next(o.id for o in world.fake.open_orders() if o.parent_id == parent.id))
    for _ in range(world.risk.execution.call_attempts):
        world.fake.fail_next("submit")
    out = world.run(stop_guard.make_job)
    assert out["still_unprotected"] == ["NVDA"]
    assert [t for t in world.alerts.texts if "could not place a stop" in t]
    world.run(stop_guard.make_job)
    assert_protected(world)
    assert_no_duplicates(world)


# --- a stop deleted at the broker ----------------------------------------------------------------


def test_a_stop_deleted_at_the_broker_is_replaced_by_stop_guard(world: World) -> None:
    parent = enter(world)
    world.fake.delete(next(o.id for o in world.fake.open_orders() if o.parent_id == parent.id))
    world.run(stop_guard.make_job)
    (stop,) = world.fake.open_orders()
    assert (stop.qty, stop.stop_price, stop.time_in_force.value) == (142, 95.0, "gtc")
    assert_protected(world)
    assert_no_duplicates(world)


# --- the price already through the stop level ------------------------------------------------


def test_a_gap_through_the_stop_sells_at_once_escalating_to_market(world: World) -> None:
    parent = enter(world)
    world.fake.delete(next(o.id for o in world.fake.open_orders() if o.parent_id == parent.id))
    world.fake.set_price("NVDA", 93.5)
    sell_fills_at_market(world.fake)
    world.run(stop_guard.make_job)
    sales = [o for o in world.fake.all_orders() if o.side is Side.SELL and not o.is_sell_stop]
    assert [o.type for o in sales] == [OrderType.LIMIT, OrderType.LIMIT, OrderType.MARKET]
    assert sales[1].replaces == sales[0].id  # the re-price
    assert world.fake.position("NVDA") == 0
    world.run(post_market.make_job)
    with world.engine.connect() as conn:
        (position,) = entities(conn, select(Position))
    assert position.exit_reason == "HARD_STOP"
    assert_no_duplicates(world)


def test_a_stop_below_the_plan_that_cant_be_raised_sells(world: World) -> None:
    enter(world)
    leg = next(o for o in world.fake.open_orders())
    world.fake.replace(leg.id, stop_price=90.0)  # someone lowered it at the broker
    world.fake.set_price("NVDA", 94.0)
    sell_fills_at_market(world.fake)
    world.run(execute_open.make_job)
    assert world.fake.position("NVDA") == 0
    assert_no_duplicates(world)


# --- a partly filled entry ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["cancel", "activate"])
def test_a_partly_filled_entry_is_protected_by_the_next_stop_guard(world: World, mode: str) -> None:
    world.fake.partial_cancel_leg = mode  # type: ignore[assignment]
    world.signal("NVDA")
    world.run(execute_entries.make_job)
    (parent,) = [o for o in world.fake.all_orders() if o.side is Side.BUY]
    world.fake.fill(parent.id, 50, price=100.5)
    out = world.run(stop_guard.make_job)
    assert out["problems"] == {"NVDA": ["PARTIAL_ENTRY", "UNCOVERED"]}
    assert world.fake.get_order(parent.id).status is OrderStatus.CANCELED
    assert sum(o.remaining_qty for o in world.fake.open_orders()) == 50
    with world.engine.connect() as conn:
        (position,) = entities(conn, select(Position))
    assert position.shares == 50
    assert_protected(world)
    assert_no_duplicates(world)
