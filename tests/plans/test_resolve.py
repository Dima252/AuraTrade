"""Rule precedence (README §7.3), mostly on sessions where several rules fire at once."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from auratrade.config import Regime, Strategy, TrailMA
from auratrade.plans.model import (
    Day,
    EventKind,
    HoldState,
    PlanChange,
    PositionPlan,
    Resolution,
    Rule,
    Sale,
)
from auratrade.plans.resolve import resolve
from auratrade.plans.versioning import check_tighten_only
from tests.plans.factories import RISK, make_day, make_plan

ONEIL = make_plan(Strategy.EMERGING_LEADER)


def run(plan: PositionPlan, day: Day, shares: int = 100) -> Resolution:
    return resolve(plan, day, shares=shares, config=RISK)


def kinds(resolution: Resolution) -> list[EventKind]:
    return [e.kind for e in resolution.events]


# --- Full exits: the highest rule is the reason, and nothing else runs ------------------------


def test_hard_stop_beats_everything() -> None:
    day = make_day(
        price=95.0,
        low=91.5,
        climax_signals=3,
        earnings_cushion_due=True,
        trails={TrailMA.SMA_50: 99.0},
        regime_downgrade=Regime.CORRECTION,
    )
    result = run(ONEIL, day)
    assert result.exit is Rule.HARD_STOP
    assert result.sales == ()
    assert result.plan is ONEIL  # nothing else ran, not even the downgrade
    assert kinds(result) == [EventKind.EXIT]


def test_earnings_cushion_beats_the_heavy_volume_reversal() -> None:
    # Session 2: gain -0.125R (cushion exit) and a heavy-volume reversal.
    day = make_day(
        session=2,
        price=99.0,
        prior_close=101.0,
        volume=2000.0,
        earnings_cushion_due=True,
    )
    assert run(ONEIL, day).exit is Rule.EARNINGS_CUSHION


def test_heavy_volume_reversal_beats_climax_and_trail() -> None:
    day = make_day(
        session=3,
        price=99.0,
        prior_close=101.0,
        volume=2000.0,
        climax_signals=3,
        trails={TrailMA.SMA_50: 99.5},
    )
    assert run(ONEIL, day).exit is Rule.HEAVY_VOLUME_REVERSAL


def test_climax_beats_the_trail() -> None:
    day = make_day(session=25, price=130.0, climax_signals=3, trails={TrailMA.SMA_50: 131.0})
    assert run(ONEIL, day).exit is Rule.CLIMAX


def test_trail_beats_the_time_stop() -> None:
    plan = make_plan(time_stop=True)
    day = make_day(session=20, price=95.0, high_since_entry=105.0, trails={TrailMA.SMA_50: 96.0})
    assert run(plan, day).exit is Rule.TRAIL
    assert (
        run(plan, make_day(session=20, price=97.0, high_since_entry=105.0)).exit is Rule.TIME_STOP
    )


def test_rules_outside_the_plan_never_fire() -> None:
    # The Minervini set has no climax sales, heavy-volume reversal or time stop.
    day = make_day(
        session=20,
        price=99.0,
        prior_close=101.0,
        volume=3000.0,
        climax_signals=4,
        high_since_entry=101.0,
    )
    result = run(make_plan(), day)
    assert result.exit is None
    assert result.sales == ()


# --- Plan changes come first; the session's exits see the tightened plan ----------------------


def test_downgrade_to_correction_then_the_10_day_trail_exits_the_same_session() -> None:
    day = make_day(price=104.0, regime_downgrade=Regime.CORRECTION, trails={TrailMA.TEN_DAY: 104.5})
    result = run(make_plan(), day)
    assert result.exit is Rule.TRAIL
    assert result.plan.exits.trail is TrailMA.TEN_DAY
    assert (result.plan.version, result.plan.change) == (2, PlanChange.REGIME_DOWNGRADE)
    assert kinds(result)[:2] == [EventKind.REGIME_DOWNGRADE, EventKind.TRAIL_TIGHTENED]
    assert kinds(result)[-1] is EventKind.EXIT


def test_a_raised_stop_the_price_is_through_sells_at_once() -> None:
    # +3R (124) was reached, so the stop is at breakeven (100); the price is back at 99.5.
    day = make_day(price=99.5, low=99.0, high_since_entry=125.0)
    result = run(make_plan(), day)
    assert result.exit is Rule.HARD_STOP
    assert result.plan.hard_stop == 100.0


def test_the_hold_replaces_the_normal_trail_with_the_50_day() -> None:
    plan = make_plan(Strategy.MOMENTUM_BREAKOUT, eight_week_hold=True)  # 10-day trail
    day = make_day(session=5, price=121.0, trails={TrailMA.TEN_DAY: 122.0, TrailMA.SMA_50: 105.0})
    result = run(plan, day)
    assert result.exit is None  # below the 10-day, but the hold trails the 50-day
    assert result.plan.hold is HoldState.ACTIVE
    assert result.plan.hard_stop == 100.0  # the hold keeps the stop at breakeven or above
    assert result.plan.change is PlanChange.EIGHT_WEEK_HOLD
    # Below the 50-day, the hold exits.
    later = make_day(
        session=12, price=104.0, trails={TrailMA.TEN_DAY: 122.0, TrailMA.SMA_50: 105.0}
    )
    assert run(result.plan, later).exit is Rule.TRAIL


def test_the_hold_suppresses_the_strength_partial() -> None:
    # +20% at session 10: the hold starts, and the O'Neil +20% partial is suspended.
    result = run(ONEIL, make_day(session=10, price=121.0))
    assert result.plan.hold is HoldState.ACTIVE
    assert result.sales == ()
    assert not result.plan.partial_taken
    # Climax sales still run during the hold.
    climax = run(result.plan, make_day(session=12, price=135.0, climax_signals=2))
    assert climax.sales == (Sale(Rule.CLIMAX, 50),)
    # After the hold, the partial resumes.
    after = run(climax.plan, make_day(session=41, price=140.0), shares=50)
    assert after.plan.hold is HoldState.ENDED
    assert after.sales == (Sale(Rule.STRENGTH_PARTIAL, 12),)


def test_the_late_stage_sale_runs_during_a_hold() -> None:
    # User decision (task 3.5a): ranked with the climax sales, not suppressed by the hold.
    active = ONEIL.evolve(hold=HoldState.ACTIVE, hold_until_session=40, hard_stop=100.0)
    result = run(active, make_day(session=20, price=130.0, breakout_base_count=4))
    assert result.sales == (Sale(Rule.LATE_STAGE, 50),)


# --- Partial sales stack in order, each on the shares left -------------------------------------


def test_partial_sales_stack_in_precedence_order() -> None:
    # Session 16 at +20% (2.5R): the cushion trims (1R-3R), climax sells ½ (2 signals), a
    # 4th-stage breakout sells ½, and the O'Neil partial sells ¼. The hold window has closed.
    day = make_day(
        session=16,
        price=120.0,
        high_since_entry=121.0,
        earnings_cushion_due=True,
        climax_signals=2,
        breakout_base_count=4,
    )
    result = run(ONEIL, day, shares=100)
    assert result.exit is None
    assert result.sales == (
        Sale(Rule.EARNINGS_CUSHION, 50),
        Sale(Rule.CLIMAX, 25),
        Sale(Rule.LATE_STAGE, 12),
        Sale(Rule.STRENGTH_PARTIAL, 3),
    )
    assert result.shares_sold == 90 < 100
    plan = result.plan
    assert plan.climax_half_taken
    assert plan.late_stage_taken
    assert plan.partial_taken
    assert plan.hold is HoldState.ENDED
    assert plan.hard_stop == 100.0  # round-trip rule and the cushion
    assert (plan.version, plan.change) == (2, PlanChange.EIGHT_WEEK_HOLD)
    assert EventKind.EARNINGS_CUSHION in kinds(result)
    assert kinds(result).count(EventKind.SALE) == 4


def test_partial_sales_never_sell_the_whole_position() -> None:
    plan = make_plan(Strategy.EMERGING_LEADER, time_stop=True)
    day = make_day(
        session=16,
        price=120.0,
        high_since_entry=121.0,
        earnings_cushion_due=True,
        climax_signals=2,
        breakout_base_count=4,
    )
    for shares in (1, 2, 3, 7):
        result = run(plan, day, shares=shares)
        assert result.exit is None
        assert result.shares_sold < shares


def test_strength_partial_then_breakeven() -> None:
    result = run(make_plan(), make_day(session=8, price=116.0, high_since_entry=117.0), shares=30)
    assert result.sales == (Sale(Rule.STRENGTH_PARTIAL, 10),)
    assert result.plan.partial_taken
    assert result.plan.hard_stop == 100.0  # breakeven after the first partial
    assert result.plan.change is PlanChange.PARTIAL_SALE
    assert EventKind.STOP_RAISED in kinds(result)
    # Once only.
    again = run(result.plan, make_day(session=9, price=118.0, high_since_entry=118.0), shares=20)
    assert again.sales == ()
    assert again.plan is result.plan


def test_kullamagi_partial_does_not_move_the_stop_without_the_rule() -> None:
    plan = make_plan(Strategy.MOMENTUM_BREAKOUT, breakeven_after_partial=False)
    result = run(plan, make_day(session=3, price=113.0), shares=9)
    assert result.sales == (Sale(Rule.STRENGTH_PARTIAL, 3),)
    assert result.plan.hard_stop == 92.0


def test_time_stop_halves_once() -> None:
    plan = make_plan(time_stop=True)
    result = run(plan, make_day(session=10, price=103.0, high_since_entry=106.0), shares=40)
    assert result.sales == (Sale(Rule.TIME_STOP, 20),)
    assert result.plan.hard_stop == 92.0  # halving a laggard isn't a profit partial
    again = run(result.plan, make_day(session=11, price=103.0, high_since_entry=106.0), shares=20)
    assert again.sales == ()


def test_cushion_hold_full_raises_the_stop() -> None:
    # At +3.125R the position may be held full; only the cushion raises the stop here.
    plan = make_plan(breakeven_at_r=False).evolve(partial_taken=True)
    day = make_day(session=30, price=125.0, high_since_entry=125.0, earnings_cushion_due=True)
    result = run(plan, day)
    assert result.sales == ()
    assert result.plan.hard_stop == 100.0
    assert result.plan.change is PlanChange.EARNINGS_CUSHION
    assert [e.details for e in result.events if e.kind is EventKind.EARNINGS_CUSHION] == [
        {"action": "HOLD"}
    ]


def test_cushion_off_ignores_the_report() -> None:
    plan = make_plan(earnings_cushion=False)
    assert run(plan, make_day(price=101.0, earnings_cushion_due=True)).exit is None


# --- Events and tranche 2 ---------------------------------------------------------------------


def test_quiet_session_changes_nothing() -> None:
    plan = make_plan()
    result = run(plan, make_day(session=5, price=104.0))
    assert result.plan is plan
    assert (result.exit, result.sales, result.events, result.block_tranche2) == (
        None,
        (),
        (),
        False,
    )


def test_dip_below_the_pivot_holds_and_blocks_tranche_2() -> None:
    # Heavy volume, but the Minervini set has no heavy-volume reversal: hold, no tranche 2.
    day = make_day(session=2, price=99.5, prior_close=101.0, volume=3000.0)
    result = run(make_plan(), day)
    assert result.exit is None
    assert result.block_tranche2
    assert [e.details for e in result.events if e.kind is EventKind.FAILED_BREAKOUT] == [
        {"session": 2}
    ]
    assert not run(make_plan(), make_day(session=25, price=99.5)).block_tranche2


def test_faltering_rs_and_climax_signals_are_events() -> None:
    result = run(make_plan(), make_day(price=110.0, rs_line_at_low=True, climax_signals=2))
    assert result.sales == ()  # no climax sales in the Minervini set
    assert set(kinds(result)) == {EventKind.FALTERING_RS, EventKind.CLIMAX_SIGNALS}


def test_every_downgrade_raises_an_event() -> None:
    plan = make_plan(regime_tightening=False)
    result = run(plan, make_day(price=104.0, regime_downgrade=Regime.UNDER_PRESSURE))
    assert result.plan is plan
    assert kinds(result) == [EventKind.REGIME_DOWNGRADE]


def test_exits_block_tranche_2() -> None:
    assert run(make_plan(), make_day(price=95.0, low=91.0)).block_tranche2


def test_shares_must_be_positive() -> None:
    with pytest.raises(ValueError, match="shares"):
        run(make_plan(), make_day(), shares=0)


# --- Property: the engine never loosens a plan or oversells ------------------------------------

sessions = st.lists(
    st.tuples(
        st.floats(min_value=0.85, max_value=1.15),  # price vs the prior close
        st.floats(min_value=0.0, max_value=4.0),  # volume vs average
        st.integers(min_value=0, max_value=4),  # climax signals
        st.sampled_from([None, None, None, Regime.UNDER_PRESSURE, Regime.CORRECTION]),
        st.booleans(),  # cushion due
        st.sampled_from([None, None, 3, 4]),  # breakout base count
        st.floats(min_value=0.8, max_value=1.1),  # trail level vs the price
    ),
    min_size=1,
    max_size=60,
)


@settings(max_examples=150, deadline=None)
@given(strategy=st.sampled_from(list(Strategy)), path=sessions, time_stop=st.booleans())
def test_plans_only_tighten_and_sales_never_exceed_the_position(
    strategy: Strategy,
    path: list[tuple[float, float, int, Regime | None, bool, int | None, float]],
    time_stop: bool,
) -> None:
    plan = make_plan(strategy, time_stop=time_stop)
    shares, prior, high_since = 300, 100.0, 100.0
    for session, (move, vol, climax, downgrade, cushion, bases, trail) in enumerate(path, start=1):
        price = prior * move
        high_since = max(high_since, price)
        day = make_day(
            session=session,
            price=price,
            prior_close=prior,
            high=price,
            low=price,
            high_since_entry=high_since,
            volume=1000.0 * vol,
            climax_signals=climax,
            regime_downgrade=downgrade,
            earnings_cushion_due=cushion,
            breakout_base_count=bases,
            trails=dict.fromkeys(TrailMA, price * trail),
        )
        result = run(plan, day, shares=shares)
        check_tighten_only(plan, result.plan)
        assert result.plan.version in (plan.version, plan.version + 1)
        assert result.plan.hard_stop >= plan.hard_stop
        if result.exit is not None:
            assert result.sales == ()
            break
        assert 0 <= result.shares_sold < shares
        shares -= result.shares_sold
        plan, prior = result.plan, price
