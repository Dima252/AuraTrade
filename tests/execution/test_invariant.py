from __future__ import annotations

from auratrade.execution import ids
from auratrade.execution.broker import LimitSell
from auratrade.execution.invariant import Problem, check, live_stops, unprotected
from auratrade.sim.fake_broker import FakeBroker
from tests.execution.helpers import SESSION, leg_of, oto_buy, standalone_stop


def problems(fake: FakeBroker, levels: dict[str, float] | None = None) -> dict[str, set[str]]:
    return {
        s: {p.value for p in prot.problems}
        for s, prot in check(fake.positions(), fake.open_orders(), levels).items()
    }


def test_a_filled_oto_is_protected_by_its_leg() -> None:
    fake = FakeBroker()
    oto_buy(fake)
    assert problems(fake) == {"SSO": set()}
    assert problems(fake, {"SSO": 95.0}) == {"SSO": set()}


def test_an_unfilled_entry_is_not_a_position() -> None:
    fake = FakeBroker()
    oto_buy(fake, fill=False)
    assert problems(fake) == {}


def test_a_partly_filled_entry_is_unprotected() -> None:
    fake = FakeBroker()
    parent = oto_buy(fake, fill=False)
    fake.fill(parent.id, 4)
    assert problems(fake) == {"SSO": {"PARTIAL_BUY", "UNCOVERED"}}


def test_two_buys_cover_together() -> None:
    fake = FakeBroker()
    oto_buy(fake, qty=6)
    oto_buy(fake, qty=6)
    result = check(fake.positions(), fake.open_orders(), {"SSO": 95.0})["SSO"]
    assert result.ok
    assert result.covered == 12
    assert len(result.stops) == 2


def test_a_stop_below_the_plan_level_counts_as_uncovered() -> None:
    fake = FakeBroker()
    oto_buy(fake, stop=95.0)
    assert problems(fake, {"SSO": 96.0}) == {"SSO": {"BELOW_PLAN", "UNCOVERED"}}
    assert problems(fake) == {"SSO": set()}  # stop_guard doesn't check levels


def test_a_missing_stop_and_an_orphan_stop() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake))
    fake.delete(leg.id)
    fake.set_price("AMD", 50.0)
    assert problems(fake) == {"SSO": {"UNCOVERED"}}
    result = check(fake.positions(), fake.open_orders())
    assert result["SSO"].missing == 10
    assert unprotected(result) == [result["SSO"]]


def test_over_coverage_and_a_stray_sale() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake))
    fake.cancel(leg.id)
    standalone_stop(fake, "SSO", 4, 95.0)
    fake.submit(LimitSell("SSO", 6, 100.0, ids.exit_id(SESSION, "SSO", "REBALANCE")))
    assert problems(fake) == {"SSO": {"STRAY_SELL", "UNCOVERED"}}


def test_the_replacement_of_a_pending_replace_isnt_counted_twice() -> None:
    fake = FakeBroker()
    leg = leg_of(fake, oto_buy(fake, qty=11))
    fake.cancel(leg.id)
    stop = standalone_stop(fake, "SSO", 11, 95.0)
    fake.close_market()
    fake.replace(stop.id, qty=10)
    stops = live_stops(fake.open_orders())
    assert [s.id for s in stops] == [stop.id]
    assert problems(fake) == {"SSO": set()}


def test_a_stop_with_no_position_is_an_orphan() -> None:
    fake = FakeBroker()
    parent = oto_buy(fake)
    fake.fill(leg_of(fake, parent).id, price=95.0)
    assert problems(fake) == {}
    fake.set_price("SSO", 101.0)
    oto_buy(fake, qty=2)
    fake.fill(next(o.id for o in fake.open_orders() if o.is_leg), price=95.0)
    assert problems(fake) == {}
    other = oto_buy(fake, "AMD", 3, limit=50.0, stop=46.0)
    fake.fill(other.id, price=50.0) if other.is_open else None
    fake._positions["AMD"].qty = 0  # sold outside the order layer
    assert problems(fake) == {"AMD": {Problem.ORPHAN_STOP.value}}
