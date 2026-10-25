"""Rule precedence for one session of one held position (archived README §7.3), in one place.

The order is ``Rule``'s: hard stop, earnings cushion, early-behaviour exits, climax sales (with
the late-stage sale), regime-downgrade tightening, 8-week-hold constraints, strength partials,
trailing-MA exit, time stop. "Applicable" means in the position's plan, plus the safety rules.

- If any applicable rule closes the whole position, the highest one is the recorded exit reason
  and nothing else runs.
- Otherwise every applicable partial sale runs in that order, each on the shares left after the
  one before; together they never sell the whole position.
- Constraints suppress the rules they name: an active 8-week hold suppresses strength partials
  and the normal trail (its own 50-day trail replaces it).

The plan-changing rules (regime-downgrade tightening, the 8-week hold, the stop-raising
milestones) are applied first, so the session's exits see the tightened plan. A stop raised to a
level the price is already through sells at once, as the order layer does (§5.7).

The EOD jobs, the simulator, the baseline and the verdict scorecard all call ``resolve``; none of
them re-implements it.
"""

from auratrade.config import RiskConfig
from auratrade.plans.cushion import CushionAction, cushion_action
from auratrade.plans.early import (
    below_pivot,
    heavy_volume_reversal,
    time_stop_exit,
    time_stop_halve,
)
from auratrade.plans.exits import (
    ClimaxSale,
    climax_sale,
    late_stage_sale,
    milestone_stop,
    strength_partial,
    trail_exit,
)
from auratrade.plans.hold import tighten_for_downgrade, update_hold
from auratrade.plans.model import (
    Day,
    Event,
    EventKind,
    HoldState,
    PlanChange,
    PositionPlan,
    Resolution,
    Rule,
    Sale,
    shares_to_sell,
)
from auratrade.plans.versioning import next_version


def resolve(plan: PositionPlan, day: Day, *, shares: int, config: RiskConfig) -> Resolution:
    """What ``plan`` does on ``day`` for a position of ``shares`` shares."""
    if shares < 1:
        raise ValueError(f"shares must be >= 1, got {shares}")
    plans = config.plans

    # 1. The broker's stop (in backtests: the session low reaching it).
    if day.low <= plan.hard_stop:
        return _closed(plan, plan, [], Rule.HARD_STOP, [], day)

    # Plan changes that sell nothing, in precedence order.
    changes: list[PlanChange] = []
    events: list[Event] = []
    p, found = tighten_for_downgrade(plan, day.regime_downgrade)
    _note(changes, events, plan, p, found, PlanChange.REGIME_DOWNGRADE)
    before = p
    p, found = update_hold(p, day, plans)
    _note(changes, events, before, p, found, PlanChange.EIGHT_WEEK_HOLD)
    before = p
    p = p.evolve(hard_stop=milestone_stop(p, day, plans))
    _note(changes, events, before, p, [], PlanChange.MILESTONE)

    cushion = (
        cushion_action(p.gain_r(day.price), plans.earnings_cushion)
        if day.earnings_cushion_due and p.exits.earnings_cushion
        else None
    )
    climax = climax_sale(p, day, config.climax)

    full_exits = (
        (Rule.HARD_STOP, day.price <= p.hard_stop),
        (Rule.EARNINGS_CUSHION, cushion is CushionAction.EXIT),
        (Rule.HEAVY_VOLUME_REVERSAL, heavy_volume_reversal(p, day, plans)),
        (Rule.CLIMAX, climax is ClimaxSale.ALL),
        (Rule.TRAIL, trail_exit(p, day)),
        (Rule.TIME_STOP, time_stop_exit(p, day, plans)),
    )
    for rule, fires in full_exits:
        if fires:
            return _closed(plan, p, changes, rule, events, day)

    # Partial sales, in order, each on what is left.
    remaining = shares
    sales: list[Sale] = []

    def sell(rule: Rule, fraction: float) -> None:
        nonlocal remaining
        if n := shares_to_sell(remaining, fraction):
            sales.append(Sale(rule, n))
            remaining -= n

    before = p
    if cushion is not None:
        if cushion is CushionAction.TRIM:
            sell(Rule.EARNINGS_CUSHION, plans.earnings_cushion.trim_fraction)
        p = p.evolve(hard_stop=max(p.hard_stop, p.breakeven))
        events.append(Event(EventKind.EARNINGS_CUSHION, Rule.EARNINGS_CUSHION, {"action": cushion}))
    _note(changes, events, before, p, [], PlanChange.EARNINGS_CUSHION)

    before = p
    profit_taken = False
    if climax is ClimaxSale.HALF:
        sell(Rule.CLIMAX, plans.climax_sell_fraction)
        p, profit_taken = p.evolve(climax_half_taken=True), True
    if late_stage_sale(p, day, plans):
        sell(Rule.LATE_STAGE, plans.late_stage_sale.sell_fraction)
        p, profit_taken = p.evolve(late_stage_taken=True), True
    partial = p.exits.partial
    if partial is not None and p.hold is not HoldState.ACTIVE and strength_partial(p, day, plans):
        sell(Rule.STRENGTH_PARTIAL, partial.fraction)
        p, profit_taken = p.evolve(partial_taken=True), True
    if time_stop_halve(p, day, plans):
        sell(Rule.TIME_STOP, plans.time_stop.halve_fraction)
        p = p.evolve(time_stop_halved=True)
    if profit_taken and p.exits.breakeven_after_partial:
        p = p.evolve(hard_stop=max(p.hard_stop, p.breakeven))
    _note(changes, events, before, p, [], PlanChange.PARTIAL_SALE)

    events += [Event(EventKind.SALE, sale.rule, {"shares": sale.shares}) for sale in sales]
    if day.climax_signals:
        events.append(Event(EventKind.CLIMAX_SIGNALS, Rule.CLIMAX, {"count": day.climax_signals}))
    if day.rs_line_at_low:
        events.append(Event(EventKind.FALTERING_RS))
    block_tranche2 = below_pivot(p, day, plans)
    if block_tranche2:
        events.append(Event(EventKind.FAILED_BREAKOUT, details={"session": day.session}))

    return Resolution(
        plan=_version(plan, p, changes),
        exit=None,
        sales=tuple(sales),
        events=tuple(events),
        block_tranche2=block_tranche2,
    )


def _note(
    changes: list[PlanChange],
    events: list[Event],
    before: PositionPlan,
    after: PositionPlan,
    found: list[Event],
    change: PlanChange,
) -> None:
    events += found
    if after.hard_stop > before.hard_stop:
        events.append(
            Event(EventKind.STOP_RAISED, details={"from": before.hard_stop, "to": after.hard_stop})
        )
    if after != before:
        changes.append(change)


def _version(original: PositionPlan, plan: PositionPlan, changes: list[PlanChange]) -> PositionPlan:
    """One new version for the session's changes (checked tighten-only), or the original."""
    return next_version(original, plan, changes[0]) if changes else original


def _closed(
    original: PositionPlan,
    plan: PositionPlan,
    changes: list[PlanChange],
    rule: Rule,
    events: list[Event],
    day: Day,
) -> Resolution:
    return Resolution(
        plan=_version(original, plan, changes),
        exit=rule,
        sales=(),
        events=(*events, Event(EventKind.EXIT, rule, {"price": day.price})),
        block_tranche2=True,
    )
