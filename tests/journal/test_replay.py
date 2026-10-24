"""Sim R and the plan counterfactual (journal.replay), on the simulator's hand-built days."""

from dataclasses import replace

import pytest

from auratrade.config import ExitPlan, Strategy, TrailMA
from auratrade.journal.replay import counterfactual, sized_entry, with_defaults
from auratrade.sim.trade import TradeOutcome
from tests.sim.test_trade import RISK, approved, daily, entry_session, setup

VCP = Strategy.VCP_BREAKOUT


def test_sized_entry_matches_the_guardrails_sizing() -> None:
    entry = sized_entry(
        symbol="ABC",
        strategy=VCP,
        pivot=50.0,
        stop=47.0,
        code_pivot=50.0,
        adr=0.04,
        risk_fraction=0.01,
        equity=5000.0,
        risk=RISK,
    )
    assert entry == approved()


def test_sized_entry_under_one_share_or_one_tranche() -> None:
    kw = {
        "symbol": "ABC",
        "strategy": VCP,
        "pivot": 50.0,
        "stop": 47.0,
        "code_pivot": 50.0,
        "adr": 0.04,
        "equity": 5000.0,
        "risk": RISK,
    }
    assert sized_entry(risk_fraction=0.0001, **kw) is None  # type: ignore[arg-type]
    small = sized_entry(risk_fraction=0.0008, **kw)  # type: ignore[arg-type]
    assert small is not None
    assert (small.planned_shares, small.tranches) == (1, 1)


def test_with_defaults_keeps_the_levels_and_resets_plan_and_risk() -> None:
    chosen = ExitPlan(trail=TrailMA.TEN_DAY)
    decided = approved(exits=chosen, risk=0.005, risk_amount=25.0, planned_shares=6)
    default = with_defaults(
        decided, code_pivot=50.0, adr=0.04, default_risk=0.01, equity=5000.0, risk=RISK
    )
    assert default == approved()


def test_sim_r_and_default_plan_r_on_the_same_sessions() -> None:
    sessions = [entry_session(), daily(50.9, 51.5, 50.5, 51.2), daily(51.2, 51.3, 46.0, 46.5)]
    decided = approved(risk=0.005, risk_amount=25.0, planned_shares=6, tranches=2)
    result = counterfactual(
        setup(decided), sessions, default_risk=0.01, equity=5000.0, record=None, risk=RISK
    )
    assert result.sim.outcome is TradeOutcome.CLOSED
    assert result.default_plan is not None
    assert result.default_plan.order is not None
    assert result.default_plan.order.tranches.total == 12
    assert result.sim_r == pytest.approx(result.sim.r)
    assert result.default_plan_r == pytest.approx(result.default_plan.r)


def test_no_default_plan_without_adr_or_shares() -> None:
    sessions = [entry_session()]
    no_adr = counterfactual(
        replace(setup(), adr=None), sessions, default_risk=0.01, equity=5000, record=None, risk=RISK
    )
    assert no_adr.default_plan is None
    assert no_adr.default_plan_r is None
    tiny = counterfactual(
        setup(), sessions, default_risk=0.00001, equity=5000, record=None, risk=RISK
    )
    assert tiny.default_plan is None
    assert tiny.sim.outcome is TradeOutcome.OPEN
