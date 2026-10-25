"""Hard-stop and soft-rule levels (archived README §7.3 "Execution split").

The hard stop rests at the broker; the soft rules are decided by code at the EOD review on the
decision price. ``plan_levels`` is what ``position_plans`` stores as a version's current levels,
and what the intraday monitor watches for stop-raising milestones.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from auratrade.config import (
    AverageKind,
    KullamagiPartial,
    MinerviniPartial,
    ONeilPartial,
    PlansConfig,
    TrailMA,
)
from auratrade.indicators.moving_averages import ema, sma
from auratrade.plans.model import HoldState, PositionPlan


def trail_levels(closes: Sequence[float], config: PlansConfig) -> dict[TrailMA, float | None]:
    """Each trailing average as of the last close; None while it warms up.

    At the EOD review the last value is the decision price standing in for today's close; in
    backtests it is the close.
    """
    out: dict[TrailMA, float | None] = {}
    for trail, spec in config.trails.items():
        average = sma if spec.kind is AverageKind.SMA else ema
        series = average(closes, spec.window)
        out[trail] = series[-1] if series else None
    return out


@dataclass(frozen=True, slots=True)
class PlanLevels:
    hard_stop: float
    breakeven: float
    trail: TrailMA
    trail_level: float | None
    """A decision price below this exits (soft rule)."""
    breakeven_trigger: float | None
    """A session high at or above this raises the stop to breakeven (+3R)."""
    round_trip_trigger: float | None
    """A session high at or above this raises the stop to breakeven (+10%)."""
    partial_trigger: float | None
    """A decision price at or above this fires the strength partial."""


def plan_levels(
    plan: PositionPlan, trails: dict[TrailMA, float | None], config: PlansConfig
) -> PlanLevels:
    entry, r = plan.entry_price, plan.risk_per_share
    below_breakeven = plan.hard_stop < plan.breakeven

    partial_trigger: float | None = None
    if not plan.partial_taken and plan.hold is not HoldState.ACTIVE:
        match plan.exits.partial:
            case KullamagiPartial():
                partial_trigger = entry + config.partial_menus.KULLAMAGI.min_r * r
            case ONeilPartial(gain=gain):
                partial_trigger = entry * (1 + gain)
            case MinerviniPartial(r_multiple=multiple):
                partial_trigger = entry + multiple * r
            case _:  # no strength partial
                pass

    return PlanLevels(
        hard_stop=plan.hard_stop,
        breakeven=plan.breakeven,
        trail=plan.trail,
        trail_level=trails.get(plan.trail),
        breakeven_trigger=(
            entry + config.breakeven_at_r * r
            if plan.exits.breakeven_at_r and below_breakeven
            else None
        ),
        round_trip_trigger=(
            entry * (1 + config.round_trip_gain)
            if plan.exits.round_trip and below_breakeven
            else None
        ),
        partial_trigger=partial_trigger,
    )
