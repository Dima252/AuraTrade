"""IBD climax-top signals for a held position (README §7.3, "Climax sale").

``move_start`` is the breakout session the move is measured from. Every "since the move began"
signal needs at least one session after it, so the breakout day itself never counts against
itself.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from auratrade.indicators.bars import OHLCV
from auratrade.indicators.series import check_window


@dataclass(frozen=True, slots=True)
class ClimaxSignals:
    largest_gain: bool
    """The largest one-session gain since the move began."""
    exhaustion_gap: bool
    """Opened above the prior session's high by at least the minimum gap."""
    heaviest_volume_up_day: bool
    """An up day on the heaviest volume since the breakout."""
    up_day_cluster: bool
    """At least k up days in the last n (7 of 8, 8 of 10)."""
    far_above_long_average: bool
    """At least the threshold above the 200-day."""
    fast_gain: bool
    """A large gain within a few sessions, once the run is already extended."""

    @property
    def count(self) -> int:
        return sum(
            (
                self.largest_gain,
                self.exhaustion_gap,
                self.heaviest_volume_up_day,
                self.up_day_cluster,
                self.far_above_long_average,
                self.fast_gain,
            )
        )


def _up(bars: Sequence[OHLCV], t: int) -> bool:
    return t >= 1 and bars[t].close > bars[t - 1].close


def _gain(bars: Sequence[OHLCV], t: int) -> float | None:
    return bars[t].close / bars[t - 1].close - 1 if t >= 1 else None


def climax_signals(
    bars: Sequence[OHLCV],
    *,
    at: int,
    move_start: int,
    long_average: float | None,
    exhaustion_min_gap: float,
    up_day_clusters: Sequence[tuple[int, int]],
    min_above_long_average: float,
    fast_gain_min: float,
    fast_gain_window: int,
    fast_gain_min_move_sessions: int,
) -> ClimaxSignals:
    """Climax signals at session ``at`` for a move that began at ``move_start``.

    - ``long_average``: the 200-day SMA at ``at`` (``None`` if not available).
    - ``up_day_clusters``: ``[(7, 8), (8, 10)]`` = 7 up days in 8 or 8 in 10.
    - Fast gain: the close is at least ``fast_gain_min`` above the lowest close of the prior
      ``fast_gain_window`` sessions (25% in 15), counted only once the move is at least
      ``fast_gain_min_move_sessions`` old. That is the README's "after an extended run": early in
      a move a fast gain is the 8-week-hold signal, not a climax.
    """
    if not 0 <= move_start <= at < len(bars):
        raise IndexError(f"need 0 <= move_start ({move_start}) <= at ({at}) < {len(bars)}")
    check_window(fast_gain_window, "fast_gain_window")
    bar = bars[at]
    in_move = at > move_start

    gain = _gain(bars, at)
    earlier_gains = [g for t in range(move_start, at) if (g := _gain(bars, t)) is not None]
    largest_gain = (
        in_move and gain is not None and gain > 0 and all(gain > g for g in earlier_gains)
    )

    exhaustion_gap = in_move and bar.open > bars[at - 1].high * (1 + exhaustion_min_gap)

    heaviest_volume_up_day = (
        in_move
        and _up(bars, at)
        and all(bar.volume > bars[t].volume for t in range(move_start, at))
    )

    up_day_cluster = False
    for needed, of in up_day_clusters:
        check_window(of, "cluster length")
        if at >= of and sum(_up(bars, t) for t in range(at - of + 1, at + 1)) >= needed:
            up_day_cluster = True

    far_above_long_average = long_average is not None and bar.close >= long_average * (
        1 + min_above_long_average
    )

    fast_gain = False
    if at - move_start >= fast_gain_min_move_sessions and at >= fast_gain_window:
        base = min(b.close for b in bars[at - fast_gain_window : at])
        fast_gain = bar.close >= base * (1 + fast_gain_min)

    return ClimaxSignals(
        largest_gain=largest_gain,
        exhaustion_gap=exhaustion_gap,
        heaviest_volume_up_day=heaviest_volume_up_day,
        up_day_cluster=up_day_cluster,
        far_above_long_average=far_above_long_average,
        fast_gain=fast_gain,
    )
