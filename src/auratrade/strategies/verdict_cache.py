"""The verdict cache (README §5.3 "Verdict cache", §9): which selected setups the Analyst
re-reviews tonight.

A setup is re-reviewed only if its ticker has no verdict for the strategy, its **structure**
changed, or the verdict is more than ``verdict_max_age_sessions`` old. The structure is the
base or consolidation: the pivot for VCP, momentum, emerging-leader and EP setups, the average
being tested for pullbacks. A pullback's trigger (the prior session's high) moves every day and
doesn't count as a change. The others keep their cached verdict; they still hold their slot.
"""

from dataclasses import dataclass

from auratrade.config import Strategy
from auratrade.strategies.base import Setup
from auratrade.strategies.leader_pullback import PullbackDetail


@dataclass(frozen=True, slots=True)
class CachedVerdict:
    """The Analyst's last verdict on a ticker's setup."""

    symbol: str
    strategy: Strategy
    structure: float | int
    sessions_ago: int
    """Sessions since the verdict was given (the caller counts them with the calendar)."""


def structure(setup: Setup) -> float | int:
    """What a re-review is keyed on: the pivot to the cent, or a pullback's tested average."""
    if isinstance(setup.detail, PullbackDetail):
        return setup.detail.tested_ema
    return round(setup.pivot, 2)


def needs_review(setup: Setup, cached: CachedVerdict | None, max_age_sessions: int) -> bool:
    if cached is None or cached.symbol != setup.symbol or cached.strategy is not setup.strategy:
        return True
    return cached.structure != structure(setup) or cached.sessions_ago > max_age_sessions
