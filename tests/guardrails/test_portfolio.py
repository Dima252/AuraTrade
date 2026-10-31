from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date

import pytest

from auratrade.config import load_risk_config
from auratrade.guardrails.model import GuardrailRule, Outcome
from auratrade.guardrails.portfolio import (
    EnforcedTargets,
    PortfolioContext,
    PortfolioDecision,
    evaluate_decision,
)
from auratrade.strategies.core_allocation import Trend, twin_targets
from auratrade.tax.israel import Lot

CFG = load_risk_config().portfolio
EQUITY = 100_000.0
OPENED = date(2025, 1, 2)


def run(
    shift: float = 0.0,
    satellite: Mapping[str, float] | None = None,
    *,
    state: Trend = Trend.UP,
    capped: bool = False,
    halted: bool = False,
    increases: bool = True,
    current: Mapping[str, float] | None = None,
    previous: Mapping[str, float] | None = None,
    lots: Mapping[str, Sequence[Lot]] | None = None,
    prices: Mapping[str, float] | None = None,
    tax_this_year: float = 0.0,
) -> EnforcedTargets:
    ctx = PortfolioContext(
        state,
        capped,
        halted,
        increases,
        current or {},
        previous or {},
        EQUITY,
        lots or {},
        prices or {},
        tax_this_year,
    )
    return evaluate_decision(PortfolioDecision(shift, satellite or {}), ctx, CFG)


def rules(result: EnforcedTargets) -> list[GuardrailRule]:
    return [f.rule for f in result.findings]


# --- the level and the core (#4) ------------------------------------------------------------


def test_a_decision_inside_the_limits_passes_and_fills_the_satellite_with_cash() -> None:
    got = run(0.1, {"QQQ": 0.1, "GLD": 0.05})
    assert got.outcome is Outcome.APPROVE
    assert (got.rule_level, got.level) == (1.3, pytest.approx(1.4))
    assert got.core_level == pytest.approx(1.5)  # (1.4 - 0.2) / 0.8
    assert got.satellite == pytest.approx({"QQQ": 0.1, "GLD": 0.05, "BIL": 0.05})
    assert got.targets == pytest.approx(
        {"SPY": 0.8 * 0.5, "SSO": 0.8 * 0.5, "QQQ": 0.1, "GLD": 0.05, "BIL": 0.05}
    )
    assert math.fsum(got.targets.values()) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("state", "capped", "halted"),
    [
        (Trend.UP, False, False),
        (Trend.DOWN, False, False),
        (Trend.UP, True, False),
        (Trend.DOWN, False, True),
    ],
)
def test_no_change_at_neutral_is_the_twin(state: Trend, capped: bool, halted: bool) -> None:
    got = run(0.0, {"SPY": 0.2}, state=state, capped=capped, halted=halted)
    assert got.findings == ()
    assert got.targets == pytest.approx(twin_targets(got.rule_level, CFG))


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
def test_the_level_is_clipped_into_the_band(
    shift: float, state: Trend, capped: bool, halted: bool, level: float
) -> None:
    got = run(shift, state=state, capped=capped, halted=halted)
    assert got.level == pytest.approx(level)
    assert got.core_level == pytest.approx((level - 0.2) / 0.8)
    assert rules(got) == [GuardrailRule.CORE_SHIFT]
    assert got.outcome is Outcome.CLIP


def test_a_shift_that_isnt_a_number_is_rejected() -> None:
    got = run(float("nan"))
    assert got.level == 1.3
    assert rules(got) == [GuardrailRule.INVALID_INPUT]
    assert got.outcome is Outcome.REJECT


# --- the satellite ----------------------------------------------------------------------------


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


def test_the_market_fund_may_fill_the_satellite_and_no_more() -> None:
    assert run(0.0, {"SPY": 0.2}).satellite == pytest.approx({"SPY": 0.2})
    got = run(0.0, {"SPY": 0.25})
    assert rules(got) == [GuardrailRule.SATELLITE_WEIGHT]
    assert got.satellite == pytest.approx({"SPY": 0.2})


def test_a_satellite_over_its_share_is_scaled_down() -> None:
    got = run(0.0, {"QQQ": 0.1, "GLD": 0.1, "IEF": 0.1})
    assert rules(got) == [GuardrailRule.SATELLITE_TOTAL]
    assert got.satellite == pytest.approx({"QQQ": 0.2 / 3, "GLD": 0.2 / 3, "IEF": 0.2 / 3})
    assert "BIL" not in got.satellite


# --- hold by default (#4) -------------------------------------------------------------------


def test_small_changes_keep_last_months_targets() -> None:
    previous = {"GLD": 0.10, "QQQ": 0.08, "BIL": 0.02}
    got = run(0.0, {"GLD": 0.09, "QQQ": 0.09}, previous=previous)
    assert rules(got) == [GuardrailRule.SATELLITE_HOLD, GuardrailRule.SATELLITE_HOLD]
    assert got.satellite == pytest.approx(previous)
    assert got.outcome is Outcome.CLIP


def test_a_small_new_holding_isnt_opened_but_a_small_exit_is_honoured() -> None:
    got = run(0.0, {"GLD": 0.015, "QQQ": 0.1}, state=Trend.DOWN, previous={"VEA": 0.01})
    assert rules(got) == [GuardrailRule.SATELLITE_HOLD]  # GLD, not opened
    assert got.satellite == pytest.approx({"QQQ": 0.1, "BIL": 0.1})  # VEA sold


