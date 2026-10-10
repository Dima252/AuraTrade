from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError

from auratrade.config import Strategy, TrailMA, load_risk_config
from auratrade.db.models import (
    HOLD_STATES,
    PLAN_CHANGES,
    PLAN_RULES,
    POSITION_EVENT_KINDS,
    STRATEGIES,
    TRAILS,
    PositionEventRow,
    PositionPlanRow,
    Ticker,
)
from auratrade.plans.entry import new_plan
from auratrade.plans.levels import plan_levels
from auratrade.plans.model import EventKind, HoldState, PlanChange, PositionPlan, Rule

RISK = load_risk_config()


def test_database_values_match_the_code() -> None:
    assert tuple(Strategy) == STRATEGIES
    assert tuple(TrailMA) == TRAILS
    assert tuple(HoldState) == HOLD_STATES
    assert tuple(PlanChange) == PLAN_CHANGES
    assert tuple(Rule) == PLAN_RULES
    assert tuple(EventKind) == POSITION_EVENT_KINDS


def plan_row(ticker_id: int, plan: PositionPlan, **overrides: Any) -> dict[str, Any]:
    levels = plan_levels(plan, dict.fromkeys(TrailMA, 90.0), RISK.plans)
    values = {
        "position_id": 1,
        "version": plan.version,
        "ticker_id": ticker_id,
        "strategy": plan.strategy.value,
        "change": plan.change.value,
        "session_date": date(2026, 10, 9),
        "pivot": plan.pivot,
        "entry_price": plan.entry_price,
        "initial_stop": plan.initial_stop,
        "hard_stop": plan.hard_stop,
        "trail": plan.trail.value,
        "hold_state": plan.hold.value,
        "hold_until_session": plan.hold_until_session,
        "plan": plan.model_dump(mode="json"),
        "levels": asdict(levels),
        "risk_hash": RISK.config_hash,
    }
    return values | overrides


def event_row(ticker_id: int, /, **overrides: Any) -> dict[str, Any]:
    values = {
        "position_id": 1,
        "ticker_id": ticker_id,
        "session_date": date(2026, 10, 9),
        "plan_version": 1,
        "kind": EventKind.SALE.value,
        "rule": Rule.CLIMAX.value,
        "shares": 50,
        "details": {"signals": 2},
    }
    return values | overrides


PLAN = new_plan(
    Strategy.EMERGING_LEADER, pivot=50.0, entry_price=50.5, initial_stop=46.5, config=RISK.plans
)


@pytest.fixture
def ticker_id(pg_engine: Engine) -> int:
    with pg_engine.begin() as conn:
        return int(
            conn.execute(insert(Ticker).values(symbol="ANET").returning(Ticker.id)).scalar_one()
        )


Rejects = Callable[[dict[str, Any]], None]


@pytest.fixture
def rejects_plan(pg_engine: Engine) -> Rejects:
    def check(values: dict[str, Any]) -> None:
        with pytest.raises(IntegrityError), pg_engine.begin() as conn:
            conn.execute(insert(PositionPlanRow).values(**values))

    return check


def test_plan_round_trip(pg_engine: Engine, ticker_id: int) -> None:
    with pg_engine.begin() as conn:
        conn.execute(insert(PositionPlanRow).values(**plan_row(ticker_id, PLAN)))
        conn.execute(insert(PositionEventRow).values(**event_row(ticker_id)))
        stored = conn.execute(select(PositionPlanRow.plan, PositionPlanRow.levels)).one()
        event = conn.execute(
            select(PositionEventRow.kind, PositionEventRow.rule, PositionEventRow.shares)
        ).one()
    assert PositionPlan.model_validate(stored.plan) == PLAN
    assert stored.levels["trail"] == "SMA_50"
    assert stored.levels["round_trip_trigger"] == pytest.approx(55.55)
    assert (event.kind, event.rule, event.shares) == ("SALE", "CLIMAX", 50)


def test_one_row_per_plan_version(pg_engine: Engine, ticker_id: int, rejects_plan: Rejects) -> None:
    with pg_engine.begin() as conn:
        conn.execute(insert(PositionPlanRow).values(**plan_row(ticker_id, PLAN)))
    rejects_plan(plan_row(ticker_id, PLAN))


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 0},
        {"strategy": "SHORT_SQUEEZE"},
        {"change": "LOOSEN"},
        {"trail": "SMA_200"},
        {"hold_state": "PAUSED"},
        {"initial_stop": 51.0},
        {"hard_stop": 46.0},
        {"pivot": 0.0},
        {"hold_state": "ACTIVE"},  # without hold_until_session
        {"hold_until_session": 40},  # while not active
    ],
)
def test_bad_plan_rows_are_rejected(
    ticker_id: int, rejects_plan: Rejects, overrides: dict[str, Any]
) -> None:
    rejects_plan(plan_row(ticker_id, PLAN, **overrides))


@pytest.mark.parametrize(
    "overrides", [{"kind": "PANIC"}, {"rule": "GUT_FEEL"}, {"shares": 0}, {"ticker_id": 999_999}]
)
def test_bad_event_rows_are_rejected(
    pg_engine: Engine, ticker_id: int, overrides: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError), pg_engine.begin() as conn:
        conn.execute(insert(PositionEventRow).values(**event_row(ticker_id, **overrides)))


def test_events_without_a_rule_or_shares(pg_engine: Engine, ticker_id: int) -> None:
    with pg_engine.begin() as conn:
        conn.execute(
            insert(PositionEventRow).values(
                **event_row(ticker_id, kind="FALTERING_RS", rule=None, shares=None)
            )
        )
