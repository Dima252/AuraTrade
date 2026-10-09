"""Market breadth for the regime's breadth pillar (README §5.2)."""

from collections.abc import Iterable, Sequence

from auratrade.indicators.series import (
    Series,
    at_window_high,
    at_window_low,
    check_same_length,
    rolling_mean,
)


def new_highs(highs: Sequence[float], window: int) -> list[bool]:
    """True where a symbol's intraday high is the highest of its last ``window`` sessions (252 for
    a 52-week high). Never true before a full window of history."""
    return at_window_high(highs, window)


def new_lows(lows: Sequence[float], window: int) -> list[bool]:
    """True where a symbol's intraday low is the lowest of its last ``window`` sessions."""
    return at_window_low(lows, window)


def fraction_above(closes_and_averages: Iterable[tuple[float, float | None]]) -> float | None:
    """Share of symbols closing above their moving average (e.g. the 50 SMA), 0 to 1.

    Symbols whose average isn't available yet are left out. ``None`` if none are left.
    """
    above = counted = 0
    for close, average in closes_and_averages:
        if average is None:
            continue
        counted += 1
        above += close > average
    return above / counted if counted else None


def net_new_highs_average(
    new_high_counts: Sequence[int], new_low_counts: Sequence[int], window: int
) -> Series:
    """``window``-session average of (new highs - new lows) across the universe, per session."""
    check_same_length(new_high_counts, new_low_counts)
    return rolling_mean(
        [float(h - lo) for h, lo in zip(new_high_counts, new_low_counts, strict=True)], window
    )