def test_a_change_of_two_points_or_more_is_traded() -> None:
    previous = {"GLD": 0.10, "QQQ": 0.10}
    got = run(0.0, {"GLD": 0.08, "QQQ": 0.10}, state=Trend.DOWN, previous=previous)
    assert got.findings == ()
    assert got.satellite == pytest.approx({"GLD": 0.08, "QQQ": 0.10, "BIL": 0.02})


def test_holding_gives_back_the_excess_from_the_changed_funds() -> None:
    # GLD held at 0.10 (0.085 is within 2 points); IEF and QQQ give back the 0.015 over.
    got = run(
        0.0,
        {"GLD": 0.085, "IEF": 0.10, "QQQ": 0.015},
        state=Trend.DOWN,
        previous={"GLD": 0.10, "QQQ": 0.10},
    )
    assert rules(got) == [GuardrailRule.SATELLITE_HOLD]
    scale = 0.1 / 0.115
    assert got.satellite == pytest.approx({"GLD": 0.10, "IEF": 0.1 * scale, "QQQ": 0.015 * scale})


# --- the tax budget (#4) --------------------------------------------------------------------

PREVIOUS = {"QQQ": 0.10, "GLD": 0.10}
SWAP = {"GLD": 0.10, "IEF": 0.10}  # sells all $10,000 of QQQ
QQQ_LOTS = {"QQQ": [Lot(OPENED, 20.0, 5_000.0)]}  # 20 units at $500 now: a $5,000 gain
PRICES = {"QQQ": 500.0, "GLD": 200.0, "IEF": 100.0}


def test_a_sale_within_the_budget_passes() -> None:
    lots = {"QQQ": [Lot(OPENED, 20.0, 9_500.0)]}  # $500 gain, $125 tax of $200
    got = run(0.0, SWAP, previous=PREVIOUS, lots=lots, prices=PRICES)
    assert got.findings == ()
    assert got.satellite == pytest.approx(SWAP)


@pytest.mark.parametrize(("used", "step"), [(0.0, 0.16), (100.0, 0.08), (500.0, 0.0)])
def test_a_sale_over_the_budget_moves_back_toward_holding(used: float, step: float) -> None:
    # The whole sale: $1,250 tax. The budget: 0.2% of $100,000 = $200 a year, less what's used.
    got = run(0.0, SWAP, previous=PREVIOUS, lots=QQQ_LOTS, prices=PRICES, tax_this_year=used)
    assert rules(got) == [GuardrailRule.TAX_BUDGET]
    assert got.findings[0].proposed == 1250.0
    assert got.findings[0].enforced == max(200.0 - used, 0.0)
    assert got.satellite["QQQ"] == pytest.approx(0.10 - step * 0.10, abs=1e-9)
    assert got.satellite.get("IEF", 0.0) == pytest.approx(step * 0.10, abs=1e-9)
    assert got.satellite["GLD"] == pytest.approx(0.10)
    assert math.fsum(got.satellite.values()) == pytest.approx(0.2)


def test_losses_offset_gains_in_the_budget() -> None:
    lots = {**QQQ_LOTS, "GLD": [Lot(OPENED, 50.0, 15_000.0)]}  # GLD: a $5,000 loss
    got = run(0.0, {"IEF": 0.10, "BIL": 0.10}, previous=PREVIOUS, lots=lots, prices=PRICES)
    assert got.findings == ()


def test_a_fund_without_a_price_counts_its_whole_sale_as_gain() -> None:
    lots = {"QQQ": [Lot(OPENED, 20.0, 10_000.0)]}  # no gain at $500, but no price given
    got = run(0.0, SWAP, previous=PREVIOUS, lots=lots, prices={"GLD": 200.0})
    assert rules(got) == [GuardrailRule.TAX_BUDGET]
    assert got.findings[0].proposed == 2500.0


@pytest.mark.parametrize(
    ("state", "capped", "halted"),
    [(Trend.DOWN, False, False), (Trend.UP, True, False), (Trend.UP, False, True)],
)
def test_the_budget_never_holds_back_a_sale_in_a_risk_state(
    state: Trend, capped: bool, halted: bool
) -> None:
    got = run(
        0.0,
        SWAP,
        state=state,
        capped=capped,
        halted=halted,
        previous=PREVIOUS,
        lots=QQQ_LOTS,
        prices=PRICES,
    )
    assert got.findings == ()
    assert got.satellite == pytest.approx(SWAP)


# --- no increase ------------------------------------------------------------------------------


def test_with_increases_blocked_nothing_rises_but_cash() -> None:
    got = run(0.0, {"QQQ": 0.1, "BIL": 0.1}, increases=False, current={"SPY": 0.9, "BIL": 0.1})
    # The core at 1.375: SPY 0.5 (a fall, allowed); SSO and QQQ would rise from nothing.
    assert rules(got) == [GuardrailRule.NO_INCREASE, GuardrailRule.NO_INCREASE]
    assert [f.message.split()[0] for f in got.findings] == ["QQQ", "SSO"]
    assert got.targets == pytest.approx({"SPY": 0.5, "BIL": 0.5})
    assert math.fsum(got.targets.values()) == pytest.approx(1.0)
    assert got.outcome is Outcome.CLIP


def test_with_increases_blocked_a_fund_at_its_weight_is_kept() -> None:
    got = run(0.0, {}, state=Trend.DOWN, increases=False, current={"SPY": 0.3, "BIL": 0.7})
    # DOWN, the satellite in cash: the core at 0.375 holds SPY 0.8 x 0.375 = 0.30, unchanged.
    assert got.findings == ()
    assert got.targets == pytest.approx({"SPY": 0.3, "BIL": 0.7})
