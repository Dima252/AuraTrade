"""Earnings-gap catalyst detection (archived README §5.3, "Earnings catalyst").

Finds a gap up on the first session that could react to an earnings report. The report's session
and timing come from ``earnings_events`` (EDGAR 8-K acceptance time for history, §5.1); mapping
dates to bar positions is the caller's job, since it needs the calendar.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from auratrade.indicators.bars import OHLCV
from auratrade.indicators.series import check_window


class EarningsTiming(StrEnum):
    BEFORE_OPEN = "before_open"
    DURING_MARKET = "during_market"
    AFTER_CLOSE = "after_close"


def reaction_session(report_session: int, timing: EarningsTiming) -> int:
    """The first session whose open can reflect the report: the next one for an after-close
    report, the report session itself otherwise."""
    return report_session + 1 if timing is EarningsTiming.AFTER_CLOSE else report_session


@dataclass(frozen=True, slots=True)
class EarningsGap:
    session: int
    gap: float
    """Open / prior close - 1."""
    day_gain: float
    """Close / prior close - 1: whether the gap held into the close."""
    volume_ratio: float
    """Session volume / the average of the prior ``volume_window`` sessions."""


def earnings_gap(
    bars: Sequence[OHLCV],
    session: int,
    *,
    min_gap: float,
    volume_window: int,
    min_volume_ratio: float,
) -> EarningsGap | None:
    """The gap up at ``session`` (a reaction session), if it opened at least ``min_gap`` above the
    prior close on volume at least ``min_volume_ratio`` times the prior average.

    ``None`` if it didn't, or if there aren't ``volume_window`` prior sessions.
    """
    check_window(volume_window, "volume_window")
    if not 0 <= session < len(bars):
        raise IndexError(f"session {session} out of range")
    if session < volume_window:
        return None
    prior = bars[session - 1]
    bar = bars[session]
    average = sum(b.volume for b in bars[session - volume_window : session]) / volume_window
    if average <= 0:
        return None
    gap = bar.open / prior.close - 1
    volume_ratio = bar.volume / average
    if gap < min_gap or volume_ratio < min_volume_ratio:
        return None
    return EarningsGap(
        session=session,
        gap=gap,
        day_gain=bar.close / prior.close - 1,
        volume_ratio=volume_ratio,
    )


def latest_earnings_gap(
    bars: Sequence[OHLCV],
    reaction_sessions: Iterable[int],
    at: int,
    *,
    lookback: int,
    min_gap: float,
    volume_window: int,
    min_volume_ratio: float,
) -> EarningsGap | None:
    """The most recent qualifying earnings gap within the last ``lookback`` sessions up to ``at``.

    Reaction sessions after ``at`` are ignored, so a backtest can't see a report early.
    """
    check_window(lookback, "lookback")
    if not 0 <= at < len(bars):
        raise IndexError(f"session {at} out of range")
    for session in sorted(set(reaction_sessions), reverse=True):
        if session > at:
            continue
        if session <= at - lookback:
            break
        gap = earnings_gap(
            bars,
            session,
            min_gap=min_gap,
            volume_window=volume_window,
            min_volume_ratio=min_volume_ratio,
        )
        if gap is not None:
            return gap
    return None
