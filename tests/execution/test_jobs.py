"""The skeleton's order jobs against the fake broker and the test Postgres (task 1.7)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import insert, select

from auratrade.config import TradingMode
from auratrade.db.flags import HALT_DAILY_LOSS, TRADING_ENABLED
from auratrade.db.models import (
    AccountSnapshot,
    CorporateAction,
    Mismatch,
    OrderRow,
    Position,
    PositionEventRow,
    PositionPlanRow,
    Signal,
    TradeJournalRow,
)
from auratrade.execution.broker import OrderStatus, OrderType, Side
from auratrade.execution.runtime import DepsFactory
from auratrade.execution.store import entities
from auratrade.jobs import eod_review, execute_entries, execute_open, post_market, stop_guard
from auratrade.jobs.base import JobSpec
from auratrade.plans.model import PositionPlan
from tests.execution.conftest import TODAY, World, ny
from tests.execution.helpers import protected


def signals(world: World) -> dict[str, tuple[str, str | None, int | None]]:
    with world.engine.connect() as conn:
        rows = conn.execute(
            select(Signal.symbol, Signal.status, Signal.status_reason, Signal.shares)
        )
        return {r.symbol: (r.status, r.status_reason, r.shares) for r in rows}


def entries(world: World) -> list:
    return [o for o in world.fake.all_orders() if o.side is Side.BUY]


def positions(world: World) -> dict[str, Position]:
    with world.engine.connect() as conn:
        return {p.symbol: p for p in entities(conn, select(Position))}


# --- execute_entries -----------------------------------------------------------------------


def test_entries_go_in_as_gtc_otos_sized_on_the_limit(world: World) -> None:
    world.signal("NVDA", rank=1, trigger=100.0, limit=102.0, stop=95.0)
    out = world.run(execute_entries.make_job)
    assert out["blocked"] == []
    assert len(out["entries"]) == 1
    (parent,) = entries(world)
    # 1% of $100,000 on a $7 width at the limit = 142 shares; the 25% cap allows 245.
    assert parent.qty == 142
    assert (parent.type, parent.stop_price, parent.limit_price) == (
        OrderType.STOP_LIMIT,
        100.0,
        102.0,
    )
    assert parent.client_order_id == "at-20261022-NVDA-e1-1"
    leg = next(o for o in world.fake.all_orders() if o.parent_id == parent.id)
    assert (leg.stop_price, leg.time_in_force.value) == (95.0, "gtc")
    assert signals(world)["NVDA"] == ("SUBMITTED", None, 142)


def test_a_price_past_the_trigger_enters_with_a_limit_buy_and_above_the_limit_skips(
    world: World,
) -> None:
    world.signal("NVDA", rank=1)
    world.signal("AMD", rank=2)
    world.fake.set_price("NVDA", 101.0)
    world.fake.set_price("AMD", 102.5)
    world.run(execute_entries.make_job)
    (parent,) = entries(world)
    assert (parent.symbol, parent.type, parent.stop_price) == ("NVDA", OrderType.LIMIT, None)
    status, reason, _ = signals(world)["AMD"]
    assert status == "SKIPPED"
    assert reason is not None
    assert "above the limit" in reason


def test_only_the_top_picks_go_in(world: World) -> None:
    for rank, symbol in enumerate(("NVDA", "AMD", "MSFT", "AAPL"), start=1):
        world.signal(symbol, rank=rank, risk_fraction=0.0025)
    world.run(execute_entries.make_job)
    assert sorted(o.symbol for o in entries(world)) == ["AMD", "MSFT", "NVDA"]
    assert signals(world)["AAPL"][:2] == ("SKIPPED", "picks or positions full")


def test_zero_shares_and_no_price_are_skipped(world: World) -> None:
    world.signal("NVDA", rank=1, risk_fraction=0.00001)
    world.signal("AMD", rank=2)
    world.fake.set_price("AMD", None)
    world.run(execute_entries.make_job)
    assert not entries(world)
    assert signals(world)["NVDA"][1] == "sized to 0 shares"
    assert signals(world)["AMD"][1] == "no decision price"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda w: w.set_flag(TRADING_ENABLED, False), "kill switch on"),
        (lambda w: w.set_flag(HALT_DAILY_LOSS, True), "loss or drawdown halt"),
        (lambda w: w.set_session(TODAY, calendar_check="DISAGREE"), "calendar cross-check"),
        (lambda w: w.set_session(TODAY, is_early_close=True), "early close"),
        (
            lambda w: w.set_session(date(2026, 10, 21), data_status="INCOMPLETE"),
            "data INCOMPLETE",
        ),
    ],
)
def test_entries_fail_closed(world: World, setup: object, reason: str) -> None:
    world.signal("NVDA")
    setup(world)  # type: ignore[operator]
    out = world.run(execute_entries.make_job)
    assert any(reason in r for r in out["blocked"])
    assert not entries(world)
    assert signals(world)["NVDA"][0] == "PENDING"


def test_an_account_mismatch_blocks_entries_and_a_symbol_one_skips_the_symbol(
    world: World,
) -> None:
    world.signal("NVDA", rank=1)
    world.signal("AMD", rank=2)
    with world.engine.begin() as conn:
        conn.execute(insert(Mismatch).values(kind="RECONCILIATION", symbol="NVDA"))
    world.run(execute_entries.make_job)
    assert [o.symbol for o in entries(world)] == ["AMD"]
    with world.engine.begin() as conn:
        conn.execute(insert(Mismatch).values(kind="RECONCILIATION", symbol=None))
    world.signal("MSFT", rank=3, session=TODAY)
    out = world.run(execute_entries.make_job)
    assert "an account-level mismatch is open" in out["blocked"]


def test_dry_run_places_nothing(world: World) -> None:
    world.signal("NVDA")
    out = world.run(execute_entries.make_job, mode=TradingMode.DRY_RUN)
    assert "dry_run" in out["blocked"]
    assert not entries(world)


def test_a_closed_market_places_nothing(world: World) -> None:
    world.signal("NVDA")
    world.fake.close_market()
    out = world.run(execute_entries.make_job)
    assert "market closed" in out["blocked"]


def test_a_rejected_entry_is_skipped_and_alerted(world: World) -> None:
    from auratrade.execution.broker import BrokerError

    world.signal("NVDA")
    world.fake.fail_next("submit", BrokerError("asset not tradable", status=422))
    world.run(execute_entries.make_job)
    assert signals(world)["NVDA"][0] == "SKIPPED"
    assert any("rejected" in t for t in world.alerts.texts)


def test_exposure_limits_the_size(world: World) -> None:
    world.fake.cash = 1000.0
    world.signal("NVDA", risk_fraction=0.01)
    world.run(execute_entries.make_job)
    # 1% of $1,000 / $7 = 1 share; the 25% position cap is 2 shares at $102.
    assert [o.qty for o in entries(world)] == [1]


# --- reconciliation ---------------------------------------------------------------------------


def _enter(world: World, symbol: str = "NVDA", *, fill: int | None = None) -> str:
    world.signal(symbol)
    world.run(execute_entries.make_job)
    parent = next(o for o in entries(world) if o.symbol == symbol)
    world.fake.fill(parent.id, fill, price=100.5)
    return parent.id


def test_a_fill_opens_a_position_with_its_plan(world: World) -> None:
    _enter(world)
    out = world.run(execute_open.make_job)
    assert out["opened"] == ["NVDA"]
    assert out["problems"] == {}
    pos = positions(world)["NVDA"]
    assert (pos.status, pos.shares, pos.entry_price, pos.initial_stop) == (
        "OPEN",
        142,
        100.5,
        95.0,
    )
    with world.engine.connect() as conn:
        plan = PositionPlan.model_validate(conn.execute(select(PositionPlanRow.plan)).scalar_one())
        mirrored = conn.execute(select(OrderRow.purpose, OrderRow.position_id)).all()
    assert (plan.hard_stop, plan.exits.trail.value, plan.entry.tranches) == (95.0, "EMA_21", 1)
    assert sorted(m.purpose for m in mirrored) == ["ENTRY", "STOP_LEG"]
    assert {m.position_id for m in mirrored} == {pos.id}
    assert signals(world)["NVDA"][0] == "FILLED"


def test_a_stop_fill_closes_the_position_with_a_journal_row(world: World) -> None:
    parent = _enter(world)
    world.run(execute_open.make_job)
    leg = next(o for o in world.fake.open_orders() if o.parent_id == parent)
    world.fake.fill(leg.id, price=94.9)
    out = world.run(post_market.make_job)
    assert out["closed"] == ["NVDA"]
    with world.engine.connect() as conn:
        (journal,) = entities(conn, select(TradeJournalRow))
        event = conn.execute(select(PositionEventRow.kind, PositionEventRow.rule)).one()
    assert journal.exit_reason == "HARD_STOP"
    assert journal.exit_price == 94.9
    assert journal.r_multiple == pytest.approx((94.9 - 100.5) / (100.5 - 95.0))
    assert journal.holding_sessions == 1
    assert (event.kind, event.rule) == ("EXIT", "HARD_STOP")
    assert positions(world)["NVDA"].status == "CLOSED"


def test_an_unknown_position_is_a_mismatch_and_left_alone(world: World) -> None:
    world.fake.fill(
        world.fake.submit(
            __import__("auratrade.execution.broker", fromlist=["OtoBuy"]).OtoBuy(
                "META", 5, 101.0, 90.0, "manual-buy"
            )
        ).id
    )
    out = world.run(execute_open.make_job)
    assert out["unknown"] == ["META"]
    with world.engine.connect() as conn:
        assert conn.execute(select(Mismatch.symbol)).scalar_one() == "META"
    world.run(execute_open.make_job)  # no second mismatch, no second alert
    assert sum("unknown position" in t for t in world.alerts.texts) == 1


def test_an_entry_cancelled_unfilled_ends_its_signal(world: World) -> None:
    world.signal("NVDA")
    world.run(execute_entries.make_job)
    world.clock.now = ny(TODAY, 15, 40)
    world.run(eod_review.make_job)
    world.run(post_market.make_job)  # the next reconciliation sees the cancelled entry
    assert signals(world)["NVDA"][0] == "CANCELED"


def test_a_split_scales_the_plan_and_opens_a_mismatch(world: World) -> None:
    _enter(world)
    world.run(execute_open.make_job)
    with world.engine.begin() as conn:
        conn.execute(
            insert(CorporateAction).values(
                ticker_id=world.tickers["NVDA"],
                symbol="NVDA",
                action_type="SPLIT",
                ex_date=date(2026, 10, 23),
                ratio=2.0,
                source="ALPACA",
                source_id="split-1",
            )
        )
    world.fake.split_ex_date("NVDA")
    world.fake.set_price("NVDA", 50.0)
    out = world.run(execute_open.make_job, session=date(2026, 10, 23))
    assert out["splits"] == ["NVDA"]
    stop = next(o for o in world.fake.open_orders())
    assert (stop.qty, stop.stop_price) == (142, 47.5)  # the broker's quantity, the scaled level
    assert world.fake.position("NVDA") == 142  # paper doesn't apply the split
    with world.engine.connect() as conn:
        assert conn.execute(select(Mismatch.kind)).scalar_one() == "CORPORATE_ACTION"
        versions = conn.execute(select(PositionPlanRow.change)).scalars().all()
    assert sorted(versions) == ["ENTRY", "SPLIT"]


# --- execute_open, eod_review, post_market ----------------------------------------------------


def test_before_the_open_a_stop_through_the_market_leaves_the_sale_owed(world: World) -> None:
    parent = _enter(world)
    world.run(execute_open.make_job)
    world.fake.delete(next(o.id for o in world.fake.open_orders() if o.parent_id == parent))
    world.fake.close_market()
    world.fake.set_price("NVDA", 94.0)
    out = world.run(execute_open.make_job)
    assert out["repaired"] == ["NVDA"]
    assert world.fake.position("NVDA") == 142
    assert positions(world)["NVDA"].exit_pending == "HARD_STOP"

    world.fake.open_market()
    world.fake.on_submit.append(
        lambda o: world.fake.fill(o.id, price=93.8) if o.side is Side.SELL else None
    )
    world.fake.set_price("NVDA", 94.0)
    out = world.run(execute_open.make_job)
    # The invariant finds the shares still unprotected and sells them at once; the owed-sale
    # retry after it finds nothing left.
    assert out["repaired"] == ["NVDA"]
    assert world.fake.position("NVDA") == 0
    world.run(post_market.make_job)
    assert positions(world)["NVDA"].exit_reason == "HARD_STOP"


def test_eod_cancels_unfilled_entries_and_exits_below_the_trail(world: World) -> None:
    world.bars("NVDA", [110.0] * 120)
    _enter(world)
    world.signal("AMD", rank=2)
    world.run(execute_entries.make_job)
    world.clock.now = ny(TODAY, 15, 40)
    world.fake.set_price("NVDA", 100.0)  # far below a 21 EMA of 110
    world.fake.on_submit.append(
        lambda o: world.fake.fill(o.id, price=99.9) if o.side is Side.SELL else None
    )
    out = world.run(eod_review.make_job)
    assert out["entries_cancelled"] == 1
    assert out["exits"][0]["decision"] == "exit"
    assert world.fake.position("NVDA") == 0
    world.run(post_market.make_job)
    assert positions(world)["NVDA"].exit_reason == "TRAIL"


def test_eod_holds_above_the_trail_and_waits_without_a_price(world: World) -> None:
    world.bars("NVDA", [90.0] * 120)
    world.bars("AMD", [90.0] * 120)
    _enter(world, "NVDA")
    _enter(world, "AMD")
    world.fake.set_price("NVDA", 100.0)
    world.fake.set_price("AMD", None)
    out = world.run(eod_review.make_job)
    decisions = {e["symbol"]: e["decision"] for e in out["exits"]}
    assert decisions == {"NVDA": "hold", "AMD": "wait: no decision price"}


def near_close_eod(deps_factory: DepsFactory) -> JobSpec:
    return eod_review.make_job(deps_factory, near_close_only=True)


@pytest.mark.parametrize(
    ("hour", "minute", "early", "runs"),
    [
        (15, 40, False, True),
        (12, 40, False, False),  # the half-day slot on a regular day
        (12, 40, True, True),
        (15, 40, True, False),  # the regular slot after a half day's close
        (16, 0, False, False),  # at the close is too late
        (15, 29, False, False),  # 31 minutes out
    ],
)
def test_the_scheduled_eod_review_runs_only_near_the_close(
    world: World, hour: int, minute: int, early: bool, runs: bool
) -> None:
    if early:
        world.set_session(TODAY, close_at=ny(TODAY, 13), is_early_close=True, minutes=210)
    world.clock.now = ny(TODAY, hour, minute)
    out = world.run(near_close_eod)
    assert ("skipped" not in out) is runs
    if not runs:
        assert out == {"skipped": "not within 30 min of the close"}


def test_the_scheduled_eod_review_skips_a_day_without_a_session(world: World) -> None:
    saturday = date(2026, 10, 24)
    world.clock.now = ny(saturday, 15, 40)
    assert "skipped" in world.run(near_close_eod, session=saturday)
    assert eod_review.JOB.name == "eod_review"


def test_an_unfilled_trail_exit_is_owed_and_retried_at_the_check_run(world: World) -> None:
    world.bars("NVDA", [110.0] * 120)
    _enter(world)
    world.fake.set_price("NVDA", 100.0)
    world.run(eod_review.make_job)
    assert positions(world)["NVDA"].exit_pending == "TRAIL"
    assert protected(world.fake)
    world.fake.on_submit.append(
        lambda o: world.fake.fill(o.id, price=100.0) if o.side is Side.SELL else None
    )
    world.run(execute_open.make_job, session=date(2026, 10, 23))
    assert world.fake.position("NVDA") == 0


def test_post_market_snapshot_report_backup_and_expiry(world: World) -> None:
    _enter(world)
    world.signal("AMD", rank=2)
    world.signal("MSFT", rank=3)
    world.run(execute_entries.make_job)
    world.clock.now = ny(TODAY, 16, 20)
    world.fake.close_market()
    out = world.run(post_market.make_job)
    assert out["entries_cancelled"] == 2
    assert out["backup_rows"]["positions"] == 1
    assert out["backup_rows"]["signals"] == 3
    assert world.storage.exists("backup/trade_journal/2026-10-22.parquet")
    report = next(t for t in world.alerts.texts if "daily report" in t)
    assert "NVDA 142 sh" in report
    assert "stop 95.00" in report
    with world.engine.connect() as conn:
        (snap,) = entities(conn, select(AccountSnapshot))
    assert snap.open_positions == 1
    assert snap.drawdown == 0.0


def test_post_market_dry_run_still_reports(world: World) -> None:
    out = world.run(post_market.make_job, mode=TradingMode.DRY_RUN)
    assert out["dry_run"] is True
    assert any("daily report" in t for t in world.alerts.texts)


def test_the_kill_switch_cancels_resting_entries_in_any_order_job(world: World) -> None:
    world.signal("NVDA")
    world.run(execute_entries.make_job)
    world.set_flag(TRADING_ENABLED, False)
    world.run(execute_open.make_job)
    assert all(o.status is OrderStatus.CANCELED for o in entries(world))


# --- stop_guard -----------------------------------------------------------------------------


def test_stop_guard_does_nothing_when_all_is_well(world: World) -> None:
    _enter(world)
    world.run(execute_open.make_job)
    calls = len(world.fake.calls)
    assert world.run(stop_guard.make_job) == {"ok": True}
    assert world.fake.calls[calls:] == ["is_market_open", "positions", "open_orders"]


def test_stop_guard_skips_a_closed_market(world: World) -> None:
    world.fake.close_market()
    assert world.run(stop_guard.make_job) == {"skipped": "market closed"}
