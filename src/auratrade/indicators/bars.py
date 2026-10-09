"""The bar shape every indicator reads.

Bars are daily, split-adjusted (README §5.1), oldest first, one per session with no gaps for
closed days. Volume should already be half-day normalized where it is compared across sessions
(``volume.normalize_session_volumes``).
"""

from dataclasses import dataclass
from typing import Protocol


class OHLCV(Protocol):
    """Anything with daily open/high/low/close/volume: ORM rows, API models or ``Bar``."""

    @property
    def open(self) -> float: ...
    @property
    def high(self) -> float: ...
    @property
    def low(self) -> float: ...
    @property
    def close(self) -> float: ...
    @property
    def volume(self) -> float: ...


@dataclass(frozen=True, slots=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float
