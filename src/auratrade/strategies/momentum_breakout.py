"""`MomentumBreakout`: Kullamägi's breakout after a big move (archived README §5.3, §7.1).

A top-~2% momentum stock (1-, 3- or 6-month return) that made a move of 30-100%+ within the
last 1-3 months, then consolidated for 10-40 sessions with higher lows and a tightening range,
riding its rising 10- or 20-day average.

- **The consolidation** starts at the move's high: the highest high of the last
  ``max_consolidation_sessions``, which must also top the ``move_sessions`` before it. Its
  length is the sessions since.
- **The move** runs from the lowest low of the ``move_sessions`` before that high to the high.
- **Higher lows and a tightening range:** the consolidation's second half has a higher low and
  a smaller average daily range than its first half.
- **Riding the average:** the close is at or above a rising ``ride_smas`` average.

The trigger is the consolidation high (or the opening-range high above it, §7.1); the structural
stop is the second half's low, the consolidation's last higher low. The order-time stop is the
low of the day at 09:36, capped at 1 x ADR below the trigger. Momentum setups are exempt from
the 25%-above-50-day rule; their own consolidation bounds their extension (archived README §5.3).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from auratrade.config import Strategy
from auratrade.indicators.bars import OHLCV
from auratrade.strategies.base import (
    BaseStrategy,
    Fact,
    Setup,
    SymbolData,
    fraction_fact,
    price_fact,
)


@dataclass(frozen=True, slots=True)
class MomentumDetail:
    peak_session: int
    consolidation_sessions: int
    move: float
    """From the move's low to the consolidation high (0.45 = +45%)."""
    move_low: float
    momentum_rank: float
    first_half_range: float
    second_half_range: float
    """The average daily range (high / low - 1) of each half of the consolidation."""
    depth: float
    """The consolidation's pullback: from its high to its lowest low."""
    adr: float | None
    close: float
    pivot: float

    def facts(self) -> Mapping[str, Fact]:
        return {
            "consolidation_sessions": self.consolidation_sessions,
            "prior_move": fraction_fact(self.move),
            "momentum_rank": round(self.momentum_rank, 1),
            "consolidation_depth": fraction_fact(self.depth),
            "range_first_half": fraction_fact(self.first_half_range),
            "range_second_half": fraction_fact(self.second_half_range),
            "adr": None if self.adr is None else fraction_fact(self.adr),
            "distance_to_pivot": fraction_fact(self.pivot / self.close - 1),
            "move_low": price_fact(self.move_low),
        }


def _average_range(bars: Sequence[OHLCV]) -> float:
    return sum(b.high / b.low - 1 for b in bars) / len(bars)


class MomentumBreakout(BaseStrategy):
    strategy = Strategy.MOMENTUM_BREAKOUT
    system_prompt = (
        "MomentumBreakout (Kullamägi): one of the market's strongest stocks (top ~2% by 1-, 3- "
        "or 6-month return) made a 30-100%+ move in the last 1-3 months, then consolidated for "
        "10-40 sessions with higher lows and a tightening range while riding its rising 10- or "
        "20-day average. The entry is the break of the consolidation high (or the 5-minute "
        "opening-range high above it); the stop is the low of the day, at most 1 x ADR below "
        "the trigger. It may sit well above its 50-day: the tight consolidation, not the 50-day, "
        "bounds its extension. Better setups have a fresh, powerful move, an orderly, tightening "
        "consolidation on lighter volume and a clear catalyst or theme."
    )

    def scan(self, data: SymbolData, at: int | None = None) -> Setup | None:
        t = data.last if at is None else at
        cfg = self.risk.strategies.MOMENTUM_BREAKOUT
        rank = data.momentum_ranks[t]
        if rank is None or rank < cfg.min_rank or not self.passes_trend_template(data, t):
            return None
        bars = data.bars
        peak = max(
            range(max(0, t - cfg.max_consolidation_sessions), t + 1),
            key=lambda i: (bars[i].high, -i),
        )
        sessions = t - peak
        if sessions < cfg.min_consolidation_sessions or peak < 1:
            return None
        before = bars[max(0, peak - cfg.move_sessions) : peak]
        high = bars[peak].high
        if any(b.high > high for b in before):
            return None
        move_low = min(b.low for b in before)
        move = high / move_low - 1
        if move < cfg.min_move:
            return None
        half = (sessions + 1) // 2
        first, second = bars[peak + 1 : peak + 1 + half], bars[peak + 1 + half : t + 1]
        first_low, second_low = min(b.low for b in first), min(b.low for b in second)
        first_range, second_range = _average_range(first), _average_range(second)
        if second_low <= first_low or second_range >= first_range:
            return None
        if not self._rides_rising_average(data, t):
            return None
        return Setup(
            symbol=data.symbol,
            strategy=self.strategy,
            session=t,
            pivot=high,
            structural_stop=second_low,
            detail=MomentumDetail(
                peak_session=peak,
                consolidation_sessions=sessions,
                move=move,
                move_low=move_low,
                momentum_rank=rank,
                first_half_range=first_range,
                second_half_range=second_range,
                depth=1 - min(first_low, second_low) / high,
                adr=data.adr(cfg.adr_sessions)[t],
                close=bars[t].close,
                pivot=high,
            ),
        )

    def _rides_rising_average(self, data: SymbolData, t: int) -> bool:
        cfg = self.risk.strategies.MOMENTUM_BREAKOUT
        back = t - cfg.ma_rising_sessions
        if back < 0:
            return False
        close = data.bars[t].close
        for window in cfg.ride_smas:
            average = data.sma(window)
            now, then = average[t], average[back]
            if now is not None and then is not None and close >= now and now > then:
                return True
        return False
