"""The 09:36 check of an approved entry (README §7.1, §8)."""

from dataclasses import replace

import pytest

from auratrade.config import Strategy
from auratrade.execution.sizing import Tranches
from auratrade.execution.triggers import OpeningRange, OrderKind
from auratrade.guardrails.evaluate import ApprovedEntry, evaluate
from auratrade.guardrails.levels import StrategyRecord
from auratrade.guardrails.model import GuardrailRule, Outcome
from auratrade.guardrails.orders import MarketAtOpen, OrderCheck, check_order
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
