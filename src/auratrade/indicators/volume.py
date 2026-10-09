"""Volume measures: up/down volume ratio, half-day normalization and the intraday pace curve."""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from auratrade.indicators.bars import OHLCV
from auratrade.indicators.series import Series, check_same_length, check_window


def up_down_volume_ratio(bars: Sequence[OHLCV], window: int) -> Series:
    """Volume on up days / volume on down days over the last ``window`` sessions (IBD).

    An up (down) day closes above (below) the prior close; unchanged days count for neither. The
    first value is at session ``window``, the first whose window has a prior close for every day.
    No down volume gives ``inf`` (or ``None`` if there was no up volume either).
    """
    check_window(window)
    up = [0.0] * len(bars)
    down = [0.0] * len(bars)
    for t in range(1, len(bars)):
        if bars[t].close > bars[t - 1].close:
            up[t] = bars[t].volume
        elif bars[t].close < bars[t - 1].close:
            down[t] = bars[t].volume
    out: Series = []
    up_sum = down_sum = 0.0
    for t in range(len(bars)):
        up_sum += up[t]
        down_sum += down[t]
        if t > window:
            up_sum -= up[t - window]
            down_sum -= down[t - window]
        if t < window:
            out.append(None)
        elif down_sum > 0:
            out.append(up_sum / down_sum)
        else:
            out.append(math.inf if up_sum > 0 else None)
    return out


def normalize_session_volume(
    volume: float, session_minutes: int, full_session_minutes: int
) -> float:
    """Scale a short session's volume to a full-session equivalent (README §4).

    ``volume * 390 / 210`` on a 13:00 early close, so half days don't drag down averages or read
    as light volume.
    """
    check_window(session_minutes, "session_minutes")
    return volume * full_session_minutes / session_minutes


def normalize_session_volumes(
    volumes: Sequence[float], session_minutes: Sequence[int], full_session_minutes: int
) -> list[float]:
    check_same_length(volumes, session_minutes)
    return [
        normalize_session_volume(v, m, full_session_minutes)
        for v, m in zip(volumes, session_minutes, strict=True)
    ]


@dataclass(frozen=True, slots=True)
class PaceDay:
    """One full session's minute volumes and the daily volume they should project to."""

    minute_volumes: Sequence[float]
    day_volume: float
    """The daily-bar volume the 50-day average is built from. It may include volume outside the
    minute bars (the closing auction, extended hours: spike 0.3a), so a curve built against it
    projects to the same definition as the average it is compared with."""


def build_pace_curve(days: Sequence[PaceDay]) -> list[float]:
    """Typical cumulative share of the day's volume after each minute of the session.

    ``curve[m - 1]`` is the median, across ``days``, of (volume in the first ``m`` minutes) /
    ``day_volume``. Days with no volume are skipped. All days must be full sessions of the same
    length.
    """
    usable = [day for day in days if day.day_volume > 0]
    if not usable:
        raise ValueError("no days with volume to build a pace curve from")
    minutes = len(usable[0].minute_volumes)
    if minutes == 0 or any(len(day.minute_volumes) != minutes for day in usable):
        raise ValueError("every day needs the same, non-zero number of minutes")
    shares: list[list[float]] = [[] for _ in range(minutes)]
    for day in usable:
        cumulative = 0.0
        for m, volume in enumerate(day.minute_volumes):
            cumulative += volume
            shares[m].append(cumulative / day.day_volume)
    return [statistics.median(s) for s in shares]


def project_day_volume(
    cumulative_volume: float, minutes_elapsed: int, curve: Sequence[float], session_minutes: int
) -> float | None:
    """Project the session's full volume from the volume so far, using a pace curve.

    ``minutes_elapsed`` counts the minutes the volume covers (delayed SIP at 15:40 covers to
    15:25: 355 minutes). On a short session the elapsed share of the session is mapped onto the
    full-session curve; normalize the result before comparing it with a full-day average.
    ``None`` before the first minute or where the curve has no volume yet.
    """
    if not curve:
        raise ValueError("empty pace curve")
    check_window(session_minutes, "session_minutes")
    if minutes_elapsed <= 0:
        return None
    position = round(min(minutes_elapsed, session_minutes) * len(curve) / session_minutes)
    share = curve[max(position, 1) - 1]
    return cumulative_volume / share if share > 0 else None
