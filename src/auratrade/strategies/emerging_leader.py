"""`EmergingLeader`: O'Neil's first leaders out of a correction (archived README §5.3, §6, §7.1).

A stock with an RS rating >= 85, Trend Template rules 1 and 5-8 (the moving-average stack lags
right after a correction), at a new 52-week high or with its RS line at one, that held up best
during the correction.

- **The correction** is the regime's latest `CORRECTION` spell that started by session T (cut
  at T if it hadn't ended yet). It counts for ``max_sessions_since_correction`` after its end.
- **"Held up best"** (the user's choice, #137): the stock's drawdown over the correction, from
  ``lead_sessions`` before its first session, is at most ``max_drawdown_vs_index`` times SPY's
  over the same sessions. Drawdown = the largest close-to-close fall from a running peak. O'Neil
  notes that leaders' bases correct about 1.5-2.5 times the market; 1.5 is the strict end.

The pivot is the 52-week high (T's high when T made it); the structural stop is the low of the
last ``stop_sessions``, capped by the guardrails at 8% below the limit price (3.5b).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from auratrade.config import Strategy
from auratrade.indicators.series import rolling_max
from auratrade.strategies.base import (
    BaseStrategy,
    Correction,
    Fact,
    Setup,
    SymbolData,
    fraction_fact,
)


def max_drawdown(closes: Sequence[float]) -> float:
    """The largest fall from a running peak, as a fraction (0.2 = -20%); 0 for a rising series."""
    if not closes:
        return 0.0
    peak, worst = closes[0], 0.0
    for close in closes:
        peak = max(peak, close)
        worst = max(worst, 1 - close / peak)
    return worst


@dataclass(frozen=True, slots=True)
class EmergingDetail:
    new_price_high: bool
    rs_line_new_high: bool
    drawdown: float
    index_drawdown: float
    sessions_since_correction: int
    """0 while the correction lasts."""
    rs_rating: float
    close: float
    pivot: float

    def facts(self) -> Mapping[str, Fact]:
        return {
            "new_52_week_high": self.new_price_high,
            "rs_line_new_high": self.rs_line_new_high,
            "correction_drawdown": fraction_fact(self.drawdown),
            "spy_drawdown": fraction_fact(self.index_drawdown),
            "drawdown_vs_spy": round(self.drawdown / self.index_drawdown, 2),
            "sessions_since_correction": self.sessions_since_correction,
            "distance_to_pivot": fraction_fact(self.pivot / self.close - 1),
            "rs_rating": round(self.rs_rating, 1),
        }


def _latest(corrections: Sequence[Correction], t: int) -> Correction | None:
    started = [c for c in corrections if c.start <= t]
    return started[-1] if started else None


class EmergingLeader(BaseStrategy):
    strategy = Strategy.EMERGING_LEADER
    system_prompt = (
        "EmergingLeader (O'Neil): right after a correction, the first stocks to reach new highs "
        "are often the next cycle's leaders. This one has an RS rating of 85 or more, is at a "
        "new 52-week high or has its RS line at one, and fell no more than 1.5x the market "
        "during the correction. Its moving averages may not have re-aligned yet. The entry is "
        "the new-high pivot (or the opening-range high above it), as a starter position in an "
        "early uptrend; the stop is structural, at most 8% below the limit price. Better setups "
        "have an RS line leading price to new highs, strong earnings and sales growth, a "
        "leading industry group and a sound base built during the correction."
    )

    def scan(self, data: SymbolData, at: int | None = None) -> Setup | None:
        t = data.last if at is None else at
        cfg = self.risk.strategies.EMERGING_LEADER
        rating = data.rs_ratings[t]
        benchmark = data.benchmark_closes
        if rating is None or rating < cfg.min_rs_rating or benchmark is None:
            return None
        if not self.passes_trend_template(data, t):
            return None
        correction = _latest(data.corrections, t)
        if correction is None:
            return None
        end = t if correction.end is None or correction.end > t else correction.end
        since = t - end
        if since > cfg.max_sessions_since_correction:
            return None
        bars = data.bars
        high = self._highs(data, cfg.high_sessions)[t]
        rs_high = self._rs_line_highs(data, benchmark, cfg.high_sessions)[t]
        if high is None or rs_high is None:
            return None
        new_price_high = bars[t].high >= high
        rs_line_new_high = bars[t].close / benchmark[t] >= rs_high
        if not (new_price_high or rs_line_new_high):
            return None
        first = max(0, correction.start - cfg.lead_sessions)
        index_drawdown = max_drawdown(benchmark[first : end + 1])
        if index_drawdown <= 0:
            return None
        drawdown = max_drawdown([b.close for b in bars[first : end + 1]])
        if drawdown > cfg.max_drawdown_vs_index * index_drawdown:
            return None
        return Setup(
            symbol=data.symbol,
            strategy=self.strategy,
            session=t,
            pivot=high,
            structural_stop=min(b.low for b in bars[max(0, t - cfg.stop_sessions + 1) : t + 1]),
            detail=EmergingDetail(
                new_price_high=new_price_high,
                rs_line_new_high=rs_line_new_high,
                drawdown=drawdown,
                index_drawdown=index_drawdown,
                sessions_since_correction=since,
                rs_rating=rating,
                close=bars[t].close,
                pivot=high,
            ),
        )

    @staticmethod
    def _highs(data: SymbolData, window: int) -> list[float | None]:
        return data.cached("high", window, lambda: rolling_max([b.high for b in data.bars], window))

    @staticmethod
    def _rs_line_highs(
        data: SymbolData, benchmark: Sequence[float], window: int
    ) -> list[float | None]:
        return data.cached(
            "rs_line_high",
            window,
            lambda: rolling_max(
                [b.close / c for b, c in zip(data.bars, benchmark, strict=True)], window
            ),
        )
