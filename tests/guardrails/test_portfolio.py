from __future__ import annotations

import math
from collections.abc import Mapping

import pytest

from auratrade.config import load_risk_config
from auratrade.guardrails.model import GuardrailRule, Outcome
from auratrade.guardrails.portfolio import (
    EnforcedTargets,
    PortfolioContext,
    PortfolioDecision,
    evaluate_decision,
)
from auratrade.strategies.core_allocation import Trend

CFG = load_risk_config().portfolio


def run(
    shift: float = 0.0,
    satellite: Mapping[str, float] | None = None,
    *,
    state: Trend = Trend.UP,
    capped: bool = False,
    halted: bool = False,
    increases: bool = True,
    current: Mapping[str, float] | None = None,
) -> EnforcedTargets:
    ctx = PortfolioContext(state, capped, halted, increases, current or {})
    return evaluate_decision(PortfolioDecision(shift, satellite or {}), ctx, CFG)


def rules(result: EnforcedTargets) -> list[GuardrailRule]:
    return [f.rule for f in result.findings]


def test_a_decision_inside_the_limits_passes_and_fills_the_satellite_with_cash() -> None:
    got = run(0.1, {"QQQ": 0.1, "GLD": 0.05})
    assert got.outcome is Outcome.APPROVE
    assert (got.rule_level, got.core_level) == (1.3, pytest.approx(1.4))
    assert got.satellite == pytest.approx({"QQQ": 0.1, "GLD": 0.05, "BIL": 0.05})
    assert got.targets == pytest.approx(
        {"SPY": 0.8 * 0.6, "SSO": 0.8 * 0.4, "QQQ": 0.1, "GLD": 0.05, "BIL": 0.05}
    )
    assert math.fsum(got.targets.values()) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("shift", "state", "capped", "halted", "level"),
    [
        (0.3, Trend.UP, False, False, 1.5),  # above the shift
        (0.1, Trend.DOWN, False, False, 0.5),  # upward while DOWN
        (-0.25, Trend.DOWN, False, False, 0.3),  # below min_level
        (0.2, Trend.UP, True, False, 1.0),  # upward while capped
        (0.05, Trend.UP, False, True, 1.0),  # upward while halted
    ],
)
def test_the_core_is_clipped_into_the_band(
    shift: float, state: Trend, capped: bool, halted: bool, level: float
) -> None:
    got = run(shift, state=state, capped=capped, halted=halted)
    assert got.core_level == pytest.approx(level)
    assert rules(got) == [GuardrailRule.CORE_SHIFT]
    assert got.outcome is Outcome.CLIP


def test_a_shift_that_isnt_a_number_is_rejected() -> None:
    got = run(float("nan"))
    assert got.core_level == 1.3
    assert rules(got) == [GuardrailRule.INVALID_INPUT]
    assert got.outcome is Outcome.REJECT


def test_the_satellite_list_and_weights() -> None:
    got = run(
        0.0,
        {"SSO": 0.05, "XYZ": 0.05, "QQQ": -0.01, "GLD": float("inf"), "IEF": 0.15, "VEA": 0.0},
    )
    assert rules(got) == [
        GuardrailRule.INVALID_INPUT,  # GLD
        GuardrailRule.SATELLITE_WEIGHT,  # IEF
        GuardrailRule.INVALID_INPUT,  # QQQ
        GuardrailRule.SATELLITE_LIST,  # SSO: leveraged, never in the satellite
        GuardrailRule.SATELLITE_LIST,  # XYZ
    ]
    assert got.satellite == pytest.approx({"IEF": 0.10, "BIL": 0.10})
    assert got.outcome is Outcome.REJECT


def test_a_satellite_over_its_share_is_scaled_down() -> None:
    got = run(0.0, {"QQQ": 0.1, "GLD": 0.1, "IEF": 0.1})
    assert rules(got) == [GuardrailRule.SATELLITE_TOTAL]
    assert got.satellite == pytest.approx({"QQQ": 0.2 / 3, "GLD": 0.2 / 3, "IEF": 0.2 / 3})
    assert "BIL" not in got.satellite


def test_with_increases_blocked_nothing_rises_but_cash() -> None:
    got = run(0.0, {"QQQ": 0.1, "BIL": 0.1}, increases=False, current={"SPY": 0.9, "BIL": 0.1})
    # SPY falls to 0.56 (allowed); SSO and QQQ would rise from nothing: held at 0.
    assert rules(got) == [GuardrailRule.NO_INCREASE, GuardrailRule.NO_INCREASE]
    assert [f.message.split()[0] for f in got.findings] == ["QQQ", "SSO"]
    assert got.targets == pytest.approx({"SPY": 0.56, "BIL": 0.44})
    assert math.fsum(got.targets.values()) == pytest.approx(1.0)
    assert got.outcome is Outcome.CLIP


def test_with_increases_blocked_a_fund_at_its_weight_is_kept() -> None:
    got = run(0.0, {}, state=Trend.DOWN, increases=False, current={"SPY": 0.4, "BIL": 0.6})
    # DOWN: SPY 0.8 x 0.5 = 0.40, unchanged.
    assert got.findings == ()
    assert got.targets == pytest.approx({"SPY": 0.4, "BIL": 0.6})
