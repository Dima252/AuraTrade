"""Volume measures: up/down volume ratio, half-day normalization and the intraday pace curve."""

from collections.abc import Sequence

from auratrade.indicators.series import check_same_length, check_window


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
