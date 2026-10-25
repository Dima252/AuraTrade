"""A close entry through the simulator (the #144 candidate), on hand-built days with known fills.

The default setup is the VCP of ``test_trade``: pivot 50, structural stop 47, 60 flat sessions at
49 on 1M shares ($49M a day: 0.2% slippage), $50 of risk. Under the `CLOSE` candidate it is
bought on the entry session's decision price if the breakout gate passes. On a daily bar that
price is the close: 50.9 -> limit 50.9 x 1.005 = 51.16, fill 50.9 x 1.002 = 51.01, stop at the
day's low 49.4 (above the structural 47). $50 / (51.16 - 49.4) = 28 shares, capped at 25% of
$5,000 / 51.16 = 24.
"""

from dataclasses import replace

import pytest

from auratrade.config import RiskConfig, Strategy, load_risk_config
from auratrade.guardrails.evaluate import ApprovedEntry
from auratrade.guardrails.model import GuardrailRule
from auratrade.indicators.bars import Bar
from auratrade.plans.model import Rule
from auratrade.sim.trade import (
    FillKind,
    SimSession,
    TradeFill,
    TradeOutcome,
    TradeResult,
    TradeSetup,
    simulate_trade,
)
from tests.sim.helpers import DECISION_MINUTE, OPEN_AT, at, bars
from tests.sim.test_trade import BREAKOUT, HISTORY

CLOSE_RISK = load_risk_config().with_candidate("CLOSE")
ENTRY_DAY = Bar(49.5, 51.0, 49.4, 50.9, 2_000_000)


def approved(strategy: Strategy = Strategy.VCP_BREAKOUT, **kw: object) -> ApprovedEntry:
    defaults = ApprovedEntry(
        symbol="ABC",
        strategy=strategy,
        pivot=50.0,
        stop=47.0,
        conviction=None,
        exits=CLOSE_RISK.plans.default_exit_plan(strategy),
        risk=0.01,
        risk_amount=50.0,
        planned_limit=51.0,
        planned_shares=12,
        tranches=1,
        reserved_exposure=5000.0,
    )
    return replace(defaults, **kw)  # type: ignore[arg-type]


def setup(entry: ApprovedEntry | None = None, **kw: object) -> TradeSetup:
    defaults = TradeSetup(
        entry=entry or approved(),
        code_pivot=50.0,
        atr=1.0,
        adr=0.04,
        dollar_volume=49e6,
        history=HISTORY,
    )
    return replace(defaults, **kw)  # type: ignore[arg-type]


def day(bar: Bar = ENTRY_DAY, **kw: object) -> SimSession:
    return SimSession(bar=bar, **{"rs_line_near_high": True, **kw})  # type: ignore[arg-type]


def run(
    *sessions: SimSession, trade: TradeSetup | None = None, risk: RiskConfig = CLOSE_RISK
) -> TradeResult:
    return simulate_trade(
        trade or setup(), sessions or (day(),), equity=5000.0, record=None, risk=risk
    )


def test_daily_close_entry_fills_at_the_decision_price_plus_slippage() -> None:
    result = run(day(), day(Bar(51.0, 52.0, 50.5, 51.5, 1_000_000)))
    assert result.outcome is TradeOutcome.OPEN
    assert result.gate is not None
    assert result.gate.passed
    order = result.order
    assert order is not None
    assert (order.limit, order.stop, order.tranches.first, order.tranches.second) == (
        51.16,
        49.4,
        24,
        0,
    )
    assert result.fills == (TradeFill(1, FillKind.ENTRY, 24, 51.01),)
    assert result.risk == pytest.approx(24 * (51.01 - 49.4))
    assert len(result.marks) == 2


@pytest.mark.parametrize(
    ("bar", "failure"),
    [
        (Bar(49.5, 50.5, 49.4, 49.9, 2_000_000), "below the level"),
        (Bar(49.5, 52.8, 49.4, 52.6, 2_000_000), "max chase"),  # > 50 x 1.05
        (Bar(49.5, 51.0, 49.4, 49.9, 2_000_000), "below the level"),
        (Bar(50.0, 53.0, 49.4, 50.9, 2_000_000), "upper"),  # (50.9 - 49.4) / 3.6 < 0.5
        (Bar(49.5, 51.0, 49.4, 50.9, 1_300_000), "volume"),  # < 1.4 x 1M
    ],
)
def test_a_failed_gate_is_not_confirmed(bar: Bar, failure: str) -> None:
    result = run(day(bar))
    assert result.outcome is TradeOutcome.NOT_CONFIRMED
    assert result.gate is not None
    assert any(failure in f for f in result.gate.failures)
    assert (result.order, result.fills) == (None, ())


