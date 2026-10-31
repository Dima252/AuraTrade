"""IBD distribution days and follow-through days, per index (README §5.2).

These are the per-index signals. Combining SPY and QQQ (higher distribution count, an FTD on
either index), deciding when ``CORRECTION`` starts and what happens after an FTD belong to the
regime state machine, which drives ``step_rally_attempt`` one session at a time.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import pairwise

from auratrade.indicators.bars import OHLCV
from auratrade.indicators.series import check_window


def distribution_days(bars: Sequence[OHLCV], *, min_decline: float) -> list[bool]:
    """True where the index closed down by at least ``min_decline`` (0.002 = 0.2%) on volume above
    the prior session's."""
    out = [False] * min(1, len(bars))
    for prev, bar in pairwise(bars):
        out.append(bar.close <= prev.close * (1 - min_decline) and bar.volume > prev.volume)
    return out


def distribution_count(
    flags: Sequence[bool],
    at: int,
    *,
    window: int,
    reset_after: int | None = None,
    excluded: Sequence[bool] | None = None,
) -> int:
    """Distribution days among the last ``window`` sessions up to and including ``at``.

    ``reset_after`` is the session of the latest follow-through day: the count restarts after it
    (README §5.2). ``excluded`` marks sessions not to count (option-expiration or index-rebalance
    sessions, if the regime backtest decides to drop them).
    """
    check_window(window)
    if not 0 <= at < len(flags):
        raise IndexError(f"session {at} out of range")
    first = at - window + 1
    if reset_after is not None:
        first = max(first, reset_after + 1)
    return sum(
        1
        for t in range(max(first, 0), at + 1)
        if flags[t] and not (excluded is not None and excluded[t])
    )


@dataclass(frozen=True, slots=True)
class RallyAttempt:
    """One index's rally attempt inside ``CORRECTION``."""

    rally_low: float
    """The correction low the attempt started from: the lowest intraday low since the session the
    regime entered ``CORRECTION``."""
    low_session: int
    day: int
    """0 until the first up close after the low; then 1, 2, ... (every session counts)."""


@dataclass(frozen=True, slots=True)
class FollowThroughDay:
    session: int
    day: int
    gain: float
    rally_low: float
    weak: bool
    """Later than the strongest days (IBD: the best FTDs come on days 4-7)."""


def start_rally_attempt(bar: OHLCV, session: int) -> RallyAttempt:
    """Start tracking on the session the regime entered ``CORRECTION``: its low is the first
    correction low, so an attempt can begin at once even if the index is near its high."""
    return RallyAttempt(rally_low=bar.low, low_session=session, day=0)


def step_rally_attempt(
    state: RallyAttempt,
    prev: OHLCV,
    bar: OHLCV,
    session: int,
    *,
    min_gain: float,
    first_day: int,
    last_strong_day: int,
) -> tuple[RallyAttempt, FollowThroughDay | None]:
    """Advance one session; return the new state and the follow-through day, if this is one.

    - An intraday low below the rally low makes a new correction low and restarts the count
      (the low session itself can't be day 1).
    - Day 1 is the first close above the prior close after the low.
    - An FTD is day ``first_day`` (4) or later, up at least ``min_gain`` (0.0125) on volume above
      the prior session's; after ``last_strong_day`` (7) it is flagged weak.
    """
    if bar.low < state.rally_low:
        return RallyAttempt(rally_low=bar.low, low_session=session, day=0), None
    if state.day == 0:
        return (replace(state, day=1) if bar.close > prev.close else state), None
    advanced = replace(state, day=state.day + 1)
    gain = bar.close / prev.close - 1
    if advanced.day >= first_day and gain >= min_gain and bar.volume > prev.volume:
        return advanced, FollowThroughDay(
            session=session,
            day=advanced.day,
            gain=gain,
            rally_low=advanced.rally_low,
            weak=advanced.day > last_strong_day,
        )
    return advanced, None
