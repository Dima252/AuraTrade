"""After the leveraged fund's stop fills, its proceeds go into the cash fund at the next 15:40
until the next E (task 5.2, README §7.2), through `execute_close` on the fake broker."""

from __future__ import annotations

from datetime import date

from auratrade.config import TradingMode
from auratrade.execution.broker import Side
from auratrade.jobs import execute_close
from tests.execution.conftest import World
from tests.execution.portfolio_world import Market, at_close, decided, protected, stops

NEXT = date(2026, 10, 23)
MONDAY = date(2026, 10, 26)
STOP_PRICE = 69.5


def stopped_out(world: World) -> None:
    """The first rebalance on E (298 SSO under a leg at 70), then the leg fills the next
    session before 15:40."""
    at_close(world)
    Market(world.fake)
    decided(world)
    world.run(execute_close.make_job)
    at_close(world, NEXT)
    [leg] = stops(world.fake)
    world.fake.fill(leg.id, price=STOP_PRICE)
    assert world.fake.position("SSO") == 0


def bil_bought(world: World) -> int:
    return sum(
        o.filled_qty for o in world.fake.all_orders() if o.symbol == "BIL" and o.side is Side.BUY
    )


def test_the_proceeds_go_into_the_cash_fund(world: World) -> None:
    stopped_out(world)
    out = world.run(execute_close.make_job, session=NEXT)
    proceeds = out["stop_proceeds"]
    assert (proceeds["stopped"], proceeds["proceeds"]) == (298, 298 * STOP_PRICE)
    # 20,711 at the 91.95 limit (91.50 + 0.5%).
    assert proceeds["shares"] == 225
    assert world.fake.position("BIL") == 225
    assert world.fake.cash >= 0
    assert protected(world)
    [alert] = [t for t in world.alerts.texts if "stop filled" in t]
    assert "298 shares sold for 20,711.00" in alert
    assert "--event STOP_FILLED" in alert


def test_later_runs_buy_only_what_is_still_owed(world: World) -> None:
    stopped_out(world)
    world.run(execute_close.make_job, session=NEXT)
    # 225 filled at 91.50, under the limit: 123.50 of the proceeds is still owed, one share.
    at_close(world, MONDAY)
    world.alerts.texts.clear()
    monday = world.run(execute_close.make_job, session=MONDAY)
    assert not any("stop filled" in t for t in world.alerts.texts)  # once, not every day
    assert (monday["stop_proceeds"]["already_in"], monday["stop_proceeds"]["shares"]) == (
        225 * 91.5,
        1,
    )
    later = world.run(execute_close.make_job, session=date(2026, 10, 27))
    # The rebalance's change (about 700 in cash) stays out.
    assert later["stop_proceeds"]["shares"] == 0
    assert bil_bought(world) == 226


def test_nothing_without_a_stop_fill(world: World) -> None:
    at_close(world)
    Market(world.fake)
    decided(world)
    world.run(execute_close.make_job)
    at_close(world, NEXT)
    out = world.run(execute_close.make_job, session=NEXT)
    assert "stop_proceeds" not in out
    assert world.fake.position("BIL") == 0


def test_nothing_before_any_rebalance_finished(world: World) -> None:
    at_close(world, NEXT)
    out = world.run(execute_close.make_job, session=NEXT)
    assert "stop_proceeds" not in out


def test_the_kill_switch_keeps_them_in_cash_unless_the_cash_fund_is_held(world: World) -> None:
    stopped_out(world)
    world.set_flag("trading_enabled", False)
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["stop_proceeds"]["skipped"].startswith("increases blocked: kill switch on")
    assert world.fake.position("BIL") == 0
    assert any("the stop's proceeds stay in cash" in t for t in world.alerts.texts)
    # Already held, the cash fund may be added to.
    world.fake._book("BIL", Side.BUY, 1, 91.5)
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["stop_proceeds"]["shares"] == 225  # a booked share is no order: not counted


def test_dry_run_plans_and_buys_nothing(world: World) -> None:
    stopped_out(world)
    out = world.run(execute_close.make_job, session=NEXT, mode=TradingMode.DRY_RUN)
    assert out["stop_proceeds"]["dry_run"] is True
    assert out["stop_proceeds"]["shares"] == 225
    assert world.fake.position("BIL") == 0


def test_without_a_price_they_wait(world: World) -> None:
    stopped_out(world)
    world.fake.set_price("BIL", None)
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["stop_proceeds"]["skipped"] == "no price"
    assert any("no price for BIL" in t for t in world.alerts.texts)


def test_the_halt_sells_instead(world: World) -> None:
    stopped_out(world)
    world.set_flag("halt_drawdown", True)
    out = world.run(execute_close.make_job, session=NEXT)
    assert "stop_proceeds" not in out
    assert out.get("halt_sale") is None  # nothing left to sell
