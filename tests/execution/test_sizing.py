"""Whole-share sizing and tranches (archived README §7.1, §8, #122)."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from auratrade.config import load_risk_config
from auratrade.execution.sizing import Tranches, planned_shares, split_tranches, tranche2_shares

ENTRIES = load_risk_config().entries


def size(**kw: float) -> int:
    args = {
        "risk_amount": 50.0,
        "limit": 102.0,
        "stop": 94.0,
        "max_position_value": 1250.0,
        "exposure_room": 5000.0,
    } | kw
    return planned_shares(**args)


def test_sized_on_the_worst_fill() -> None:
    # $50 at risk / (102 - 94) = 6.25 -> 6 shares.
    assert size() == 6


def test_clipped_by_position_size_and_exposure() -> None:
    assert size(stop=101.0) == 12  # 50 shares by risk, 1250 / 102 = 12 by value
    assert size(stop=101.0, exposure_room=500.0) == 4


def test_float_noise_does_not_lose_a_share() -> None:
    # 0.3 / 0.1 = 2.9999999999999996
    assert size(risk_amount=0.3, limit=1.1, stop=1.0) == 3


@pytest.mark.parametrize(
    "kw",
    [
        {"stop": 102.0},
        {"stop": 0.0},
        {"risk_amount": 0.0},
        {"limit": float("nan")},
        {"risk_amount": float("inf")},
        {"risk_amount": 5.0},  # under one share: 0, skipped
        {"exposure_room": -10.0},
    ],
)
def test_zero_shares(kw: dict[str, float]) -> None:
    assert size(**kw) == 0


def test_split_tranches() -> None:
    assert split_tranches(6, 2, ENTRIES) == Tranches(3, 3)
    assert split_tranches(5, 2, ENTRIES) == Tranches(2, 3)
    assert split_tranches(2, 2, ENTRIES) == Tranches(1, 1)
    assert split_tranches(1, 2, ENTRIES) == Tranches(1, 0)  # under 2 shares: one tranche
    assert split_tranches(7, 1, ENTRIES) == Tranches(7, 0)
    assert split_tranches(0, 2, ENTRIES).total == 0
    with pytest.raises(ValueError, match="shares"):
        split_tranches(-1, 2, ENTRIES)


def t2(**kw: float) -> int:
    args = {
        "planned_risk": 60.0,
        "planned_shares": 10,
        "filled_shares": 5,
        "fill_price": 100.0,
        "limit": 103.0,
        "stop": 94.0,
    } | kw
    return tranche2_shares(**args)  # type: ignore[arg-type]


def test_tranche2_resized_on_the_actual_price() -> None:
    # Tranche 1: 5 x (100 - 94) = 30 at risk; 30 left / (103 - 94) = 3.33 -> 3.
    assert t2() == 3
    # A cheaper tranche 2 is capped by the planned size: 30 / 1 = 30 -> 5 left.
    assert t2(limit=95.0) == 5


def test_tranche2_after_a_partial_fill() -> None:
    # Tranche 1 filled 2 of 5: 12 at risk, 48 left / 9 = 5.3 -> 5; 8 left of the plan.
    assert t2(filled_shares=2) == 5


@pytest.mark.parametrize(
    "kw",
    [
        {"fill_price": 106.0},  # tranche 1 already used the whole risk
        {"limit": 94.0},
        {"stop": 0.0},
        {"planned_risk": float("nan")},
        {"filled_shares": -1},
        {"filled_shares": 10},
    ],
)
def test_no_tranche2(kw: dict[str, float]) -> None:
    assert t2(**kw) == 0


@given(
    planned_risk=st.floats(1, 1000),
    planned=st.integers(1, 500),
    filled=st.integers(0, 500),
    fill=st.floats(10, 200),
    limit=st.floats(10, 200),
    width=st.floats(0.01, 0.3),
)
def test_tranche2_never_exceeds_the_plan(
    planned_risk: float, planned: int, filled: int, fill: float, limit: float, width: float
) -> None:
    stop = min(fill, limit) * (1 - width)
    shares = tranche2_shares(
        planned_risk=planned_risk,
        planned_shares=planned,
        filled_shares=filled,
        fill_price=fill,
        limit=limit,
        stop=stop,
    )
    assert shares >= 0
    assert filled + shares <= max(planned, filled)
    total_risk = filled * (fill - stop) + shares * (limit - stop)
    assert shares == 0 or total_risk <= planned_risk + 1e-6
    assert not math.isnan(total_risk)
