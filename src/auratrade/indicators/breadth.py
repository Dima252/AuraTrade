"""Market breadth for the regime's breadth pillar (README §5.2)."""

from collections.abc import Sequence

from auratrade.indicators.series import Series, check_same_length, rolling_mean


def net_new_highs_average(
    new_high_counts: Sequence[int], new_low_counts: Sequence[int], window: int
) -> Series:
    """``window``-session average of (new highs - new lows) across the universe, per session."""
    check_same_length(new_high_counts, new_low_counts)
    return rolling_mean(
        [float(h - lo) for h, lo in zip(new_high_counts, new_low_counts, strict=True)], window
    )
