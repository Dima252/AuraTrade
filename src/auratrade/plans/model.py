"""The position plan and what the EOD review reads and writes (archived README §7.3).

A ``PositionPlan`` is one version of a position's plan: the exit-menu choices made at entry
(``ExitPlan``), the levels code derives from them (the hard stop), and the milestones reached so
far. Every later version is tighter (``plans.versioning``). Sessions are counted from the entry
session, which is session 1.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from auratrade.config import EntryPolicy, ExitPlan, Regime, Strategy, TrailMA

Price = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]

# The 8-week hold replaces the trail with the 50-day (archived README §7.3).
HOLD_TRAIL = TrailMA.SMA_50

# Computed levels carry float noise (100 * 1.1 = 110.00000000000001); a value this close to a
# level has reached it.
_LEVEL_TOLERANCE = 1e-9

# Selling a fraction floors the share count; the tolerance keeps ⅓ written as 0.3333333333
# from flooring 30 shares to 9.
_SHARE_TOLERANCE = 1e-6


class HoldState(StrEnum):
    """The 8-week hold. It only moves forward: WATCHING -> ACTIVE -> ENDED."""

    OFF = "OFF"
    """Not in the plan."""
    WATCHING = "WATCHING"
    """In the plan; +20% within 15 sessions not reached yet."""
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"
    """Expired, not triggered in time, ended by a downgrade to CORRECTION, or ended by choice."""


class PlanChange(StrEnum):
    """Why a plan version was written."""

    ENTRY = "ENTRY"
    TRANCHE_2 = "TRANCHE_2"
    MILESTONE = "MILESTONE"
    PARTIAL_SALE = "PARTIAL_SALE"
    EIGHT_WEEK_HOLD = "EIGHT_WEEK_HOLD"
    REGIME_DOWNGRADE = "REGIME_DOWNGRADE"
    EARNINGS_CUSHION = "EARNINGS_CUSHION"
    TIGHTEN = "TIGHTEN"
    """A tightening chosen by the Portfolio Manager or the EOD LLM."""
    SPLIT = "SPLIT"


class Rule(StrEnum):
    """Plan rules in archived README §7.3 precedence order: the first is the highest."""

    HARD_STOP = "HARD_STOP"
    EARNINGS_CUSHION = "EARNINGS_CUSHION"
    HEAVY_VOLUME_REVERSAL = "HEAVY_VOLUME_REVERSAL"
    CLIMAX = "CLIMAX"
    # Not in the README list; placed with the climax sales by user decision (task 3.5a).
    LATE_STAGE = "LATE_STAGE"
    REGIME_DOWNGRADE = "REGIME_DOWNGRADE"
    EIGHT_WEEK_HOLD = "EIGHT_WEEK_HOLD"
    STRENGTH_PARTIAL = "STRENGTH_PARTIAL"
    TRAIL = "TRAIL"
    TIME_STOP = "TIME_STOP"


class EventKind(StrEnum):
    """``position_events.kind``."""

    EXIT = "EXIT"
    SALE = "SALE"
    STOP_RAISED = "STOP_RAISED"
    TRAIL_TIGHTENED = "TRAIL_TIGHTENED"
    HOLD_STARTED = "HOLD_STARTED"
    HOLD_ENDED = "HOLD_ENDED"
    REGIME_DOWNGRADE = "REGIME_DOWNGRADE"
    EARNINGS_CUSHION = "EARNINGS_CUSHION"
    CLIMAX_SIGNALS = "CLIMAX_SIGNALS"
    FALTERING_RS = "FALTERING_RS"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    """Closed below the pivot without a heavy-volume reversal exit: hold, no tranche 2; the
    LLM may exit early on days 1-3 (archived README §5.9)."""


TRAIL_ORDER: tuple[TrailMA, ...] = tuple(TrailMA)  # loosest first


def tighter_trail(trail: TrailMA) -> TrailMA:
    """One step tighter: 50 SMA -> 21 EMA -> 10-day; the 10-day stays."""
    return TRAIL_ORDER[min(TRAIL_ORDER.index(trail) + 1, len(TRAIL_ORDER) - 1)]


def stop_tick_up(price: float) -> float:
    """Round a stop level up to a valid tick ($0.01 at $1 and above, $0.0001 below).

    Up, so a level such as breakeven is never undercut by rounding.
    """
    tick = 0.01 if price >= 1 else 0.0001
    return round(math.ceil(round(price / tick, 6)) * tick, 4)


def reached(value: float, level: float) -> bool:
    """``value >= level``, ignoring float noise in a computed level."""
    return value >= level - _LEVEL_TOLERANCE


def shares_to_sell(shares: int, fraction: float) -> int:
    """``fraction`` of ``shares``, floored (never more than ``shares``)."""
    return min(shares, math.floor(shares * fraction + _SHARE_TOLERANCE))


class PositionPlan(BaseModel):
    """One version of a position's plan; stored whole in ``position_plans.plan``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: PositiveInt
    change: PlanChange
    strategy: Strategy
    entry: EntryPolicy
    exits: ExitPlan
    pivot: Price
    """The code pivot (a pullback's trigger; an EP's opening-range high)."""
    entry_price: Price
    """Average cost over the filled tranches."""
    initial_stop: Price
    hard_stop: Price
    """The broker's GTC stop level: initial -> breakeven -> round-trip level; never lowered."""
    hold: HoldState
    hold_trail: TrailMA | None = None
    """The trail while the 8-week hold is active: the 50-day, tightened by a downgrade."""
    hold_until_session: PositiveInt | None = None
    partial_taken: bool = False
    climax_half_taken: bool = False
    late_stage_taken: bool = False
    time_stop_halved: bool = False
    tranche2_filled: bool = False

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.initial_stop >= self.entry_price:
            raise ValueError("the initial stop must be below the entry price")
        if self.hard_stop < self.initial_stop:
            raise ValueError("the hard stop cannot be below the initial stop")
        if (self.hold is HoldState.OFF) == self.exits.eight_week_hold:
            raise ValueError("hold is OFF exactly when the plan has no 8-week hold")
        if (self.hold in (HoldState.WATCHING, HoldState.ACTIVE)) != (self.hold_trail is not None):
            raise ValueError("hold_trail is set exactly while the hold is watching or active")
        if (self.hold is HoldState.ACTIVE) != (self.hold_until_session is not None):
            raise ValueError("hold_until_session is set exactly while the hold is active")
        if self.tranche2_filled and self.entry.tranches != 2:
            raise ValueError("tranche 2 filled on a single-tranche entry")
        return self

    def evolve(self, **changes: object) -> Self:
        """A validated copy with ``changes`` applied (``model_copy`` would skip validation)."""
        return self.model_validate({**self.model_dump(), **changes})

    @property
    def risk_per_share(self) -> float:
        """R: entry price - initial stop."""
        return self.entry_price - self.initial_stop

    @property
    def breakeven(self) -> float:
        return stop_tick_up(self.entry_price)

    @property
    def trail(self) -> TrailMA:
        """The trail in force: the hold's while it is active, else the plan's."""
        if self.hold is HoldState.ACTIVE and self.hold_trail is not None:
            return self.hold_trail
        return self.exits.trail

    def gain_r(self, price: float) -> float:
        return (price - self.entry_price) / self.risk_per_share


