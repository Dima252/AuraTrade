"""Highest high and lowest low over a window of sessions (the 52-week range is 252 sessions)."""

from collections.abc import Sequence

from auratrade.indicators.bars import OHLCV
from auratrade.indicators.series import Series, rolling_max, rolling_min


def highest_high(bars: Sequence[OHLCV], window: int) -> Series:
    """Highest intraday high over the last ``window`` sessions, including the current one."""
    return rolling_max([bar.high for bar in bars], window)


def lowest_low(bars: Sequence[OHLCV], window: int) -> Series:
    """Lowest intraday low over the last ``window`` sessions, including the current one."""
    return rolling_min([bar.low for bar in bars], window)
