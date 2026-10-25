"""Your numbers (Minervini): win rate, average gain, average loss and expectancy, pure.

A trade's ``gain`` is its realized return over all tranches and partials (a fraction of its
cost); ``r`` is its realized R. A winner has a gain above 0 and a loser below 0; a flat trade
counts in the win rate's denominator only. Computed over the last ``window`` closed trades (all
of them without one), oldest first.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from auratrade.config import Strategy
from auratrade.guardrails.levels import StrategyRecord


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    strategy: Strategy
    gain: float
    r: float


@dataclass(frozen=True, slots=True)
class Numbers:
    trades: int
    win_rate: float | None
    average_gain: float | None
    """The average winner's gain; None without a winner."""
    average_loss: float | None
    """The average loser's loss, as a positive fraction; None without a loser."""
    expectancy: float | None
    """The average gain per trade (a fraction): win rate x average gain - loss rate x average
    loss."""
    expectancy_r: float | None
    """The average R per trade."""


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def numbers(trades: Sequence[ClosedTrade], window: int | None = None) -> Numbers:
    if window is not None and window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    recent = list(trades[-window:] if window is not None else trades)
    gains = [t.gain for t in recent if t.gain > 0]
    losses = [-t.gain for t in recent if t.gain < 0]
    n = len(recent)
    return Numbers(
        trades=n,
        win_rate=len(gains) / n if n else None,
        average_gain=_mean(gains),
        average_loss=_mean(losses),
        expectancy=_mean([t.gain for t in recent]),
        expectancy_r=_mean([t.r for t in recent]),
    )


def strategy_record(
    trades: Sequence[ClosedTrade], strategy: Strategy, *, window: int
) -> StrategyRecord:
    """A strategy's count of closed trades and its rolling average gain, for the guardrails'
    "keep losses small vs gains" (archived README §8)."""
    own = [t for t in trades if t.strategy is strategy]
    return StrategyRecord(closed_trades=len(own), average_gain=numbers(own, window).average_gain)
