"""Shared helpers for indicator series.

A series is a list aligned index-for-index with its input bars: position ``t`` is the value as of
session ``t``, computed from sessions ``<= t`` only, and ``None`` while the window is still warming
up. Windows are counted in sessions (bars), never calendar days.
"""

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


def change(values: Sequence[float], lookback: int) -> Series:
    """Fractional change over ``lookback`` sessions: ``values[t] / values[t - lookback] - 1``."""
    check_window(lookback, "lookback")
    return [
        values[t] / values[t - lookback] - 1 if t >= lookback else None for t in range(len(values))
    ]
