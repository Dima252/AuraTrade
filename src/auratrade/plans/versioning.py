"""Tighten-only plan versions (archived README §7.3 "Tighten-only", §8 "Plans are tighten-only").

Later decisions may raise the stop, choose a tighter trail, sell earlier or add an exit rule.
Anything else is rejected. The one widening allowed is one the plan committed to at entry: an
8-week hold activating (its 50-day trail). Splits rescale every level (a new version, not a
loosening), and tranche 2 updates the average entry price.

Every version the engine writes goes through ``check_tighten_only`` too, so a bug in a rule
can't loosen a plan either.
"""

import math
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from auratrade.config import (
    AnyPartial,
    ExitPlan,
    KullamagiPartial,
    MinerviniPartial,
    ONeilPartial,
    PlansConfig,
    StrengthPartial,
    TrailMA,
)
from auratrade.plans.model import (
    TRAIL_ORDER,
    HoldState,
    PlanChange,
    PositionPlan,
    Price,
    stop_tick_up,
)

# Exit rules that may be switched on after entry. The 8-week hold is not one of them: it widens
# the trail, so only a plan that opted in at entry can have it.
type Switch = Literal[
    "breakeven_after_partial",
    "breakeven_at_r",
    "round_trip",
    "climax_sales",
    "heavy_volume_reversal",
    "late_stage_sale",
    "time_stop",
    "earnings_cushion",
    "regime_tightening",
]
SWITCHES: tuple[Switch, ...] = get_args(Switch.__value__)

_FIXED = ("strategy", "entry", "pivot", "entry_price", "initial_stop", "tranche2_filled")
_MILESTONES = ("partial_taken", "climax_half_taken", "late_stage_taken", "time_stop_halved")
_HOLD_STEPS = {
    (HoldState.WATCHING, HoldState.ACTIVE),
    (HoldState.WATCHING, HoldState.ENDED),
    (HoldState.ACTIVE, HoldState.ENDED),
}


