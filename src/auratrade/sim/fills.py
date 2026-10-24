"""The fill model (README §5.12), pure: one bar at a time, minute or daily.

- **Buy-stop-limit:** triggers on a bar that trades at or above the trigger, and fills at
  max(trigger, the bar's open) plus slippage **if that is at or below the limit**. Otherwise it
  rests as a limit order and fills at the limit only on a later bar that trades at or below it;
  a breakout that jumps past its limit and never comes back is a **miss**.
- **Limit buy** (the price already past the trigger at 09:36): marketable when placed, so the
  first bar fills it at its open plus slippage if that is within the limit; otherwise it rests
  as above.
- **Stops** fill at min(stop, the bar's open) minus slippage, so gaps go through.
- **Soft-rule exits** fill at the decision price minus slippage.
- **Pessimistic same-bar order:** a bar that fills an entry can also stop it out, and a bar that
  reaches both the hard stop and a favourable level takes the stop first (``plans.resolve``
  checks the stop before any milestone).

Slippage is a fraction of the price, by the stock's average dollar volume (``sim.slippage``).
Buy fills round up to a tick, sell fills down.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from auratrade.config import SimConfig
from auratrade.execution.prices import round_down, round_up
from auratrade.plans.model import reached


class PriceBar(Protocol):
    @property
    def open(self) -> float: ...
    @property
    def high(self) -> float: ...
    @property
    def low(self) -> float: ...
    @property
    def close(self) -> float: ...


class MinuteBar(PriceBar, Protocol):
    @property
    def start(self) -> datetime: ...
    @property
    def volume(self) -> int | float: ...


def slippage(dollar_volume: float | None, config: SimConfig) -> float:
    """The slippage fraction for a stock trading ``dollar_volume`` a day; the thinnest tier
    when it isn't known."""
    thinnest = config.slippage[-1].fraction
    if dollar_volume is None:
        return thinnest
    return next(
        (t.fraction for t in config.slippage if dollar_volume >= t.min_dollar_volume), thinnest
    )


def buy_fill(price: float, slip: float) -> float:
    return round_up(price * (1 + slip))


def sell_fill(price: float, slip: float) -> float:
    return round_down(price * (1 - slip))


@dataclass(frozen=True, slots=True)
class EntryStep:
    price: float | None
    """The fill price, when this bar fills the order."""
    resting: bool
    """The order jumped past its limit and now rests as a limit order."""


def entry_on_bar(
    bar: PriceBar, *, trigger: float | None, limit: float, slip: float, resting: bool
) -> EntryStep:
    """One bar of a working entry: a buy-stop-limit (``trigger`` set) or a limit buy placed as
    marketable (``trigger`` None), ``resting`` once it has jumped past its limit."""
    if resting:
        return EntryStep(limit if bar.low <= limit else None, resting=True)
    if trigger is not None and not reached(bar.high, trigger):
        return EntryStep(None, resting=False)
    base = bar.open if trigger is None else max(trigger, bar.open)
    price = buy_fill(base, slip)
    if price <= limit:
        return EntryStep(price, resting=False)
    return EntryStep(None, resting=True)


def stop_on_bar(bar: PriceBar, *, stop: float, slip: float) -> float | None:
    """The fill of a sell stop on this bar, if the bar reaches it."""
    if bar.low > stop:
        return None
    return sell_fill(min(stop, bar.open), slip)


def sell_limit_on_bar(bar: PriceBar, *, limit: float, slip: float) -> float | None:
    """A resting sell limit: filled when the bar trades at or above it, at the open less
    slippage if that is better."""
    if bar.high < limit:
        return None
    return max(limit, sell_fill(bar.open, slip))


@dataclass(frozen=True, slots=True)
class Fill:
    price: float
    index: int
    """The bar that filled it."""


def entry_fill(
    bars: Sequence[PriceBar], *, trigger: float | None, limit: float, slip: float
) -> Fill | None:
    """The entry's fill over ``bars`` (from the order time to its cancellation), or None: a
    miss, or never triggered."""
    resting = False
    for i, bar in enumerate(bars):
        step = entry_on_bar(bar, trigger=trigger, limit=limit, slip=slip, resting=resting)
        if step.price is not None:
            return Fill(step.price, i)
        resting = step.resting
    return None


def stop_fill(bars: Sequence[PriceBar], *, stop: float, slip: float) -> Fill | None:
    for i, bar in enumerate(bars):
        if (price := stop_on_bar(bar, stop=stop, slip=slip)) is not None:
            return Fill(price, i)
    return None


def between[B: MinuteBar](bars: Sequence[B], start: datetime, end: datetime | None) -> list[B]:
    """The bars starting in [start, end); to the last bar when ``end`` is None."""
    return [b for b in bars if start <= b.start and (end is None or b.start < end)]


def last_price(bars: Sequence[MinuteBar], *, at: datetime, max_age: timedelta) -> float | None:
    """The close of the last bar starting in [at - max_age, at): the last trade at ``at`` if it
    is fresh enough, else None."""
    recent = between(bars, at - max_age, at)
    return recent[-1].close if recent else None
