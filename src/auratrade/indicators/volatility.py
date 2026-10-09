"""ATR and ADR %: the two measures that bound stop width (README §5.3, §7.1, #66)."""

from collections.abc import Sequence
from itertools import pairwise

from auratrade.indicators.bars import OHLCV
from auratrade.indicators.series import Series, check_window, rolling_mean


def _true_ranges_after_first(bars: Sequence[OHLCV]) -> list[float]:
    return [max(bar.high, prev.close) - min(bar.low, prev.close) for prev, bar in pairwise(bars)]


def true_range(bars: Sequence[OHLCV]) -> Series:
    """max(high, prior close) - min(low, prior close); ``None`` on the first bar."""
    out: Series = [None] * min(1, len(bars))
    out.extend(_true_ranges_after_first(bars))
    return out


def atr(bars: Sequence[OHLCV], window: int) -> Series:
    """Average true range: the plain ``window``-session average of the true range (README §5.3).

    A plain average rather than Wilder's smoothing, as the README defines it. The first value is at
    session ``window``, the first with ``window`` true ranges behind it.
    """
    check_window(window)
    out: Series = [None] * min(1, len(bars))
    out.extend(rolling_mean(_true_ranges_after_first(bars), window))
    return out


def adr_pct(bars: Sequence[OHLCV], window: int) -> Series:
    """Average daily range as a fraction: the ``window``-session average of ``high / low - 1``."""
    return rolling_mean([bar.high / bar.low - 1 for bar in bars], window)
