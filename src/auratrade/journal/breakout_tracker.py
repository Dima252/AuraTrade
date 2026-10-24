"""The breakout tracker (README §5.12), pure: every trigger of the full, uncapped scan and its
result 5 sessions later, for the leadership-health pillar (§5.2).

- **A trigger** is a setup from session T-1's scan (every strategy, whatever the regime, the
  cap or the ranking) whose session-T daily high reaches its code pivot. A gap open above the
  pivot counts: the tracker asks whether breakouts work, not whether one could be bought.
- **Its result** is complete ``result_sessions`` (5) sessions after the trigger session: above
  its trigger when that session's close is above the pivot. A symbol with no bar that session
  (halted, delisted) is judged on its last close before it, so every result completes.
- **Splits.** Pivots are stored as the scan saw them; the bars may since have been adjusted. The
  result rescales the pivot by the trigger session's close now / its close then.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from auratrade.config import LeadershipConfig, Strategy
from auratrade.indicators.bars import OHLCV
from auratrade.plans.model import reached
from auratrade.regime.pillars import TrackedBreakout


@dataclass(frozen=True, slots=True)
class Watched:
    """A setup of the previous session's scan."""

    symbol: str
    strategy: Strategy
    pivot: float


@dataclass(frozen=True, slots=True)
class Breakout:
    symbol: str
    strategy: Strategy
    pivot: float
    session: date
    """The trigger session."""
    close: float
    """The trigger session's close, as the bars stood then (rescales the pivot after a split)."""


def triggers(
    watched: Sequence[Watched], bars: Mapping[str, OHLCV], session: date
) -> list[Breakout]:
    """The setups ``session``'s bars trigger; a symbol without a bar doesn't."""
    out = []
    for w in watched:
        bar = bars.get(w.symbol)
        if bar is not None and reached(bar.high, w.pivot):
            out.append(Breakout(w.symbol, w.strategy, w.pivot, session, bar.close))
    return out


def above_trigger(breakout: Breakout, *, close_then_now: float, result_close: float) -> bool:
    """The result: ``result_close`` above the pivot, rescaled by ``close_then_now`` (the trigger
    session's close in today's bars) over the close the breakout recorded."""
    scale = close_then_now / breakout.close
    return result_close > breakout.pivot * scale


@dataclass(frozen=True, slots=True)
class Tracked:
    """A tracker row as the regime reads it."""

    session: date
    above_trigger: bool | None


def for_pillar(
    rows: Sequence[Tracked], sessions: Sequence[date], cfg: LeadershipConfig
) -> list[TrackedBreakout]:
    """The rows of the window that ends at ``sessions[-1]`` (T), as the pillar reads them.
    ``sessions`` are the calendar's sessions up to T, oldest first, covering the window."""
    window = cfg.result_sessions + cfg.window_sessions
    ago = {day: i for i, day in enumerate(reversed(sessions[-window:]))}
    return [TrackedBreakout(ago[r.session], r.above_trigger) for r in rows if r.session in ago]
