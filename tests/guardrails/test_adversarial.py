"""Adversarial LLM output (execution plan 3.5b): whatever an entry says, nothing unsafe is
approved. Negative or inverted stops, huge risk, NaN prices, unknown symbols, pivots off the
code pivot or below the market."""

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from auratrade.config import Strategy
from auratrade.guardrails.evaluate import ApprovedEntry, Entry, evaluate
from auratrade.guardrails.model import Outcome
from tests.guardrails.factories import EQUITY, RISK, context, holding, setup

G = RISK.guardrails
SETUP = setup()
CTX = context(SETUP)

floats = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.floats(-10, 300),
    st.sampled_from([0.0, -0.0, 100.0, 94.0, 93.0, 103.0, 1e-12, 1e300]),
)


@settings(max_examples=500)
@given(
    symbol=st.sampled_from(["NEW", "UNKNOWN", ""]),
    strategy=st.sampled_from(list(Strategy)),
    pivot=floats,
    stop=st.one_of(st.none(), floats),
    risk=st.one_of(st.none(), floats),
    conviction=st.one_of(st.none(), st.integers(-5, 10)),
)
def test_no_unsafe_entry_is_approved(
    symbol: str,
    strategy: Strategy,
    pivot: float,
    stop: float | None,
    risk: float | None,
    conviction: int | None,
) -> None:
    action = Entry(symbol, strategy, pivot, stop, risk, conviction)
    decision = evaluate([action], CTX, RISK).decisions[0]
    a = decision.approved
    if decision.outcome is Outcome.REJECT:
        assert a is None
        return
    assert isinstance(a, ApprovedEntry)
    assert (a.symbol, a.strategy) == ("NEW", Strategy.VCP_BREAKOUT)
    assert stop is not None
    assert all(math.isfinite(v) for v in (a.pivot, a.stop, a.risk, a.planned_limit))
    assert abs(a.pivot / SETUP.pivot - 1) <= G.pivot_tolerance + 1e-9
    assert SETUP.structural_stop <= a.stop < a.pivot
    assert 0 < a.risk <= min(RISK.regimes.CONFIRMED_UPTREND.base_risk, G.max_risk_per_trade)
    assert a.planned_limit <= SETUP.pivot * (1 + G.max_chase_above_pivot) + 1e-9
    assert a.planned_limit - a.stop <= 0.08 * a.planned_limit + 1e-9
    assert a.planned_shares >= 1
    assert a.reserved_exposure <= G.max_position_size * EQUITY + 1e-6
    assert a.planned_shares * (a.planned_limit - a.stop) <= a.risk_amount + 1e-6


@given(
    n=st.integers(0, 8),
    risk=st.floats(0.0001, 1.0),
    multiplier=st.sampled_from([0.25, 0.5, 1.0, 1.25]),
)
def test_heat_never_passes_the_cap(n: int, risk: float, multiplier: float) -> None:
    held = tuple(holding(f"H{i}", seed=i, shares=1, stop=60.0) for i in range(n))  # $40 each
    ctx = context(holdings=held, multiplier=multiplier)
    decision = evaluate([Entry("NEW", Strategy.VCP_BREAKOUT, 100.0, 94.0, risk, 3)], ctx, RISK)
    a = decision.decisions[0].approved
    if isinstance(a, ApprovedEntry):
        assert 40 * n + a.risk_amount <= G.max_heat * EQUITY + 1e-6
        assert (
            a.risk_amount
            >= G.heat_min_room_of_planned * min(risk, 0.01 * multiplier) * EQUITY - 1e-6
        )
