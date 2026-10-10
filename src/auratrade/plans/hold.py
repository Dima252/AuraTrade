"""The 8-week hold (README §7.3, IBD) and regime-downgrade tightening (§6, §7.3, #84).

Both change the plan without selling. Their results are folded into one new plan version by
``plans.resolve``, which also checks it is no looser.
"""

from auratrade.config import PlansConfig, Regime, TrailMA
from auratrade.plans.model import (
    Day,
    Event,
    EventKind,
    HoldState,
    PositionPlan,
    Rule,
    reached,
    tighter_trail,
)


def update_hold(
    plan: PositionPlan, day: Day, config: PlansConfig
) -> tuple[PositionPlan, list[Event]]:
    """Start the hold on +20% (decision price) within 15 sessions of the breakout; end it when
    that window passes without it, or after session 40.

    While active, profit-target partials are suspended, the trail is the 50-day (or tighter
    after a downgrade) and the hard stop is at breakeven or above.
    """
    hold = config.eight_week_hold
    if plan.hold is HoldState.WATCHING:
        target = plan.entry_price * (1 + hold.min_gain)
        if day.session <= hold.within_sessions and reached(day.price, target):
            started = plan.evolve(hold=HoldState.ACTIVE, hold_until_session=hold.hold_sessions)
            return started, [
                Event(EventKind.HOLD_STARTED, Rule.EIGHT_WEEK_HOLD, {"until": hold.hold_sessions})
            ]
        if day.session >= hold.within_sessions:
            return _end(plan, "not triggered in time")
    if (
        plan.hold is HoldState.ACTIVE
        and plan.hold_until_session is not None
        and day.session > plan.hold_until_session
    ):
        return _end(plan, "expired")
    return plan, []


def _end(plan: PositionPlan, reason: str) -> tuple[PositionPlan, list[Event]]:
    ended = plan.evolve(hold=HoldState.ENDED, hold_trail=None, hold_until_session=None)
    return ended, [Event(EventKind.HOLD_ENDED, Rule.EIGHT_WEEK_HOLD, {"reason": reason})]


def tighten_for_downgrade(
    plan: PositionPlan, downgraded_to: Regime | None
) -> tuple[PositionPlan, list[Event]]:
    """On a downgrade of the computed regime: to ``CORRECTION`` the trail becomes the 10-day and
    any 8-week hold ends; to anything else the trail tightens one step, the hold's trail
    included (user decision, task 3.5a). Every downgrade raises an event, even when the safety
    rule is switched off."""
    if downgraded_to is None:
        return plan, []
    events = [Event(EventKind.REGIME_DOWNGRADE, Rule.REGIME_DOWNGRADE, {"to": downgraded_to})]
    if not plan.exits.regime_tightening:
        return plan, events

    exits = plan.exits
    changes: dict[str, object] = {}
    if downgraded_to is Regime.CORRECTION:
        trail = TrailMA.TEN_DAY
        if plan.hold in (HoldState.WATCHING, HoldState.ACTIVE):
            changes = {"hold": HoldState.ENDED, "hold_trail": None, "hold_until_session": None}
            events.append(
                Event(EventKind.HOLD_ENDED, Rule.REGIME_DOWNGRADE, {"reason": "CORRECTION"})
            )
    else:
        trail = tighter_trail(exits.trail)
        if plan.hold_trail is not None:
            changes = {"hold_trail": tighter_trail(plan.hold_trail)}
    tightened = plan.evolve(exits=exits.model_copy(update={"trail": trail}), **changes)
    if tightened.trail != plan.trail or tightened.exits.trail != exits.trail:
        events.append(
            Event(
                EventKind.TRAIL_TIGHTENED,
                Rule.REGIME_DOWNGRADE,
                {"from": plan.trail, "to": tightened.trail},
            )
        )
    return tightened, events
