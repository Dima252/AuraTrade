"""The 09:36 check of an approved entry (archived README §7.1, §8)."""

from dataclasses import replace

import pytest

from auratrade.config import RiskConfig, StopKind, Strategy
from auratrade.execution.sizing import Tranches
from auratrade.execution.triggers import OpeningRange, OrderKind
from auratrade.guardrails.evaluate import ApprovedEntry, evaluate
from auratrade.guardrails.levels import StrategyRecord
from auratrade.guardrails.model import GuardrailRule, Outcome
from auratrade.guardrails.orders import (
    MarketAtDecision,
    MarketAtOpen,
    OrderCheck,
    check_close,
    check_order,
)
from tests.guardrails.factories import EQUITY, RISK, context, entry, setup


def approved_vcp() -> ApprovedEntry:
    a = evaluate([entry()], context(), RISK).decisions[0].approved
    assert isinstance(a, ApprovedEntry)
    return a


def market(**kw: object) -> MarketAtOpen:
    defaults = MarketAtOpen(
        opening=OpeningRange(99.8, 98.5),
        price=99.9,
        low_of_day=98.5,
        code_pivot=100.0,
        atr=2.5,
        adr=0.04,
    )
    return replace(defaults, **kw)  # type: ignore[arg-type]


def check(
    a: ApprovedEntry | None = None, record: StrategyRecord | None = None, **kw: object
) -> OrderCheck:
    return check_order(a or approved_vcp(), market(**kw), equity=EQUITY, record=record, risk=RISK)


def rule(result: OrderCheck) -> GuardrailRule:
    assert result.plan is None
    assert result.outcome is Outcome.REJECT
    return result.findings[0].rule


def test_below_the_pivot_is_a_stop_limit_in_two_tranches() -> None:
    result = check()
    assert result.outcome is Outcome.APPROVE
    plan = result.plan
    assert plan is not None
    assert (plan.kind, plan.trigger, plan.limit, plan.stop) == (
        OrderKind.STOP_LIMIT,
        100.0,
        102.0,
        94.0,
    )
    assert plan.tranches == Tranches(6, 6)


def test_through_the_trigger_is_a_limit_buy_at_the_limit() -> None:
    plan = check(price=101.5).plan
    assert plan is not None
    assert plan.kind is OrderKind.LIMIT


def test_range_above_the_pivot_moves_the_trigger_and_rechecks_the_width() -> None:
    """Trigger 103 -> limit 105 (the code-pivot cap): the stop at 94 is 11 below it, > 8.4."""
    assert rule(check(opening=OpeningRange(103.0, 99.0), price=102.5)) is GuardrailRule.STOP_WIDTH


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"opening": OpeningRange(105.5, 100.0)}, GuardrailRule.MAX_CHASE),
        ({"price": 102.5}, GuardrailRule.PRICE_LEVELS),  # above the limit
        ({"price": None}, GuardrailRule.INVALID_INPUT),
        ({"adr": None}, GuardrailRule.INVALID_INPUT),
        ({"code_pivot": float("nan")}, GuardrailRule.INVALID_INPUT),
        ({"atr": None}, GuardrailRule.INVALID_INPUT),
    ],
)
def test_skips(kw: dict[str, object], expected: GuardrailRule) -> None:
    assert rule(check(**kw)) is expected


def test_limit_below_the_trigger_is_skipped() -> None:
    assert rule(check(adr=1e-9, opening=OpeningRange(100.001, 99.0))) is GuardrailRule.PRICE_LEVELS


def test_keep_losses_small_rechecked_at_the_open() -> None:
    assert rule(check(record=StrategyRecord(20, 0.10))) is GuardrailRule.KEEP_LOSSES_SMALL


def test_sized_within_the_reservation() -> None:
    a = replace(approved_vcp(), reserved_exposure=510.0)
    plan = check(a).plan
    assert plan is not None
    assert plan.tranches == Tranches(2, 3)  # 510 / 102 = 5 shares


def test_zero_shares_at_the_open_is_skipped() -> None:
    assert (
        rule(check(replace(approved_vcp(), reserved_exposure=50.0))) is GuardrailRule.WHOLE_SHARES
    )


def momentum() -> ApprovedEntry:
    mb = setup(strategy=Strategy.MOMENTUM_BREAKOUT, structural_stop=85.0)
    action = entry(strategy=Strategy.MOMENTUM_BREAKOUT, stop=88.0)
    a = evaluate([action], context(mb), RISK).decisions[0].approved
    assert isinstance(a, ApprovedEntry)
    return a


def test_momentum_stop_is_the_low_of_the_day() -> None:
    plan = check(momentum(), opening=OpeningRange(101.0, 97.0), price=100.8, low_of_day=97.0).plan
    assert plan is not None
    # max(consolidation high 100, range high 101) = 101; 1 x ADR = 4.04 below it.
    assert (plan.trigger, plan.stop, plan.kind) == (101.0, 97.0, OrderKind.STOP_LIMIT)
    assert plan.tranches.second == 0


