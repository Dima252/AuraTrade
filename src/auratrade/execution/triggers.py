"""Entry triggers, limits and the 09:36 order type (README §7.1), pure.

- **Opening range:** the high and low of the first ``opening_range_minutes`` 1-minute bars
  (09:30-09:34). From IEX live; when IEX printed nothing in it, the caller passes delayed-SIP
  bars at 09:51 instead (#109). The function doesn't care which feed.
- **Trigger:** a pivot strategy triggers at its pivot, or at the opening-range high whenever the
  range traded above the pivot (a stock that broke out before 09:36 would otherwise have a
  trigger below the market). Momentum triggers at max(consolidation high, opening-range high);
  an EP at its opening-range high, which is also its pivot.
- **Max chase:** a trigger more than 5% above the **code** pivot is skipped.
- **Limit** (the worst fill, which sizing uses): min(trigger x (1 + min(2%, ½ x ADR%)), code
  pivot x 1.05).
- **Order type at 09:36:** below the trigger, a buy-stop-limit; past the trigger but at or below
  the limit, a limit buy at the limit (never a market buy); above the limit, skipped.
- **Low-of-day stop** (momentum, EP): the session low up to the order time, never below the
  setup's invalidation level.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, assert_never

from auratrade.config import EntriesConfig, TriggerKind
from auratrade.execution.prices import round_down, round_up
from auratrade.plans.model import reached, stop_tick_up


class MinuteBarLike(Protocol):
    @property
    def start(self) -> datetime: ...
    @property
    def high(self) -> float: ...
    @property
    def low(self) -> float: ...


@dataclass(frozen=True, slots=True)
class OpeningRange:
    high: float
    low: float


def opening_range(
    bars: Sequence[MinuteBarLike], *, open_at: datetime, minutes: int
) -> OpeningRange | None:
    """The range of the bars starting in [open_at, open_at + minutes); None if there are none
    (thin IEX: fall back to delayed SIP, #109)."""
    end = open_at + timedelta(minutes=minutes)
    inside = [b for b in bars if open_at <= b.start < end]
    if not inside:
        return None
    return OpeningRange(high=max(b.high for b in inside), low=min(b.low for b in inside))


def session_low(
    bars: Sequence[MinuteBarLike], *, open_at: datetime, until: datetime
) -> float | None:
    """The low of the bars starting in [open_at, until): the low of the day at the order time."""
    lows = [b.low for b in bars if open_at <= b.start < until]
    return min(lows) if lows else None


def trigger_level(kind: TriggerKind, level: float, opening: OpeningRange | None) -> float | None:
    """The raw trigger (before tick rounding); None when it needs an opening range there isn't.

    ``level`` is the pivot (a pullback's prior-session high) or the consolidation high.
    """
    match kind:
        case TriggerKind.PIVOT:
            if opening is not None and opening.high > level:
                return opening.high
            return level
        case TriggerKind.CONSOLIDATION_OR_RANGE_HIGH:
            return None if opening is None else max(level, opening.high)
        case TriggerKind.OPENING_RANGE_HIGH:
            return None if opening is None else opening.high
        case _:  # pragma: no cover - every trigger kind is handled above
            assert_never(kind)


def chased_too_far(trigger: float, code_pivot: float, max_chase: float) -> bool:
    """README §7.1: skip a trigger above the code pivot x (1 + max chase)."""
    return not reached(code_pivot * (1 + max_chase), trigger)


def limit_price(
    trigger: float, *, adr: float, code_pivot: float, max_chase: float, config: EntriesConfig
) -> float:
    """min(trigger x (1 + min(2%, ½ x ADR%)), code pivot x 1.05), rounded down to a tick.

    For an EP pass its opening-range high as ``code_pivot``: the cap then never binds.
    """
    above = min(config.limit_max_above_trigger, config.limit_adr_fraction * adr)
    return round_down(min(trigger * (1 + above), code_pivot * (1 + max_chase)))


class SkipReason(StrEnum):
    INVALID = "INVALID"
    NO_OPENING_RANGE = "NO_OPENING_RANGE"
    MAX_CHASE = "MAX_CHASE"
    LIMIT_BELOW_TRIGGER = "LIMIT_BELOW_TRIGGER"
    ABOVE_LIMIT = "ABOVE_LIMIT"
    NO_PRICE = "NO_PRICE"


@dataclass(frozen=True, slots=True)
class Skip:
    reason: SkipReason
    message: str


@dataclass(frozen=True, slots=True)
class EntryLevels:
    trigger: float
    """Rounded up to a tick."""
    limit: float
    """Rounded down to a tick; the worst fill sizing uses."""


def entry_levels(
    kind: TriggerKind,
    *,
    level: float,
    code_pivot: float,
    adr: float,
    opening: OpeningRange | None,
    max_chase: float,
    config: EntriesConfig,
) -> EntryLevels | Skip:
    """The trigger and limit, or why the entry is skipped.

    ``level`` is the approved pivot (the Analyst's may differ from the code pivot by ±3%) or the
    consolidation high; ``code_pivot`` bounds the chase and the limit. For an EP both are
    ignored in favour of its opening-range high.
    """
    values = (level, code_pivot, adr)
    if not all(math.isfinite(v) and v > 0 for v in values):
        return Skip(SkipReason.INVALID, "levels must be finite and positive")
    raw = trigger_level(kind, level, opening)
    if raw is None:
        return Skip(SkipReason.NO_OPENING_RANGE, "no opening range")
    if kind is TriggerKind.OPENING_RANGE_HIGH:
        code_pivot = raw
    elif chased_too_far(raw, code_pivot, max_chase):
        return Skip(
            SkipReason.MAX_CHASE,
            f"trigger {raw:.2f} is more than {max_chase:.0%} above the pivot {code_pivot:.2f}",
        )
    trigger = round_up(raw)
    limit = limit_price(raw, adr=adr, code_pivot=code_pivot, max_chase=max_chase, config=config)
    if limit < trigger:
        return Skip(
            SkipReason.LIMIT_BELOW_TRIGGER, f"limit {limit:.2f} is below the trigger {trigger:.2f}"
        )
    return EntryLevels(trigger=trigger, limit=limit)


def low_of_day_stop(low: float, invalidation: float) -> float:
    """Momentum and EP stops: the low of the day at 09:36, never below the setup's
    invalidation level; rounded up to a tick."""
    return stop_tick_up(max(low, invalidation))


class OrderKind(StrEnum):
    STOP_LIMIT = "STOP_LIMIT"
    """A buy-stop-limit at the trigger with the limit."""
    LIMIT = "LIMIT"
    """Already past the trigger: a limit buy at the limit."""


def order_decision(price: float | None, levels: EntryLevels) -> OrderKind | Skip:
    """README §7.1 "Already through the trigger at 09:36", on the last price."""
    if price is None or not (math.isfinite(price) and price > 0):
        return Skip(SkipReason.NO_PRICE, "no price")
    if price > levels.limit:
        return Skip(SkipReason.ABOVE_LIMIT, f"price {price:.2f} above the limit {levels.limit:.2f}")
    if price > levels.trigger:
        return OrderKind.LIMIT
    return OrderKind.STOP_LIMIT