class LooseningError(ValueError):
    """A proposed plan version would loosen the plan."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = tuple(violations)


def _rank(trail: TrailMA) -> int:
    return TRAIL_ORDER.index(trail)


def _partial_loosening(old: AnyPartial | None, new: AnyPartial | None) -> list[str]:
    if old is None or old == new:
        return []  # adding a partial sells earlier
    if new is None:
        return ["the strength partial cannot be removed"]
    if new.kind != old.kind:
        return [f"the strength partial cannot change from {old.kind} to {new.kind}"]
    out = []
    if new.fraction < old.fraction:
        out.append(f"the strength partial cannot sell less ({old.fraction} -> {new.fraction})")
    match old, new:
        case KullamagiPartial(), KullamagiPartial():
            later = new.after_sessions > old.after_sessions
        case ONeilPartial(), ONeilPartial():
            later = new.gain > old.gain
        case MinerviniPartial(), MinerviniPartial():
            later = new.r_multiple > old.r_multiple
        case _:  # pragma: no cover - the kinds are equal here
            later = False
    if later:
        out.append("the strength partial cannot sell later")
    return out


def loosenings(old: PositionPlan, new: PositionPlan) -> list[str]:
    """Every way ``new`` is looser than ``old`` (empty when it's at least as tight)."""
    out = [f"{name} cannot change" for name in _FIXED if getattr(old, name) != getattr(new, name)]
    if new.hard_stop < old.hard_stop:
        out.append(f"the hard stop cannot be lowered ({old.hard_stop} -> {new.hard_stop})")
    if _rank(new.exits.trail) < _rank(old.exits.trail):
        out.append(f"the trail cannot widen ({old.exits.trail} -> {new.exits.trail})")
    out += _partial_loosening(old.exits.partial, new.exits.partial)
    out += [
        f"{name} cannot be switched off"
        for name in SWITCHES
        if getattr(old.exits, name) and not getattr(new.exits, name)
    ]
    if new.exits.eight_week_hold and not old.exits.eight_week_hold:
        out.append("an 8-week hold can only be chosen at entry")
    out += [
        f"{name} cannot be undone"
        for name in _MILESTONES
        if getattr(old, name) > getattr(new, name)
    ]

    if old.hold != new.hold and (old.hold, new.hold) not in _HOLD_STEPS:
        out.append(f"the 8-week hold cannot go from {old.hold} to {new.hold}")
    if (
        old.hold is HoldState.ACTIVE
        and new.hold is HoldState.ACTIVE
        and (new.hold_until_session or 0) > (old.hold_until_session or 0)
    ):
        out.append("the 8-week hold cannot be extended")
    if old.hold_trail is None and new.hold_trail is not None:
        out.append("an 8-week-hold trail can only be set at entry")
    if (
        old.hold_trail is not None
        and new.hold_trail is not None
        and _rank(new.hold_trail) < _rank(old.hold_trail)
    ):
        out.append(f"the 8-week-hold trail cannot widen ({old.hold_trail} -> {new.hold_trail})")
    return out


def check_tighten_only(old: PositionPlan, new: PositionPlan) -> None:
    if violations := loosenings(old, new):
        raise LooseningError(violations)


def next_version(old: PositionPlan, new: PositionPlan, change: PlanChange) -> PositionPlan:
    """``new`` as the version after ``old``; rejected if it loosens anything."""
    check_tighten_only(old, new)
    return new.evolve(version=old.version + 1, change=change)


class Tightening(BaseModel):
    """A tightening requested by the Portfolio Manager or the EOD LLM.

    The only price it carries is ``hard_stop``, and that is checked: above the current stop
    and below the current price (a stop at or above the market is an exit; ask for one).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hard_stop: Price | None = None
    trail: TrailMA | None = None
    partial: StrengthPartial | None = None
    switch_on: frozenset[Switch] = Field(default_factory=frozenset)
    end_eight_week_hold: bool = False


def tighten(
    plan: PositionPlan, request: Tightening, *, price: float, config: PlansConfig
) -> PositionPlan:
    """The next version with ``request`` applied, or ``LooseningError``."""
    if not (math.isfinite(price) and price > 0):
        raise ValueError(f"price must be a finite positive number, got {price}")
    problems: list[str] = []
    update: dict[str, object] = {}
    exits_update: dict[str, object] = dict.fromkeys(request.switch_on, True)

    if request.hard_stop is not None:
        stop = stop_tick_up(request.hard_stop)
        if stop >= price:
            problems.append(f"a stop at {stop} is not below the price {price}; exit instead")
        update["hard_stop"] = stop
    if request.trail is not None:
        exits_update["trail"] = request.trail
    if "partial" in request.model_fields_set:
        exits_update["partial"] = request.partial
    if request.end_eight_week_hold and plan.hold in (HoldState.WATCHING, HoldState.ACTIVE):
        update |= {"hold": HoldState.ENDED, "hold_trail": None, "hold_until_session": None}

    exits = ExitPlan.model_validate({**plan.exits.model_dump(), **exits_update})
    problems += config.menu_violations(exits)
    if problems:
        raise LooseningError(problems)
    return next_version(plan, plan.evolve(**update, exits=exits), PlanChange.TIGHTEN)


def with_tranche2(plan: PositionPlan, *, entry_price: float) -> PositionPlan:
    """The version after tranche 2 fills: the new average entry price. The stop is unchanged
    (the broker stop is replaced to cover the full quantity at the same level)."""
    if plan.entry.tranches != 2 or plan.tranche2_filled:
        raise ValueError("tranche 2 is only added once, to a two-tranche entry")
    if not (math.isfinite(entry_price) and plan.initial_stop < entry_price):
        raise ValueError(f"entry price {entry_price} must be above the initial stop")
    return plan.evolve(
        version=plan.version + 1,
        change=PlanChange.TRANCHE_2,
        entry_price=entry_price,
        tranche2_filled=True,
    )


def scale_for_split(plan: PositionPlan, ratio: float) -> PositionPlan:
    """The version for a split of ``ratio`` new shares per old share (0.1 = 1-for-10 reverse):
    every price level divided by it (README §5.7). The hard stop is rounded up to a tick."""
    if not (math.isfinite(ratio) and ratio > 0):
        raise ValueError(f"split ratio must be a finite positive number, got {ratio}")
    return plan.evolve(
        version=plan.version + 1,
        change=PlanChange.SPLIT,
        pivot=plan.pivot / ratio,
        entry_price=plan.entry_price / ratio,
        initial_stop=plan.initial_stop / ratio,
        hard_stop=stop_tick_up(plan.hard_stop / ratio),
    )
