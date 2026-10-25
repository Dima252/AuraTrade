"""Entry policies, the tranche-2 confirmation gate, and the plan written at entry (archived README
§7.1).

Triggers, limits and sizing are computed by ``execution`` (3.5b); this module says which kind
each strategy uses and decides whether tranche 2 is confirmed.
"""

import math
from dataclasses import dataclass

from auratrade.config import EntryPolicy, ExitPlan, GateKind, PlansConfig, Strategy
from auratrade.plans.model import HOLD_TRAIL, HoldState, PlanChange, PositionPlan, reached


class PlanError(ValueError):
    """A plan that can't be written: levels out of order or menu values out of bounds."""


def entry_policy(strategy: Strategy, config: PlansConfig) -> EntryPolicy:
    return config.entry[strategy]


def new_plan(
    strategy: Strategy,
    *,
    pivot: float,
    entry_price: float,
    initial_stop: float,
    config: PlansConfig,
    exits: ExitPlan | None = None,
) -> PositionPlan:
    """Version 1 of a position's plan.

    ``exits`` is the Portfolio Manager's menu choice; without one, the strategy's default plan.
    The levels come from code (the scan's pivot and structural stop, the fill); the menu values
    are checked against the bounds in ``risk.yaml``.
    """
    exits = exits if exits is not None else config.default_exit_plan(strategy)
    if problems := config.menu_violations(exits):
        raise PlanError("; ".join(problems))
    try:
        return PositionPlan(
            version=1,
            change=PlanChange.ENTRY,
            strategy=strategy,
            entry=entry_policy(strategy, config),
            exits=exits,
            pivot=pivot,
            entry_price=entry_price,
            initial_stop=initial_stop,
            hard_stop=initial_stop,
            hold=HoldState.WATCHING if exits.eight_week_hold else HoldState.OFF,
            hold_trail=HOLD_TRAIL if exits.eight_week_hold else None,
        )
    except ValueError as exc:
        raise PlanError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class GateInput:
    """The session tranche 2 or a close entry is decided on (the 15:40 decision price,
    projected volume)."""

    price: float
    level: float
    """The pivot (breakout gate), the trigger (pullback gate) or an EP's gap open."""
    day_high: float
    day_low: float
    projected_volume: float
    avg_volume_50: float | None
    rs_line_near_high: bool
    rs_line_at_low: bool
    tested_ma: float | None = None
    """The 10/21 EMA level a pullback turned off (pullback gate)."""


@dataclass(frozen=True, slots=True)
class GateResult:
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def confirmation_gate(kind: GateKind, x: GateInput, config: PlansConfig) -> GateResult:
    """Archived README §7.1 "Confirmation gate, per strategy". Missing data fails the check."""
    gate = config.confirmation_gates[kind]
    failures: list[str] = []
    if not all(math.isfinite(v) for v in (x.price, x.level, x.day_high, x.day_low)):
        return GateResult(("prices must be finite",))
    if not reached(x.price, x.level):
        failures.append("price is below the level")
    elif gate.max_above_level is not None and not reached(
        x.level * (1 + gate.max_above_level), x.price
    ):
        failures.append("price is above the max chase")
    day_range = x.day_high - x.day_low
    if day_range <= 0 or not reached((x.price - x.day_low) / day_range, gate.min_range_position):
        failures.append("price is not in the upper part of the day's range")
    if gate.min_volume_ratio is not None and (
        x.avg_volume_50 is None
        or x.avg_volume_50 <= 0
        or not reached(x.projected_volume, gate.min_volume_ratio * x.avg_volume_50)
    ):
        failures.append("projected volume is too light")
    if gate.rs_line_near_high and not x.rs_line_near_high:
        failures.append("the RS line is not at or near a high")
    if gate.rs_line_not_at_low and x.rs_line_at_low:
        failures.append("the RS line is at a 50-session low")
    if gate.above_tested_ma and (x.tested_ma is None or x.price <= x.tested_ma):
        failures.append("price is not above the tested moving average")
    return GateResult(tuple(failures))


def tranche2_gate(strategy: Strategy, x: GateInput, config: PlansConfig) -> GateResult:
    """The strategy's own gate; a single-tranche strategy never adds."""
    gate = entry_policy(strategy, config).gate
    if gate is None:
        return GateResult((f"{strategy} enters in a single tranche",))
    return confirmation_gate(gate, x, config)


def entry_gate(strategy: Strategy, x: GateInput, config: PlansConfig) -> GateResult:
    """A close entry's own gate at the decision time (#144); a policy without one never
    enters."""
    gate = entry_policy(strategy, config).gate
    if gate is None:
        return GateResult((f"{strategy} has no entry gate",))
    return confirmation_gate(gate, x, config)
