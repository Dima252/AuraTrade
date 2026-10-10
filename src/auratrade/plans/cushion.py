"""The earnings cushion for open positions (README §7.4).

It runs at the EOD review of the last session before a report: the prior session for a report
before the open, the report day itself for one after the close. Timing that isn't known to be
after the close is treated as before the open, so the cushion runs a session early rather than
a session late. A held position with no known next date runs it before the estimated window
instead (last report + 63 sessions, ±10: the session before the window starts).

Sessions are a sorted sequence of trading dates from the market calendar; windows count them,
never calendar days.
"""

import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from datetime import date
from enum import StrEnum

from auratrade.config import EarningsCushionConfig
from auratrade.plans.model import reached


class EarningsTiming(StrEnum):
    """``earnings_events.timing``."""

    BEFORE_OPEN = "BEFORE_OPEN"
    DURING_MARKET = "DURING_MARKET"
    AFTER_CLOSE = "AFTER_CLOSE"
    UNKNOWN = "UNKNOWN"


class CushionAction(StrEnum):
    EXIT = "EXIT"
    """Gain < 1R."""
    TRIM = "TRIM"
    """1R to 3R: trim to half; stop at least breakeven."""
    HOLD = "HOLD"
    """>= 3R: may hold full; stop at least breakeven."""


def cushion_session(
    report_date: date, timing: EarningsTiming, sessions: Sequence[date]
) -> date | None:
    """The session whose EOD review runs the cushion; None if the calendar doesn't reach it."""
    if timing is EarningsTiming.AFTER_CLOSE:
        i = bisect_right(sessions, report_date) - 1  # the report day, or the session before it
    else:
        i = bisect_left(sessions, report_date) - 1  # the session before the report day
    return sessions[i] if i >= 0 else None


def estimated_cushion_session(
    last_report: date,
    sessions: Sequence[date],
    *,
    estimate_sessions: int,
    window_sessions: int,
) -> date | None:
    """The cushion session for a position with no known next report date.

    The next report is estimated at ``estimate_sessions`` after the last one, with a window of
    ``window_sessions`` either side (README §8); the cushion runs the session before the window
    starts. None if the calendar doesn't reach it.
    """
    if window_sessions >= estimate_sessions:
        raise ValueError("the window must start after the last report")
    start = bisect_left(sessions, last_report)  # the report's session, or the next one
    i = start + estimate_sessions - window_sessions - 1
    return sessions[i] if i < len(sessions) else None


def cushion_action(gain_r: float, config: EarningsCushionConfig) -> CushionAction:
    """The README §7.4 table on the unrealized gain in R (on the decision price)."""
    if not math.isfinite(gain_r):
        raise ValueError(f"gain_r must be finite, got {gain_r}")
    if not reached(gain_r, config.exit_below_r):
        return CushionAction.EXIT
    if not reached(gain_r, config.hold_full_from_r):
        return CushionAction.TRIM
    return CushionAction.HOLD
