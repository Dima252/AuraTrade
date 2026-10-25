"""One trade through the simulator, on hand-built days with known fills (archived README §5.12).

The default setup is a VCP: pivot 50, structural stop 47, ADR 4% -> trigger 50.00, limit 51.00.
$50 of risk sized on the limit is 12 shares: 6 at the trigger, 6 on confirmation. History is 60
flat sessions at 49 on 1M shares ($49M a day: 0.2% slippage), so a trigger fill is 50.10 and
R = 3.10 per share on tranche 1.
"""

from dataclasses import replace

import pytest

from auratrade.config import Strategy, load_risk_config
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
from tests.sim.helpers import DECISION_MINUTE, OPEN_AT, M, at, bars, flat

RISK = load_risk_config()
HISTORY = [Bar(49, 49.5, 48.5, 49, 1_000_000)] * 60
DECIDE = DECISION_MINUTE - 1  # the last bar before 15:40

# A quiet open under the pivot, then the breakout at 09:40, closing near the high.
OPENING = dict.fromkeys(range(5), (49.5, 49.8, 49.4, 49.7)) | {5: (49.7, 49.9, 49.6, 49.85)}
BREAKOUT = OPENING | {
    10: (49.9, 50.4, 49.85, 50.3),
    200: (50.3, 50.8, 50.2, 50.6),
    DECIDE: (50.6, 50.9, 50.5, 50.8),
    380: (50.8, 51.0, 50.7, 50.9),
}
ENTRY_DAY = Bar(49.5, 51.0, 49.4, 50.9, 2_000_000)


