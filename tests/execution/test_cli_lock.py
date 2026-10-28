"""The operator CLI and the order lock's waiting."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest
from sqlalchemy import delete, insert, select, update

from auratrade.cli import CliDeps, build_parser, main
from auratrade.config import Settings, TradingMode
from auratrade.db.flags import TRADING_ENABLED, read_flags
from auratrade.db.models import Mismatch, SystemFlag
from auratrade.execution import funds, ids
from auratrade.execution.broker import (
    LimitBuy,
    OrderStatus,
    OrderType,
    OtoBuy,
    Side,
    StopSell,
)
from auratrade.execution.lock import LockTimeoutError, OrderLock
from tests.execution.conftest import World

# --- the CLI ---------------------------------------------------------------------------------


def cli(
    world: World, mode: TradingMode = TradingMode.PAPER
) -> tuple[Callable[[], CliDeps], list[str]]:
    out: list[str] = []
    settings = Settings(_env_file=None, trading_mode=mode)  # type: ignore[call-arg]

    def factory() -> CliDeps:
        return CliDeps(
            settings=settings,
            risk=world.risk,
            engine=world.engine,
            broker=lambda: world.fake,
            lock=lambda: world.lock,
            out=out.append,
            prices=lambda: world.fake,
            sleep=world.fake.sleep,
        )

    return factory, out


def test_kill_on_blocks_increases_and_cancels_resting_buys(world: World) -> None:
    world.fake.set_price("SPY", 500.0)
    world.fake.submit(LimitBuy("SPY", 10, 400.0, "manual-rest"))
    factory, out = cli(world)
    assert main(["kill", "on"], deps_factory=factory) == 0
    with world.engine.connect() as conn:
        assert not read_flags(conn).trading_enabled
    assert all(
        o.status is OrderStatus.CANCELED for o in world.fake.all_orders() if o.side is Side.BUY
    )
    assert out[-1] == "cancelled 1 resting buy orders"
    main(["kill", "status"], deps_factory=factory)
    assert out[-1] == "kill switch ON (increases blocked)"
    main(["kill", "off"], deps_factory=factory)
    main(["kill", "status"], deps_factory=factory)
    assert out[-1] == "kill switch off (increases allowed)"


def test_kill_on_in_dry_run_only_sets_the_flag(world: World) -> None:
    factory, out = cli(world, TradingMode.DRY_RUN)
    main(["kill", "on"], deps_factory=factory)
    assert out[-1] == "TRADING_MODE is not paper: no orders to cancel"
    assert world.fake.calls == []


def test_kill_off_creates_a_missing_flag_row(world: World) -> None:
    with world.engine.begin() as conn:
        conn.execute(delete(SystemFlag).where(SystemFlag.name == TRADING_ENABLED))
    factory, _ = cli(world)
    main(["kill", "off"], deps_factory=factory)
    with world.engine.connect() as conn:
        assert read_flags(conn).trading_enabled


def test_mismatches_and_resolve(world: World) -> None:
    factory, out = cli(world)
    main(["mismatches"], deps_factory=factory)
    assert out[-1] == "no open mismatches"
    with world.engine.begin() as conn:
        mismatch_id = conn.execute(
            insert(Mismatch)
            .values(kind="CORPORATE_ACTION", symbol="DXJ", details={"ratio": 3})
            .returning(Mismatch.id)
        ).scalar_one()
    main(["mismatches"], deps_factory=factory)
    assert out[-1].startswith(f"{mismatch_id}  CORPORATE_ACTION  DXJ")
    assert main(["resolve", str(mismatch_id), "--note", "fixed"], deps_factory=factory) == 0
    assert main(["resolve", str(mismatch_id), "--note", "again"], deps_factory=factory) == 1
    with world.engine.connect() as conn:
        assert conn.execute(select(Mismatch.resolution_note)).scalar_one() == "fixed"


def test_halt_status_and_reset(world: World) -> None:
    factory, out = cli(world)
    main(["halt", "status"], deps_factory=factory)
    assert out[-1] == "drawdown halt off; no peak yet"
    world.set_flag("halt_drawdown", True)
    with world.engine.begin() as conn:
        conn.execute(
            update(SystemFlag)
            .where(SystemFlag.name == "halt_drawdown")
            .values(details={"peak": 140_000.0, "since": "2026-10-21"})
        )
    main(["halt", "status"], deps_factory=factory)
    assert out[-1] == "drawdown halt ON (no leverage); peak 140,000.00, on since 2026-10-21"
    assert main(["halt", "reset"], deps_factory=factory) == 0
    assert out[-1] == "drawdown halt off; its peak reset to the equity 100,000.00"
    with world.engine.connect() as conn:
        assert read_flags(conn).halt_drawdown is False
    with world.engine.begin() as conn:
        conn.execute(delete(SystemFlag).where(SystemFlag.name == "halt_drawdown"))
    main(["halt", "status"], deps_factory=factory)
    assert out[-1] == "drawdown halt: no flag row (reads as ON)"


def _bought(world: World, symbol: str, qty: int, price: float, *, stop: float | None) -> None:
    world.fake.set_price(symbol, price)
    session = date(2026, 10, 22)
    if stop is None:
        order = world.fake.submit(LimitBuy(symbol, qty, price, ids.buy_id(session, symbol)))
    else:
        order = world.fake.submit(OtoBuy(symbol, qty, price, stop, ids.buy_id(session, symbol)))
        with world.engine.begin() as conn:
            funds.write_stop_level(conn, symbol, stop, session=session, change="ENTRY")
    world.fake.fill(order.id, price=price)


def _fill_sales(world: World) -> None:
    world.fake.on_submit.append(lambda o: world.fake.fill(o.id) if o.side is Side.SELL else None)


def test_flatten_needs_confirm(world: World) -> None:
    factory, out = cli(world)
    assert main(["flatten"], deps_factory=factory) == 1
    assert "--confirm" in out[-1]
    with world.engine.connect() as conn:
        assert read_flags(conn).trading_enabled  # nothing changed


def test_flatten_sells_everything_and_blocks_buying_back(world: World) -> None:
    _bought(world, "SPY", 10, 500.0, stop=None)
    _bought(world, "SSO", 20, 100.0, stop=70.0)
    world.fake.submit(LimitBuy("GLD", 5, 180.0, "manual-rest"))
    world.fake.set_price("GLD", 200.0)
    _fill_sales(world)
    factory, out = cli(world)
    assert main(["flatten", "--confirm"], deps_factory=factory) == 0
    with world.engine.connect() as conn:
        assert not read_flags(conn).trading_enabled
    assert world.fake.positions() == []
    assert world.fake.open_orders() == []  # the stop, and the resting buy, are gone
    assert out[-1] == "flat"
    assert "SSO: sold 20 of 20 (FILLED)" in out
    assert "cancelled 1 resting buy orders" in out
    sales = [
        o for o in world.fake.all_orders() if o.side is Side.SELL and o.type is OrderType.LIMIT
    ]
    assert {o.symbol for o in sales} == {"SPY", "SSO"}


def test_flatten_restores_the_stop_when_a_sale_doesnt_fill(world: World) -> None:
    _bought(world, "SSO", 20, 100.0, stop=70.0)
    factory, out = cli(world)
    assert main(["flatten", "--confirm"], deps_factory=factory) == 1
    assert out[-1] == "20 shares still held: run flatten again"
    [stop] = [o for o in world.fake.open_orders() if o.type is OrderType.STOP]
    assert (stop.symbol, stop.qty, stop.stop_price) == ("SSO", 20, 70.0)


def test_flatten_restores_an_unrecorded_stop_from_the_price(world: World) -> None:
    world.fake.set_price("SSO", 100.0)
    order = world.fake.submit(LimitBuy("SSO", 20, 100.0, ids.buy_id(date(2026, 10, 22), "SSO")))
    world.fake.fill(order.id, price=100.0)
    world.fake.submit(StopSell("SSO", 20, 70.0, ids.stop_id(date(2026, 10, 22), "SSO")))
    factory, _ = cli(world)
    assert main(["flatten", "--confirm"], deps_factory=factory) == 1
    [stop] = [o for o in world.fake.open_orders() if o.type is OrderType.STOP]
    assert stop.stop_price == 70.0  # 30% below the price: where a buy today would put it


def test_flatten_waits_for_the_market(world: World) -> None:
    _bought(world, "SPY", 10, 500.0, stop=None)
    world.fake.close_market()
    factory, out = cli(world)
    assert main(["flatten", "--confirm"], deps_factory=factory) == 1
    assert out[-1].startswith("the market is closed")
    assert world.fake.position("SPY") == 10
    with world.engine.connect() as conn:
        assert not read_flags(conn).trading_enabled  # the kill switch is on anyway


def test_flatten_in_dry_run_only_sets_the_kill_switch(world: World) -> None:
    factory, out = cli(world, TradingMode.DRY_RUN)
    assert main(["flatten", "--confirm"], deps_factory=factory) == 0
    assert out[-1] == "TRADING_MODE is not paper: no orders sent"
    assert world.fake.calls == []


def test_the_parser_needs_a_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# --- the lock --------------------------------------------------------------------------------


def test_a_waiting_job_alerts_then_times_out(world: World) -> None:
    alerts: list[str] = []
    cfg = world.risk.execution.model_copy(
        update={"lock_wait_alert_seconds": 1, "lock_wait_max_seconds": 2}
    )
    waiter = OrderLock(world.url, cfg, alert=alerts.append, sleep=lambda _: None, poll_seconds=0.5)
    with world.lock.hold("holder"):
        with pytest.raises(LockTimeoutError, match="held by"), waiter.hold("waiter"):
            pass  # pragma: no cover - never entered
        with waiter.hold("try", wait=False) as held:
            assert held is False
    assert alerts == ["waiter has waited 1 s for the order lock"]
    with waiter.hold("after") as held:
        assert held is True
        assert waiter.holder() is not None
    assert waiter.holder() is None
