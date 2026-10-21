"""The operator CLI and the order lock's waiting."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import insert, select

from auratrade.cli import CliDeps, build_parser, main
from auratrade.config import Settings, TradingMode
from auratrade.db.flags import TRADING_ENABLED, read_flags
from auratrade.db.models import Mismatch
from auratrade.execution.broker import OrderStatus, Side
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
        )

    return factory, out


def test_kill_on_blocks_entries_and_cancels_resting_ones(world: World) -> None:
    from auratrade.jobs import execute_entries

    world.signal("NVDA")
    world.run(execute_entries.make_job)
    factory, out = cli(world)
    assert main(["kill", "on"], deps_factory=factory) == 0
    with world.engine.connect() as conn:
        assert not read_flags(conn).trading_enabled
    assert all(
        o.status is OrderStatus.CANCELED for o in world.fake.all_orders() if o.side is Side.BUY
    )
    assert out[-1] == "cancelled 1 resting entry orders"
    main(["kill", "status"], deps_factory=factory)
    assert "ON (entries blocked)" in out[-1]
    main(["kill", "off"], deps_factory=factory)
    main(["kill", "status"], deps_factory=factory)
    assert "off (entries allowed)" in out[-1]


def test_kill_on_in_dry_run_only_sets_the_flag(world: World) -> None:
    factory, out = cli(world, TradingMode.DRY_RUN)
    main(["kill", "on"], deps_factory=factory)
    assert out[-1] == "TRADING_MODE is not paper: no orders to cancel"
    assert world.fake.calls == []


def test_kill_off_creates_a_missing_flag_row(world: World) -> None:
    from sqlalchemy import delete

    from auratrade.db.models import SystemFlag

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