def _finite_positive(name: str, value: float | None) -> None:
    if value is not None and not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite positive number, got {value}")


@dataclass(frozen=True, slots=True)
class Day:
    """What the EOD review knows about one held position's session.

    Close-based rules decide on ``price``: the 15:40 decision price live (12:40 on half days),
    the daily close in backtests. Volume is projected to the full day. Milestones that raise
    the hard stop (breakeven at +3R, the round-trip rule) read the session high, as the
    intraday monitor raises them when they're reached.
    """

    session: int
    """Sessions since entry; the entry session is 1."""
    price: float
    prior_close: float
    high: float
    low: float
    high_since_entry: float
    """Highest high from the entry session through this one."""
    volume: float
    avg_volume_50: float | None
    heaviest_down_volume: float | None
    """Heaviest down-day volume since the breakout, before this session (None: no down day)."""
    trail_levels: Mapping[TrailMA, float | None]
    """Each trailing average's level on this session (``plans.levels.trail_levels``)."""
    climax_signals: int = 0
    rs_line_at_low: bool = False
    """The RS line is at a 50-session low ("faltering RS")."""
    breakout_base_count: int | None = None
    """The stage of a base the stock broke out of this session, from base detection (2.2)."""
    regime_downgrade: Regime | None = None
    """The computed regime was downgraded to this since the last review (the caller reads the
    conservatism ladder)."""
    earnings_cushion_due: bool = False
    """This is the cushion's review before a known or estimated report (``plans.cushion``)."""

    def __post_init__(self) -> None:
        if self.session < 1:
            raise ValueError(f"session must be >= 1, got {self.session}")
        for name in ("price", "prior_close", "high", "low", "high_since_entry"):
            _finite_positive(name, getattr(self, name))
        _finite_positive("avg_volume_50", self.avg_volume_50)
        if not (math.isfinite(self.volume) and self.volume >= 0):
            raise ValueError(f"volume must be finite and >= 0, got {self.volume}")
        if self.climax_signals < 0:
            raise ValueError("climax_signals must be >= 0")


@dataclass(frozen=True, slots=True)
class Sale:
    rule: Rule
    shares: int


@dataclass(frozen=True, slots=True)
class Event:
    kind: EventKind
    rule: Rule | None = None
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Resolution:
    """What the plan does on one session (``plans.resolve``)."""

    plan: PositionPlan
    """The plan after the session: a new version when anything changed."""
    exit: Rule | None
    """The recorded reason when the whole position is closed."""
    sales: tuple[Sale, ...]
    """Partial sales in precedence order, each on the shares left by the one before."""
    events: tuple[Event, ...]
    block_tranche2: bool

    @property
    def shares_sold(self) -> int:
        return sum(sale.shares for sale in self.sales)
