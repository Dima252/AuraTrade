"""Hand-built minute-bar days for the simulator tests.

A session opens at 09:30 ET (13:30 UTC on 2026-10-21) and decides at 15:40 (minute 370). Bars
are keyed by their minute after the open: 0 = 09:30, 6 = 09:36, 369 = 15:39.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

OPEN_AT = datetime(2026, 10, 21, 13, 30, tzinfo=UTC)
DECISION_MINUTE = 370


@dataclass(frozen=True, slots=True)
class M:
    """A minute bar."""

    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 1000


def at(minute: int, day: int = 0) -> datetime:
    return OPEN_AT + timedelta(days=day, minutes=minute)


def bars(spec: dict[int, tuple[float, float, float, float]], day: int = 0) -> list[M]:
    """``{minute: (open, high, low, close)}`` -> minute bars, in time order."""
    return [M(at(m, day), *ohlc) for m, ohlc in sorted(spec.items())]


def flat(price: float) -> tuple[float, float, float, float]:
    return (price, price, price, price)
