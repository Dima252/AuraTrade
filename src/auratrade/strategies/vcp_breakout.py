"""`VcpBreakout`: Minervini's volatility contraction pattern (README §5.3, §7.1).

2-4 (up to 6) contractions, each shallower than the last (e.g. 25% -> 12% -> 6%), volume drying
up in the final one, and the pivot at the final contraction's high.

**Contractions** are found from the base's extremes, not with a fixed zigzag: 2.2's
``contraction_count`` uses a 3% reversal, which on volatile names finds 30-50 swings in a long
base. Here the base starts at the highest high of the last ``max_base_sessions``; the first
contraction runs from it to the lowest low after it, and each next one from the highest high
after the previous low to the lowest low after that high. So the highs never rise, the first
contraction is the base's depth, and noise inside a rally from a low doesn't count. Each low is
above the last and each high at or below it, so every contraction is **shallower than the last
by construction**: a pullback that undercuts an earlier contraction's low is part of that
contraction, not a deeper new one. A
contraction's high needs ``swing_confirm_sessions`` after it: a high made in the last few
sessions is the rise towards the pivot, not a new contraction.

The setup is the stock still inside its base on session T, so its close is at or below the
pivot by construction; a close above the base's high starts a new base (too short to count).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from auratrade.config import Strategy
from auratrade.indicators.bars import OHLCV
from auratrade.indicators.bases import Contraction
from auratrade.strategies.base import (
    BaseStrategy,
    Fact,
    Setup,
    SymbolData,
    fraction_fact,
    price_fact,
)


@dataclass(frozen=True, slots=True)
class VcpDetail:
    base_start: int
    base_sessions: int
    """From the base's high to session T."""
    base_high: float
    base_low: float
    contractions: tuple[Contraction, ...]
    final_volume_ratio: float
    """The final contraction's average volume vs the ``volume_sessions`` average at T."""
    close: float

    @property
    def distance_to_pivot(self) -> float:
        """How far the pivot is above the close (0.03 = 3%)."""
        return self.contractions[-1].high / self.close - 1

    def facts(self) -> Mapping[str, Fact]:
        return {
            "base_sessions": self.base_sessions,
            "base_high": price_fact(self.base_high),
            "base_low": price_fact(self.base_low),
            "contractions": len(self.contractions),
            "contraction_depths": " -> ".join(f"{c.depth:.1%}" for c in self.contractions),
            "final_volume_ratio": fraction_fact(self.final_volume_ratio),
            "distance_to_pivot": fraction_fact(self.distance_to_pivot),
        }


def _first_max_high(bars: Sequence[OHLCV], lo: int, hi: int) -> int:
    """The earliest session in ``[lo, hi]`` with the highest high."""
    best = lo
    for t in range(lo + 1, hi + 1):
        if bars[t].high > bars[best].high:
            best = t
    return best


def _last_max_high(bars: Sequence[OHLCV], lo: int, hi: int) -> int:
    best = lo
    for t in range(lo + 1, hi + 1):
        if bars[t].high >= bars[best].high:
            best = t
    return best


def _last_min_low(bars: Sequence[OHLCV], lo: int, hi: int) -> int:
    best = lo
    for t in range(lo + 1, hi + 1):
        if bars[t].low <= bars[best].low:
            best = t
    return best


def contractions(
    bars: Sequence[OHLCV], start: int, end: int, confirm_sessions: int
) -> list[Contraction]:
    """The contractions of the base from the high at ``start`` up to session ``end``.

    Ties go to the later session, so the next search starts after the last touch of a level.
    """
    out: list[Contraction] = []
    high = start
    while end - high >= confirm_sessions:
        low = _last_min_low(bars, high + 1, end)
        out.append(Contraction(high, bars[high].high, low, bars[low].low))
        if low == end:
            break
        high = _last_max_high(bars, low + 1, end)
    return out


class VcpBreakout(BaseStrategy):
    strategy = Strategy.VCP_BREAKOUT
    system_prompt = (
        "VcpBreakout (Minervini's volatility contraction pattern): a stage-2 leader passing all "
        "8 Trend Template rules builds a base of successively shallower pullbacks (for example "
        "25% -> 12% -> 6%), with volume drying up in the final, tightest contraction. The entry "
        "is the breakout above the pivot, the final contraction's high, on rising volume; the "
        "stop goes below the final contraction's low, at most 8% below the limit price. Better "
        "setups have few, clearly shrinking contractions, a tight final contraction on very low "
        "volume, an early-stage base and a strong RS line."
    )

    def scan(self, data: SymbolData, at: int | None = None) -> Setup | None:
        t = data.last if at is None else at
        cfg = self.risk.strategies.VCP_BREAKOUT
        if not self.passes_trend_template(data, t):
            return None
        bars = data.bars
        start = _first_max_high(bars, max(0, t - cfg.max_base_sessions), t)
        if t - start < cfg.min_base_sessions:
            return None
        found = contractions(bars, start, t, cfg.swing_confirm_sessions)
        if not cfg.min_contractions <= len(found) <= cfg.max_contractions:
            return None
        depths = [c.depth for c in found]
        if depths[0] > cfg.max_depth or depths[-1] > cfg.max_final_depth:
            return None
        base_high, base_low = found[0].high, found[0].low
        final = found[-1]
        if final.high < base_low + cfg.pivot_min_position * (base_high - base_low):
            return None
        average = data.average_volume(cfg.volume_sessions)[t]
        if average is None or average <= 0:
            return None
        window = bars[final.high_session + 1 : t + 1]
        ratio = sum(b.volume for b in window) / len(window) / average
        if ratio > cfg.max_final_volume_ratio:
            return None
        return Setup(
            symbol=data.symbol,
            strategy=self.strategy,
            session=t,
            pivot=final.high,
            structural_stop=final.low,
            detail=VcpDetail(
                base_start=start,
                base_sessions=t - start,
                base_high=base_high,
                base_low=base_low,
                contractions=tuple(found),
                final_volume_ratio=ratio,
                close=bars[t].close,
            ),
        )
