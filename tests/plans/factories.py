"""Hand-built plans and sessions for the plan-engine tests.

The default position: pivot 100, entry 100, initial stop 92, so R = 8 and +1R = 108,
+2R = 116, +3R = 124, +10% = 110, +20% = 120.
"""

from typing import Any

from auratrade.config import ExitPlan, Strategy, TrailMA, load_risk_config
from auratrade.plans.entry import new_plan
from auratrade.plans.model import Day, PositionPlan

RISK = load_risk_config()
PLANS = RISK.plans

# Trailing averages far below the price: no trail exit unless a test sets one.
LOW_TRAILS: dict[TrailMA, float | None] = dict.fromkeys(TrailMA, 50.0)


def exits_for(strategy: Strategy, **changes: Any) -> ExitPlan:
    return ExitPlan.model_validate({**PLANS.default_exit_plan(strategy).model_dump(), **changes})


def make_plan(
    strategy: Strategy = Strategy.VCP_BREAKOUT,
    *,
    pivot: float = 100.0,
    entry: float = 100.0,
    stop: float = 92.0,
    **exit_changes: Any,
) -> PositionPlan:
    return new_plan(
        strategy,
        pivot=pivot,
        entry_price=entry,
        initial_stop=stop,
        config=PLANS,
        exits=exits_for(strategy, **exit_changes),
    )


def make_day(
    session: int = 5,
    price: float = 101.0,
    *,
    prior_close: float | None = None,
    high: float | None = None,
    low: float | None = None,
    high_since_entry: float | None = None,
    volume: float = 1000.0,
    avg_volume_50: float | None = 1000.0,
    trails: dict[TrailMA, float | None] | None = None,
    **kwargs: Any,
) -> Day:
    high = high if high is not None else price
    return Day(
        session=session,
        price=price,
        prior_close=prior_close if prior_close is not None else price,
        high=high,
        low=low if low is not None else price * 0.99,
        high_since_entry=high_since_entry if high_since_entry is not None else high,
        volume=volume,
        avg_volume_50=avg_volume_50,
        heaviest_down_volume=kwargs.pop("heaviest_down_volume", None),
        trail_levels={**LOW_TRAILS, **(trails or {})},
        **kwargs,
    )