def approved(strategy: Strategy = Strategy.VCP_BREAKOUT, **kw: object) -> ApprovedEntry:
    defaults = ApprovedEntry(
        symbol="ABC",
        strategy=strategy,
        pivot=50.0,
        stop=47.0,
        conviction=None,
        exits=RISK.plans.default_exit_plan(strategy),
        risk=0.01,
        risk_amount=50.0,
        planned_limit=51.0,
        planned_shares=12,
        tranches=RISK.plans.entry[strategy].tranches,
        reserved_exposure=612.0,
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


def entry_session(
    spec: dict[int, tuple[float, float, float, float]] | None = None,
    bar: Bar = ENTRY_DAY,
    **kw: object,
) -> SimSession:
    return SimSession(
        bar=bar,
        open_at=OPEN_AT,
        decision_at=at(DECISION_MINUTE),
        sip=bars(spec if spec is not None else BREAKOUT),
        **kw,  # type: ignore[arg-type]
    )


def daily(o: float, h: float, low: float, c: float, volume: float = 1_000_000) -> SimSession:
    return SimSession(bar=Bar(o, h, low, c, volume))


def run(*sessions: SimSession, trade: TradeSetup | None = None) -> TradeResult:
    return simulate_trade(
        trade or setup(),
        sessions or (entry_session(),),
        equity=5000.0,
        record=None,
        risk=RISK,
    )


def kinds(result: TradeResult) -> list[tuple[FillKind, int, float]]:
    return [(f.kind, f.shares, f.price) for f in result.fills]


class TestEntry:
    def test_a_breakout_fills_tranche_1_at_the_trigger_plus_slippage(self) -> None:
        result = run()
        assert result.outcome is TradeOutcome.OPEN
        assert result.order is not None
        assert (result.order.trigger, result.order.limit, result.order.stop) == (50, 51, 47)
        assert (result.order.tranches.first, result.order.tranches.second) == (6, 6)
        assert result.fills == (TradeFill(1, FillKind.ENTRY, 6, 50.10),)
        assert result.risk == pytest.approx(6 * 3.10)
        assert result.pnl == pytest.approx(6 * (50.9 - 50.10))
        assert result.marks == (pytest.approx(result.pnl / result.risk),)
        assert result.r == pytest.approx(result.marks[0])

    def test_a_breakout_past_its_limit_that_never_comes_back_is_a_miss(self) -> None:
        jump = OPENING | {10: (51.5, 52.0, 51.2, 51.8), 11: (51.8, 53.0, 51.6, 52.9)}
        jump |= {DECIDE: (53.0, 53.5, 52.8, 53.2)}
        result = run(entry_session(jump))
        assert result.outcome is TradeOutcome.MISSED
        assert result.order is not None
        assert (result.fills, result.r, result.triggered) == ((), None, False)

    def test_one_that_comes_back_to_the_limit_fills_at_the_limit(self) -> None:
        back = OPENING | {10: (51.5, 52.0, 51.2, 51.8), 30: (51.8, 51.9, 50.7, 50.9)}
        back |= {DECIDE: flat(51.2)}
        result = run(entry_session(back))
        assert kinds(result) == [(FillKind.ENTRY, 6, 51.0)]

    def test_an_untriggered_order_is_a_miss(self) -> None:
        quiet = OPENING | {100: (49.8, 49.95, 49.5, 49.6), DECIDE: flat(49.7)}
        assert run(entry_session(quiet)).outcome is TradeOutcome.MISSED

    def test_the_order_window_closes_at_the_decision_time(self) -> None:
        late = OPENING | {DECIDE: flat(49.7), 380: (49.9, 50.5, 49.9, 50.4)}
        assert run(entry_session(late)).outcome is TradeOutcome.MISSED

    def test_a_price_past_the_limit_at_0936_is_skipped(self) -> None:
        result = run(entry_session(OPENING | {5: (51.2, 51.5, 51.1, 51.4)}))
        assert result.outcome is TradeOutcome.SKIPPED
        assert result.order is None
        assert [f.rule for f in result.findings] == [GuardrailRule.PRICE_LEVELS]

    def test_past_the_trigger_but_within_the_limit_goes_in_as_a_limit_buy(self) -> None:
        through = OPENING | {5: (50.1, 50.3, 50.0, 50.2), 6: (50.2, 50.5, 50.1, 50.4)}
        through |= {DECIDE: flat(50.6)}
        result = run(entry_session(through))
        assert kinds(result) == [(FillKind.ENTRY, 6, 50.31)]  # the 09:36 open x 1.002

    def test_levels_come_from_iex_and_fills_from_sip(self) -> None:
        # IEX's opening range traded above the pivot to 50.30 (SIP's reached 50.60): the
        # trigger is IEX's opening-range high, and SIP's 09:40 bar fills it.
        iex = bars({0: (50.0, 50.3, 49.9, 50.1), 5: flat(50.1), DECIDE: flat(50.9)})
        sip = OPENING | {0: (50.0, 50.6, 49.9, 50.1), 10: (50.2, 50.8, 50.1, 50.7)}
        sip |= {DECIDE: flat(50.9)}
        # A 47.50 stop: 47 would be more than 8% below the higher limit.
        result = run(entry_session(sip, iex=iex), trade=setup(approved(stop=47.5)))
        assert result.order is not None
        assert (result.order.trigger, result.order.limit) == (50.30, 51.30)
        # max(50.30, 50.2) x 1.002; $612 reserved at the 51.30 limit is 11 shares: 5 + 6.
        assert kinds(result) == [(FillKind.ENTRY, 5, 50.41)]

    def test_momentum_without_an_iex_opening_range_waits_for_the_delayed_sip_range(
        self,
    ) -> None:
        # IEX printed nothing: the consolidated range (high 50.20, low 49.60) sets the
        # trigger and the low-of-day stop, and the order goes in at 09:51. SIP's 09:42 bar
        # above the trigger is before the order and doesn't fill it.
        sip = {
            0: (49.8, 50.2, 49.6, 50.0),
            12: (50.0, 50.5, 49.9, 50.1),
            20: (50.1, 50.15, 50.0, 50.1),
            25: (50.1, 50.6, 50.05, 50.5),
            DECIDE: flat(50.8),
        }
        momentum = approved(Strategy.MOMENTUM_BREAKOUT, reserved_exposure=1250.0)
        result = run(entry_session(sip, iex=[]), trade=setup(momentum))
        assert result.order is not None
        assert (result.order.trigger, result.order.stop) == (50.20, 49.60)
        assert result.order.tranches.second == 0
        assert [(f.kind, f.price) for f in result.fills] == [(FillKind.ENTRY, 50.31)]


class TestStops:
    def test_the_fill_bar_can_stop_out_too(self) -> None:
        # Pessimistic: a minute that reaches both the trigger and the stop stops out.
        crash = OPENING | {10: (49.9, 50.4, 46.8, 47.0), DECIDE: flat(46.5)}
        result = run(entry_session(crash))
        assert result.outcome is TradeOutcome.CLOSED
        assert result.exit is Rule.HARD_STOP
        assert kinds(result) == [(FillKind.ENTRY, 6, 50.10), (FillKind.EXIT, 6, 46.90)]
        assert result.r == pytest.approx(6 * (46.90 - 50.10) / (6 * 3.10))

    def test_a_gap_through_the_stop_fills_at_the_open(self) -> None:
        result = run(entry_session(), daily(45, 45.5, 44, 44.5))
        assert kinds(result)[-1] == (FillKind.EXIT, 6, 44.91)
        assert result.marks[-1] == pytest.approx(result.r)

    def test_a_bar_reaching_the_stop_and_plus_2r_takes_the_stop(self) -> None:
        result = run(entry_session(), daily(51, 57, 46.5, 56.5))
        assert result.exit is Rule.HARD_STOP
        assert kinds(result)[1:] == [(FillKind.EXIT, 6, 46.90)]

    def test_after_the_decision_the_stop_still_runs_to_the_close(self) -> None:
        late_drop = SimSession(
            bar=Bar(50.8, 51, 46.5, 46.8, 1_000_000),
            open_at=at(0, day=1),
            decision_at=at(DECISION_MINUTE, day=1),
            sip=bars({0: flat(50.8), DECIDE: flat(50.5), 380: (50.5, 50.5, 46.5, 46.8)}, day=1),
        )
        result = run(entry_session(), late_drop)
        assert (result.exit, kinds(result)[-1]) == (Rule.HARD_STOP, (FillKind.EXIT, 6, 46.90))
        assert len(result.marks) == 2


class TestPlanRules:
    def test_tranche_2_on_confirmation_then_a_gap_through_stop(self) -> None:
        result = run(
            entry_session(rs_line_near_high=True),
            daily(50.9, 52, 50.5, 51.5),
            daily(45, 45.5, 44, 44.5, 3_000_000),
        )
        # Tranche 2 at the decision price 50.80 + 0.2% = 50.91, sized on its limit 51.06:
        # min((50 - 6 x 3.10) / (51.06 - 47), 12 - 6) = 6 shares.
        assert kinds(result) == [
            (FillKind.ENTRY, 6, 50.10),
            (FillKind.TRANCHE_2, 6, 50.91),
            (FillKind.EXIT, 12, 44.91),
        ]
        assert result.plan is not None
        assert result.plan.tranche2_filled
        assert result.plan.entry_price == pytest.approx((50.10 + 50.91) / 2)
        assert result.risk == pytest.approx(6 * 3.10 + 6 * 3.91)
        assert result.pnl == pytest.approx(12 * 44.91 - 6 * 50.10 - 6 * 50.91)
        assert len(result.marks) == 3

    @pytest.mark.parametrize(
        "kw",
        [
            {"rs_line_near_high": False},
            {"rs_line_near_high": True, "tranche2_allowed": False},
            {"rs_line_near_high": True, "projected_volume": 1_000_000},
        ],
    )
    def test_no_tranche_2_without_the_gate_or_while_not_fundable(
        self, kw: dict[str, object]
    ) -> None:
        assert [f.kind for f in run(entry_session(**kw)).fills] == [FillKind.ENTRY]

    def test_no_tranche_2_after_a_close_below_the_pivot(self) -> None:
        dip = BREAKOUT | {DECIDE: (50.2, 50.2, 49.6, 49.7)}
        result = run(entry_session(dip, rs_line_near_high=True))
        assert [f.kind for f in result.fills] == [FillKind.ENTRY]

    def test_no_tranche_2_when_the_entry_sized_to_one_tranche(self) -> None:
        small = approved(risk_amount=6.0)  # 6 / 4 = 1 share: a single tranche
        result = run(entry_session(rs_line_near_high=True), trade=setup(small))
        assert kinds(result) == [(FillKind.ENTRY, 1, 50.10)]

    def test_no_tranche_2_once_tranche_1_has_used_the_planned_risk(self) -> None:
        # $8 of risk is 2 shares (1 + 1). Tranche 1 filled at the limit (51.00) puts $4 at
        # risk; tranche 2 on its 51.46 limit would put 4.46 more: over the plan, so 0 shares.
        back = OPENING | {10: (51.5, 52.0, 51.2, 51.8), 30: (51.8, 51.9, 50.7, 50.9)}
        back |= {DECIDE: flat(51.2)}
        result = run(
            entry_session(back, rs_line_near_high=True), trade=setup(approved(risk_amount=8.0))
        )
        assert result.order is not None
        assert result.order.tranches.second == 1
        assert kinds(result) == [(FillKind.ENTRY, 1, 51.0)]

    def test_a_session_with_no_bars_before_the_decision_still_runs_the_stop(self) -> None:
        late = SimSession(
            bar=Bar(50.8, 51, 46.5, 46.8, 1_000_000),
            open_at=at(0, day=1),
            decision_at=at(DECISION_MINUTE, day=1),
            sip=bars({380: (50.5, 50.5, 46.5, 46.8)}, day=1),
        )
        assert run(entry_session(), late).exit is Rule.HARD_STOP

    def test_the_partial_at_plus_2r_then_the_stop_at_breakeven(self) -> None:
        result = run(
            entry_session(),
            daily(51, 56.6, 50.8, 56.5),
            daily(55, 55.5, 50, 50.5),
        )
        # +2R = 50.10 + 2 x 3.10 = 56.30: a third of 6 sold at 56.50 less 0.2%; the stop
        # moves to breakeven (50.10) and the next day's low takes it.
        assert kinds(result)[1:] == [
            (FillKind.SALE, 2, 56.38),
            (FillKind.EXIT, 4, 49.99),
        ]
        assert result.fills[1].rule is Rule.STRENGTH_PARTIAL
        assert result.exit is Rule.HARD_STOP

    def test_a_trail_exit_fills_at_the_decision_price_less_slippage(self) -> None:
        # The 50-day is 49.03 on day 2; a close at 48.80 is below it.
        result = run(entry_session(), daily(50, 50.2, 48.6, 48.8))
        assert result.exit is Rule.TRAIL
        assert kinds(result)[-1] == (FillKind.EXIT, 6, 48.70)

    def test_a_given_decision_price_overrides_the_close(self) -> None:
        held = replace(daily(50, 50.2, 48.6, 48.8), decision_price=50.5)
        assert run(entry_session(), held).outcome is TradeOutcome.OPEN

    def test_without_a_decision_price_only_the_stop_runs(self) -> None:
        stale = SimSession(
            bar=Bar(50, 50.2, 48.6, 48.8, 1_000_000),
            open_at=at(0, day=1),
            decision_at=at(DECISION_MINUTE, day=1),
            sip=bars({0: (50, 50.2, 48.6, 48.8)}, day=1),
        )
        result = run(entry_session(), stale)
        assert (result.outcome, len(result.marks)) == (TradeOutcome.OPEN, 2)

    def test_events_carry_their_session(self) -> None:
        result = run(entry_session(), daily(51, 56.6, 50.8, 56.5))
        assert {session for session, _ in result.events} == {2}


class TestResult:
    def test_r_after_reads_the_marks_then_the_final_r(self) -> None:
        open_trade = run(entry_session(), daily(50.9, 52, 50.5, 51.5))
        assert open_trade.r_after(2) == pytest.approx(6 * (51.5 - 50.10) / (6 * 3.10))
        assert open_trade.r_after(5) is None
        closed = run(entry_session(), daily(45, 45.5, 44, 44.5))
        assert closed.r_after(20) == closed.r
        with pytest.raises(ValueError, match=">= 1"):
            closed.r_after(0)

    def test_a_skipped_or_missed_trade_has_no_r(self) -> None:
        assert TradeResult(TradeOutcome.SKIPPED).r_after(5) is None


class TestInputs:
    def test_needs_sessions_and_history(self) -> None:
        with pytest.raises(ValueError, match="no sessions"):
            simulate_trade(setup(), [], equity=5000.0, record=None, risk=RISK)
        with pytest.raises(ValueError, match="session before its entry"):
            run(trade=setup(history=[]))

    def test_the_entry_session_needs_minute_bars(self) -> None:
        with pytest.raises(ValueError, match="entry session needs minute bars"):
            run(daily(49, 51, 48, 50.5))

    def test_minute_bars_need_their_times(self) -> None:
        untimed = SimSession(bar=ENTRY_DAY, sip=[M(at(0, day=1), 50, 50, 50, 50)])
        with pytest.raises(ValueError, match="open and decision times"):
            run(entry_session(), untimed)