def test_the_rs_line_condition_is_read_from_the_entry_session() -> None:
    result = run(day(rs_line_near_high=False))
    assert result.outcome is TradeOutcome.NOT_CONFIRMED


def test_a_stop_too_wide_from_the_limit_is_skipped() -> None:
    # The low 46.9 is under the structural stop: the stop is 47, 4.16 below the limit 51.16;
    # 8% of the limit is 4.09.
    result = run(day(Bar(49.5, 51.0, 46.9, 50.9, 2_000_000)))
    assert result.outcome is TradeOutcome.SKIPPED
    assert result.findings[0].rule is GuardrailRule.STOP_WIDTH
    assert result.gate is not None


def test_a_fill_beyond_the_limit_is_missed() -> None:
    tiers = [t.model_copy(update={"fraction": 0.01}) for t in CLOSE_RISK.sim.slippage]
    sim = CLOSE_RISK.sim.model_copy(update={"slippage": tiers})
    risk = CLOSE_RISK.model_copy(update={"sim": sim})
    result = run(day(), risk=risk)
    assert result.outcome is TradeOutcome.MISSED
    assert result.order is not None


def test_no_decision_price_is_skipped() -> None:
    stale = SimSession(
        bar=ENTRY_DAY,
        open_at=OPEN_AT,
        decision_at=at(DECISION_MINUTE),
        sip=bars({10: (49.9, 50.4, 49.85, 50.3)}),
        rs_line_near_high=True,
    )
    result = run(stale)
    assert result.outcome is TradeOutcome.SKIPPED
    assert result.findings[0].rule is GuardrailRule.INVALID_INPUT


def test_minute_bars_decide_at_1540_and_the_stop_runs_to_the_close() -> None:
    crash = BREAKOUT | {385: (50.0, 50.0, 49.0, 49.2)}
    session = SimSession(
        bar=Bar(49.5, 51.0, 49.0, 49.2, 2_000_000),
        open_at=OPEN_AT,
        decision_at=at(DECISION_MINUTE),
        sip=bars(crash),
        rs_line_near_high=True,
    )
    result = run(session, day(Bar(49.0, 50.0, 48.0, 49.5, 1_000_000)))
    # The 15:40 price is 50.8 (the bar at 15:39); the day's low by then is 49.4.
    assert result.order is not None
    assert (result.order.limit, result.order.stop) == (51.06, 49.4)
    assert result.fills[0] == TradeFill(1, FillKind.ENTRY, 24, 50.91)
    assert result.outcome is TradeOutcome.CLOSED
    assert result.exit is Rule.HARD_STOP
    assert result.fills[-1].session == 1
    assert len(result.marks) == 1


def test_a_later_session_through_the_stop_closes_it() -> None:
    result = run(day(), day(Bar(50.5, 50.6, 49.0, 49.2, 1_000_000)))
    assert result.outcome is TradeOutcome.CLOSED
    assert result.exit is Rule.HARD_STOP
    assert result.fills[-1].session == 2
    r = result.r
    assert r is not None
    assert r < 0


def test_an_ep_at_the_close_has_no_max_chase_but_its_8pct_cap() -> None:
    ep = approved(Strategy.EPISODIC_PIVOT, pivot=55.0, stop=49.0)
    trade = setup(ep, code_pivot=55.0)
    held = run(day(Bar(55.0, 58.0, 54.0, 57.5, 3_500_000), rs_line_near_high=False), trade=trade)
    assert held.outcome is TradeOutcome.OPEN
    assert held.order is not None
    # Limit 57.5 x 1.005 = 57.79, the stop at the gap day's low 54 (above the prior close 49).
    assert (held.order.limit, held.order.stop) == (57.79, 54.0)
    # 9% above the open passes the gate; the stop 5.6 below the limit 60.10 is over 8% (4.81).
    far = run(day(Bar(55.0, 60.0, 54.5, 59.8, 3_500_000)), trade=trade)
    assert far.outcome is TradeOutcome.SKIPPED
    assert far.findings[0].rule is GuardrailRule.STOP_WIDTH
    gap_filled = run(day(Bar(55.0, 56.0, 50.0, 54.9, 3_500_000)), trade=trade)
    assert gap_filled.outcome is TradeOutcome.NOT_CONFIRMED
