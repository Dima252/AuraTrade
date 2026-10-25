"""The exit menu's rules (archived README §7.3), each as a pure check on one session.

Suppression and ordering are not decided here: ``plans.resolve`` applies the precedence.
"""

from enum import StrEnum

from auratrade.config import (
    ClimaxConfig,
    KullamagiPartial,
    MinerviniPartial,
    ONeilPartial,
    PlansConfig,
)
from auratrade.plans.model import Day, HoldState, PositionPlan, reached


def strength_partial(plan: PositionPlan, day: Day, config: PlansConfig) -> bool:
    """The plan's strength partial fires (once), on the decision price.

    (a) Kullamägi: at the EOD review of session N, if >= +1.5R. (b) O'Neil: at +20-25%.
    (c) Minervini: at +2-3R.
    """
    if plan.partial_taken:
        return False
    entry, r = plan.entry_price, plan.risk_per_share
    match plan.exits.partial:
        case KullamagiPartial(after_sessions=n):
            min_r = config.partial_menus.KULLAMAGI.min_r
            return day.session == n and reached(day.price, entry + min_r * r)
        case ONeilPartial(gain=gain):
            return reached(day.price, entry * (1 + gain))
        case MinerviniPartial(r_multiple=multiple):
            return reached(day.price, entry + multiple * r)
        case _:  # no strength partial
            return False


class ClimaxSale(StrEnum):
    HALF = "HALF"
    ALL = "ALL"


def climax_sale(plan: PositionPlan, day: Day, climax: ClimaxConfig) -> ClimaxSale | None:
    """>= 2 climax signals -> sell ½ (once); >= 3 -> sell all."""
    if not plan.exits.climax_sales:
        return None
    if day.climax_signals >= climax.sell_all_signals:
        return ClimaxSale.ALL
    if day.climax_signals >= climax.sell_half_signals and not plan.climax_half_taken:
        return ClimaxSale.HALF
    return None


def late_stage_sale(plan: PositionPlan, day: Day, config: PlansConfig) -> bool:
    """A breakout from a 4th-stage base or later: sell ½ into strength (once)."""
    return (
        plan.exits.late_stage_sale
        and not plan.late_stage_taken
        and day.breakout_base_count is not None
        and day.breakout_base_count >= config.late_stage_sale.min_base_count
    )


def trail_exit(plan: PositionPlan, day: Day) -> bool:
    """The first decision price below the trail in force. No level (still warming up): no exit;
    the hard stop still protects the position."""
    level = day.trail_levels.get(plan.trail)
    return level is not None and day.price < level


def milestone_stop(plan: PositionPlan, day: Day, config: PlansConfig) -> float:
    """The hard stop after this session's milestones; never lower than it was.

    Breakeven at +3R and the round-trip rule at +10% read the highest high since entry (the
    intraday monitor raises the stop when they're reached). An active 8-week hold keeps the
    stop at breakeven or above.
    """
    entry, r = plan.entry_price, plan.risk_per_share
    high = day.high_since_entry
    at_r = plan.exits.breakeven_at_r and reached(high, entry + config.breakeven_at_r * r)
    at_gain = plan.exits.round_trip and reached(high, entry * (1 + config.round_trip_gain))
    if at_r or at_gain or plan.hold is HoldState.ACTIVE:
        return max(plan.hard_stop, plan.breakeven)
    return plan.hard_stop