def test_momentum_low_of_day_wider_than_1x_adr_is_rejected() -> None:
    result = check(momentum(), opening=OpeningRange(101.0, 96.0), price=100.8, low_of_day=96.0)
    assert rule(result) is GuardrailRule.STOP_WIDTH


def test_momentum_without_a_low_or_an_opening_range() -> None:
    assert rule(check(momentum(), low_of_day=None)) is GuardrailRule.INVALID_INPUT
    assert rule(check(momentum(), opening=None)) is GuardrailRule.PRICE_LEVELS


# --- A close entry at the decision time (the #144 candidate) ---------------------------------

CLOSE = RISK.with_candidate("CLOSE")


def approved_close(strategy: Strategy = Strategy.VCP_BREAKOUT, **kw: object) -> ApprovedEntry:
    facts = setup(strategy=strategy, **kw)
    a = evaluate([entry(strategy=strategy)], context(facts), CLOSE).decisions[0].approved
    assert isinstance(a, ApprovedEntry)
    return a


def at_close(
    a: ApprovedEntry | None = None, *, risk: RiskConfig = CLOSE, **kw: object
) -> OrderCheck:
    defaults = MarketAtDecision(price=101.0, low_of_day=98.0, atr=2.5, adr=0.04)
    m = replace(defaults, **kw)  # type: ignore[arg-type]
    return check_close(a or approved_close(), m, equity=EQUITY, record=None, risk=risk)


def test_close_entry_is_a_marketable_limit_stopped_at_the_day_low() -> None:
    a = approved_close()
    assert a.tranches == 1
    plan = at_close(a).plan
    assert plan is not None
    # 101 x 1.005 = 101.505, up to the tick; the day low 98 is above the stop of 94.
    assert (plan.kind, plan.trigger, plan.limit, plan.stop) == (
        OrderKind.LIMIT,
        101.51,
        101.51,
        98.0,
    )
    # $100 at risk / 3.51 = 28 shares, but the reservation (12 x 102 = 1,224) allows 12.
    assert plan.tranches == Tranches(12, 0)


def test_close_entry_stop_never_below_the_structural_stop() -> None:
    plan = at_close(low_of_day=92.0).plan
    assert plan is not None
    assert plan.stop == 94.0


def test_close_entry_with_the_structural_stop() -> None:
    vcp = CLOSE.plans.entry[Strategy.VCP_BREAKOUT]
    policy = vcp.model_copy(update={"stop": StopKind.STRUCTURAL})
    plans = CLOSE.plans.model_copy(
        update={"entry": {**CLOSE.plans.entry, Strategy.VCP_BREAKOUT: policy}}
    )
    structural = CLOSE.model_copy(update={"plans": plans})
    plan = at_close(risk=structural, low_of_day=None).plan
    assert plan is not None
    assert plan.stop == 94.0


@pytest.mark.parametrize("price", [None, float("nan"), 0.0])
def test_close_entry_without_a_decision_price(price: float | None) -> None:
    assert rule(at_close(price=price)) is GuardrailRule.INVALID_INPUT


def test_close_entry_without_a_low_of_the_day() -> None:
    assert rule(at_close(low_of_day=None)) is GuardrailRule.INVALID_INPUT


def test_close_entry_stop_too_wide_from_the_limit() -> None:
    # Limit 105.53: 8% is 8.44, the stop of 94 is 11.53 below it.
    assert rule(at_close(price=105.0, low_of_day=93.0)) is GuardrailRule.STOP_WIDTH


def test_close_entry_atr_cap_is_measured_from_the_limit() -> None:
    a = approved_close(Strategy.LEADER_PULLBACK)
    # 1.5 x ATR = 3.75. From the decision price 101 the stop 97.4 is 3.60 away, from the
    # limit 101.51 it is 4.11: rejected.
    assert rule(at_close(a, low_of_day=97.4)) is GuardrailRule.STOP_WIDTH
    assert at_close(a, low_of_day=98.0).plan is not None


def test_close_entry_ep_takes_the_candidate_8pct_cap() -> None:
    a = approved_close(Strategy.EPISODIC_PIVOT, structural_stop=90.0)
    # Limit 104.52, stop at the gap day's low 97: 7.52 below it. 8% (8.36) allows it; the
    # 09:36 cap of 1.5 x the pre-gap ADR (6.27) would not.
    assert at_close(a, price=104.0, low_of_day=97.0).plan is not None
    assert rule(at_close(a, risk=RISK, price=104.0, low_of_day=97.0)) is GuardrailRule.STOP_WIDTH
    assert rule(at_close(a, price=104.0, low_of_day=95.0)) is GuardrailRule.STOP_WIDTH


def test_close_entry_under_one_share_is_skipped() -> None:
    a = replace(approved_close(), reserved_exposure=50.0)
    assert rule(at_close(a)) is GuardrailRule.WHOLE_SHARES
