"""Shared helpers for indicator series.

A series is a list aligned index-for-index with its input bars: position ``t`` is the value as of
session ``t``, computed from sessions ``<= t`` only, and ``None`` while the window is still warming
up. Windows are counted in sessions (bars), never calendar days.
"""

from collections import deque
from collections.abc import Sequence

type Series = list[float | None]


def check_window(window: int, name: str = "window") -> None:
    if window < 1:
        raise ValueError(f"{name} must be >= 1, got {window}")


def check_same_length(*sequences: Sequence[object]) -> None:
    if len({len(s) for s in sequences}) > 1:
        raise ValueError(f"series lengths differ: {[len(s) for s in sequences]}")


def rolling_sum(values: Sequence[float], window: int) -> Series:
    """Sum of the last ``window`` values, ``None`` until ``window`` values exist."""
    check_window(window)
    out: Series = []
    total = 0.0
    for t, value in enumerate(values):
        total += value
        if t >= window:
            total -= values[t - window]
        out.append(total if t >= window - 1 else None)
    return out


def rolling_mean(values: Sequence[float], window: int) -> Series:
    return [None if s is None else s / window for s in rolling_sum(values, window)]


def _rolling_extreme(values: Sequence[float], window: int, *, highest: bool) -> Series:
    # Monotonic deque of indices: O(n) instead of O(n * window), which matters over 252-session
    # windows across the whole universe in backtests.
    check_window(window)
    out: Series = []
    candidates: deque[int] = deque()
    for t, value in enumerate(values):
        while candidates and (
            values[candidates[-1]] <= value if highest else values[candidates[-1]] >= value
        ):
            candidates.pop()
        candidates.append(t)
        if candidates[0] <= t - window:
            candidates.popleft()
        out.append(values[candidates[0]] if t >= window - 1 else None)
    return out


def rolling_max(values: Sequence[float], window: int) -> Series:
    """Highest of the last ``window`` values (including the current one)."""
    return _rolling_extreme(values, window, highest=True)


def rolling_min(values: Sequence[float], window: int) -> Series:
    """Lowest of the last ``window`` values (including the current one)."""
    return _rolling_extreme(values, window, highest=False)


def change(values: Sequence[float], lookback: int) -> Series:
    """Fractional change over ``lookback`` sessions: ``values[t] / values[t - lookback] - 1``."""
    check_window(lookback, "lookback")
    return [
        values[t] / values[t - lookback] - 1 if t >= lookback else None for t in range(len(values))
    ]


def at_window_high(values: Sequence[float], window: int) -> list[bool]:
    """True where the value is the highest of its last ``window`` values (ties count).

    False until a full window of history exists, so a short history never reads as a new high.
    """
    return [
        h is not None and v >= h for v, h in zip(values, rolling_max(values, window), strict=True)
    ]


def at_window_low(values: Sequence[float], window: int) -> list[bool]:
    """True where the value is the lowest of its last ``window`` values (ties count)."""
    return [
        lo is not None and v <= lo
        for v, lo in zip(values, rolling_min(values, window), strict=True)
    ]
