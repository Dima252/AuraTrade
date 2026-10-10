"""Early-behaviour rules for a position's first sessions (README §7.2).

Each fires only if it is in the position's plan: the heavy-volume reversal in the O'Neil set,
time stops on backtest evidence or by Portfolio Manager choice. The light-volume dip below the
pivot always applies, and only ever blocks tranche 2.
"""

from auratrade.config import PlansConfig
from auratrade.plans.model import Day, PositionPlan, reached


def _early(day: Day, config: PlansConfig) -> bool:
    return day.session <= config.early_sessions


def heavy_volume_reversal(plan: PositionPlan, day: Day, config: PlansConfig) -> bool:
    """A down day (below the prior close) closing below the pivot on >= 1.5x the 50-day
    average volume, the heaviest down-day volume since the breakout. Exit.

    An EP's gap day closes far above the prior close, so it never counts (#87).
    """
    if not (plan.exits.heavy_volume_reversal and _early(day, config)):
        return False
    if day.avg_volume_50 is None:
        return False
    return (
        day.price < day.prior_close
        and day.price < plan.pivot
        and reached(day.volume, config.heavy_volume_reversal.min_volume_ratio * day.avg_volume_50)
        and (day.heaviest_down_volume is None or day.volume > day.heaviest_down_volume)
    )


def below_pivot(plan: PositionPlan, day: Day, config: PlansConfig) -> bool:
    """Closed below the pivot in the early window. Unless the heavy-volume reversal exits, the
    position is held and tranche 2 is blocked (IBD: many big winners dipped below the buy point
    first)."""
    return _early(day, config) and day.price < plan.pivot


def _no_progress(plan: PositionPlan, day: Day, config: PlansConfig) -> bool:
    target = plan.entry_price + config.time_stop.min_r * plan.risk_per_share
    return not reached(day.high_since_entry, target)


def time_stop_halve(plan: PositionPlan, day: Day, config: PlansConfig) -> bool:
    """High < +1R by session 10: halve (once)."""
    return (
        plan.exits.time_stop
        and not plan.time_stop_halved
        and day.session >= config.time_stop.halve_session
        and _no_progress(plan, day, config)
    )


def time_stop_exit(plan: PositionPlan, day: Day, config: PlansConfig) -> bool:
    """Still high < +1R by session 20: exit."""
    return (
        plan.exits.time_stop
        and day.session >= config.time_stop.exit_session
        and _no_progress(plan, day, config)
    )
