"""The monthly rebalance calendar (README §6-§7): the ranking session R (the last session of
each month) and the execution session E (the next full session).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol


class SessionDay(Protocol):
    @property
    def day(self) -> date: ...
    @property
    def is_early_close(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Rebalance:
    ranking: date
    """R: the session the trend is read on."""
    execution: date
    """E: the full session it trades on."""


def rebalances(sessions: Sequence[SessionDay], offset: int = 1) -> list[Rebalance]:
    """Every rebalance the calendar fully covers, oldest first.

    The nominal execution session is the ``offset``-th session of each month (1 = the first;
    the timing-luck diagnostic moves it). R is the session before it, and E is the first full
    session from it: a half day moves E, never R. A month whose nominal session, or a full
    session from it, isn't in ``sessions`` is left out.
    """
    if offset < 1:
        raise ValueError("offset counts from 1")
    days = sorted(sessions, key=lambda s: s.day)
    out: list[Rebalance] = []
    month: tuple[int, int] | None = None
    count = 0
    for i, s in enumerate(days):
        key = (s.day.year, s.day.month)
        count = count + 1 if key == month else 1
        month = key
        if count != offset or i == 0:
            continue
        execution = next((d.day for d in days[i:] if not d.is_early_close), None)
        if execution is not None:
            out.append(Rebalance(days[i - 1].day, execution))
    return out
