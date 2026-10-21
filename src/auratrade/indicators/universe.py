"""The README §5.1 universe filters, point in time (pure).

A security is in the universe at a session when, as of that session, its close as traded (raw,
not split-adjusted) is at least ``min_price``, its average dollar volume over the last
``dollar_volume_sessions`` bars is at least ``min_dollar_volume``, and it has at least
``min_history_sessions`` bars (the ``universe`` section of risk.yaml; indicators take plain
values, not the config). The regime's breadth (live and 2.4) and the backfill's membership
table both use this function, so the backtests rebuild exactly the universe the live jobs see.
"""

from collections.abc import Sequence

from auratrade.indicators.series import check_same_length, rolling_mean


def universe_flags(
    closes: Sequence[float],
    raw_closes: Sequence[float],
    volumes: Sequence[float],
    *,
    min_price: float,
    min_dollar_volume: float,
    dollar_volume_sessions: int,
    min_history_sessions: int,
) -> list[bool]:
    """Per bar (one security, oldest first): whether it passes the filters at that bar.

    Dollar volume is the split-adjusted close times the split-adjusted volume, which equals the
    traded price times the traded shares."""
    check_same_length(closes, raw_closes, volumes)
    dollar_volume = rolling_mean(
        [c * v for c, v in zip(closes, volumes, strict=True)], dollar_volume_sessions
    )
    return [
        i + 1 >= min_history_sessions
        and raw_closes[i] >= min_price
        and dv is not None
        and dv >= min_dollar_volume
        for i, dv in enumerate(dollar_volume)
    ]
