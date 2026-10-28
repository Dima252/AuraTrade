"""Simple and exponential moving averages, slope and extension."""

from collections.abc import Sequence

from auratrade.indicators.series import Series, check_window, rolling_mean


def sma(values: Sequence[float], window: int) -> Series:
    """Simple moving average over ``window`` sessions."""
    return rolling_mean(values, window)


def ema(values: Sequence[float], window: int) -> Series:
    """Exponential moving average with ``alpha = 2 / (window + 1)``.

    Seeded with the simple average of the first ``window`` values, so the first value appears at
    the same session as the SMA's and doesn't depend on how far back the history starts beyond it.
    """
    check_window(window)
    alpha = 2 / (window + 1)
    out: Series = [None] * min(window - 1, len(values))
    if len(values) < window:
        return out
    current = sum(values[:window]) / window
    out.append(current)
    for value in values[window:]:
        current += alpha * (value - current)
        out.append(current)
    return out


def rising(values: Sequence[float | None], lookback: int) -> list[bool]:
    """True where ``values[t] > values[t - lookback]`` (both known).

    The Trend Template's "200 SMA rising for at least 22 sessions" and the regime's
    "50 SMA rising" read this with their own lookbacks.
    """
    check_window(lookback, "lookback")
    out: list[bool] = []
    for t, value in enumerate(values):
        prior = values[t - lookback] if t >= lookback else None
        out.append(value is not None and prior is not None and value > prior)
    return out


def falling(values: Sequence[float | None], lookback: int) -> list[bool]:
    """True where ``values[t] < values[t - lookback]`` (both known)."""
    check_window(lookback, "lookback")
    out: list[bool] = []
    for t, value in enumerate(values):
        prior = values[t - lookback] if t >= lookback else None
        out.append(value is not None and prior is not None and value < prior)
    return out
