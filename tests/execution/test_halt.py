"""The live drawdown halt (README §7.3) on the test Postgres: its state, `post_market`'s
check at the close, and its turning off on R."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import Engine, delete, insert, select, update

from auratrade.config import TradingMode
from auratrade.db.flags import HALT_DRAWDOWN, read_flags
from auratrade.db.models import AccountSnapshot, SystemFlag
from auratrade.execution import halt
from auratrade.jobs import post_market
from auratrade.strategies.core_allocation import Halt
from tests.execution.conftest import TODAY, World

R = date(2026, 10, 30)


def stored(engine: Engine) -> tuple[bool, dict[str, object]]:
    with engine.connect() as conn:
        row = conn.execute(
            select(SystemFlag.value, SystemFlag.details).where(SystemFlag.name == HALT_DRAWDOWN)
        ).one()
    return bool(row.value), dict(row.details)


def put(engine: Engine, on: bool, peak: float | None) -> None:
    details = {} if peak is None else {"peak": peak}
    with engine.begin() as conn:
        conn.execute(
            update(SystemFlag)
            .where(SystemFlag.name == HALT_DRAWDOWN)
            .values(value=on, details=details)
        )


def snapshot(engine: Engine, day: date, equity: float) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(AccountSnapshot).values(
                session_date=day,
                equity=equity,
                cash=equity,
                long_market_value=0.0,
                peak_equity=equity,
                drawdown=0.0,
                open_positions=0,
            )
        )


# --- the state ---------------------------------------------------------------------------


def test_without_a_peak_the_halt_starts_at_the_equity(world: World) -> None:
    with world.engine.connect() as conn:
        assert halt.load(conn, 100_000.0) == Halt(False, 100_000.0)
    put(world.engine, True, None)
    with world.engine.connect() as conn:
        assert halt.load(conn, 90_000.0) == Halt(True, 90_000.0)


def test_a_missing_flag_row_reads_as_on(world: World) -> None:
    with world.engine.begin() as conn:
        conn.execute(delete(SystemFlag).where(SystemFlag.name == HALT_DRAWDOWN))
    with world.engine.connect() as conn:
        assert halt.load(conn, 100_000.0) == Halt(True, 100_000.0)
        # Saving writes the row back.
        halt.save(conn, Halt(False, 100_000.0), session=TODAY, by="test", since=None)
        assert read_flags(conn).halt_drawdown is False


def test_the_close_raises_the_peak_then_turns_the_halt_on(world: World) -> None:
    cfg = world.risk.portfolio
    with world.engine.begin() as conn:
        first = halt.check_at_close(conn, TODAY, 100_000.0, cfg, write=True)
        higher = halt.check_at_close(conn, TODAY, 120_000.0, cfg, write=True)
        above = halt.check_at_close(conn, TODAY, 90_001.0, cfg, write=True)
        on = halt.check_at_close(conn, R, 90_000.0, cfg, write=True)
        later = halt.check_at_close(conn, R, 80_000.0, cfg, write=True)
    assert (first["on"], first["peak"]) == (False, 100_000.0)
    assert (higher["on"], higher["peak"]) == (False, 120_000.0)
    assert above["on"] is False
    assert on == {
        "on": True,
        "turned_on": True,
        "peak": 120_000.0,
        "drawdown": 0.25,
        "written": True,
    }
    assert (later["on"], later["turned_on"]) == (True, False)
    value, details = stored(world.engine)
    assert value is True
    assert details == {"peak": 120_000.0, "session": R.isoformat(), "since": R.isoformat()}


def test_a_dry_run_check_writes_nothing(world: World) -> None:
    put(world.engine, False, 200_000.0)
    with world.engine.begin() as conn:
        out = halt.check_at_close(conn, TODAY, 100_000.0, world.risk.portfolio, write=False)
    assert (out["on"], out["turned_on"], out["written"]) == (True, True, False)
    assert stored(world.engine) == (False, {"peak": 200_000.0})


@pytest.mark.parametrize(
    ("equity", "flipped_up", "off", "peak"),
    [
        (100_000.0, False, False, 140_000.0),  # still below the old peak
        (100_000.0, True, True, 100_000.0),  # DOWN -> UP: the peak reset to equity
        (140_000.0, False, True, 140_000.0),  # back at the old peak
    ],
)
def test_r_turns_the_halt_off_on_a_flip_or_a_new_peak(
    world: World, equity: float, flipped_up: bool, off: bool, peak: float
) -> None:
    put(world.engine, True, 140_000.0)
    snapshot(world.engine, R, equity)
    with world.engine.begin() as conn:
        out = halt.check_on_ranking(conn, R, flipped_up, world.risk.portfolio, write=True)
    assert (out["on"], out["turned_off"], out["peak"]) == (not off, off, peak)
    value, details = stored(world.engine)
    assert value is not off
    assert details["peak"] == (peak if off else 140_000.0)


def test_r_without_a_snapshot_leaves_the_halt_on(world: World) -> None:
    put(world.engine, True, 140_000.0)
    with world.engine.begin() as conn:
        out = halt.check_on_ranking(conn, R, True, world.risk.portfolio, write=True)
    assert out == {"on": True, "turned_off": False, "skipped": f"no snapshot on {R}"}
    assert stored(world.engine)[0] is True


def test_a_dry_run_on_r_writes_nothing(world: World) -> None:
    put(world.engine, True, 140_000.0)
    snapshot(world.engine, R, 150_000.0)
    with world.engine.begin() as conn:
        out = halt.check_on_ranking(conn, R, False, world.risk.portfolio, write=False)
    assert (out["on"], out["turned_off"], out["written"]) == (False, True, False)
    assert stored(world.engine)[0] is True


def test_reset_turns_it_off_at_the_equity(world: World) -> None:
    put(world.engine, True, 140_000.0)
    with world.engine.begin() as conn:
        assert halt.reset(conn, TODAY, 100_000.0, by="cli") == Halt(False, 100_000.0)
    assert stored(world.engine) == (False, {"peak": 100_000.0, "session": TODAY.isoformat()})


# --- post_market --------------------------------------------------------------------------


def test_post_market_turns_the_halt_on_and_alerts(world: World) -> None:
    put(world.engine, False, 140_000.0)  # the fake account holds 100,000 in cash
    out = world.run(post_market.make_job)
    assert out["halt"]["turned_on"] is True
    assert stored(world.engine)[0] is True
    assert any("DRAWDOWN HALT ON" in t for t in world.alerts.texts)
    assert any("Drawdown halt ON" in t for t in world.alerts.texts)  # the daily report
    # The next evening it stays on without a second alert.
    world.alerts.texts.clear()
    again = world.run(post_market.make_job)
    assert (again["halt"]["on"], again["halt"]["turned_on"]) == (True, False)
    assert not any("DRAWDOWN HALT ON" in t for t in world.alerts.texts)


def test_post_market_in_dry_run_reports_the_check_only(world: World) -> None:
    put(world.engine, False, 140_000.0)
    out = world.run(post_market.make_job, mode=TradingMode.DRY_RUN)
    assert (out["halt"]["on"], out["halt"]["written"]) == (True, False)
    assert stored(world.engine) == (False, {"peak": 140_000.0})
    assert not any("DRAWDOWN HALT ON" in t for t in world.alerts.texts)


def test_post_market_tracks_the_peak(world: World) -> None:
    out = world.run(post_market.make_job)
    assert out["halt"] == {
        "on": False,
        "turned_on": False,
        "peak": 100_000.0,
        "drawdown": 0.0,
        "written": True,
    }
    assert stored(world.engine) == (False, {"peak": 100_000.0, "session": TODAY.isoformat()})
