"""`LeaderPullback`: buying a leader's pullback to its 10- or 21-day EMA (README §5.3, §7.1).

A stage-2 leader (all 8 Trend Template rules, RS rating >= 80) pulls back from a recent high to
within 1 x ATR of its 10- or 21-day EMA, on volume below its 50-day average.

- **The pullback** runs from the highest high of the last ``swing_sessions`` (the latest, on a
  tie) to session T, has lasted at least ``min_pullback_sessions`` and fell at least
  ``min_depth_atr`` x ATR to its low (#137): in an uptrend a shallow two-day dip near the 10-day
  is routine, and without the depth rule the scan found 80 a session.
- **The tested average** is the shortest of ``emas`` that T's low is within
  ``max_atr_from_ema`` x ATR of, above or below. The tranche-2 gate later checks the price is
  back above it (§7.1).

The trigger is T's high, the next session's "prior session's high" (or the opening-range high
above it, as for VCP); the structural stop is the lower of the pullback's low and the tested
average, the support it fails below. The guardrails cap it at 1.5 x ATR below the trigger (3.5b).
"""

from collections.abc import Mapping
from dataclasses import dataclass

from auratrade.config import Strategy
from auratrade.strategies.base import (
    BaseStrategy,
    Fact,
    Setup,
    SymbolData,
    fraction_fact,
    price_fact,
)


@dataclass(frozen=True, slots=True)
class PullbackDetail:
    swing_session: int
    swing_high: float
    pullback_sessions: int
    pullback_low: float
    tested_ema: int
    """The window of the average tested (10 or 21)."""
    ema: float
    atr: float
    volume_ratio: float
    """The pullback's average volume vs the ``volume_sessions`` average at T."""
    rs_rating: float
    close: float

    @property
    def depth(self) -> float:
        return 1 - self.pullback_low / self.swing_high

    def facts(self) -> Mapping[str, Fact]:
        return {
            "tested_ema": self.tested_ema,
            "ema": price_fact(self.ema),
            "close_vs_ema_atr": round((self.close - self.ema) / self.atr, 2),
            "pullback_sessions": self.pullback_sessions,
            "pullback_depth": fraction_fact(self.depth),
            "pullback_volume_ratio": fraction_fact(self.volume_ratio),
            "atr": price_fact(self.atr),
            "rs_rating": round(self.rs_rating, 1),
        }


class LeaderPullback(BaseStrategy):
    strategy = Strategy.LEADER_PULLBACK
    system_prompt = (
        "LeaderPullback (Minervini / O'Neil): a stage-2 leader with an RS rating of 80 or more, "
        "passing all 8 Trend Template rules, pulls back from a recent high to its rising 10- or "
        "21-day EMA on light volume. The entry is a move above the prior session's high as the "
        "stock turns up off the average; the stop goes below the pullback low or the average, "
        "at most 1.5 x ATR below the trigger. Better setups are in a healthy market, early in "
        "the stock's advance, with an orderly, shallow pullback on drying volume, a strong RS "
        "line and a leading industry group. A pullback that slices through the average on heavy "
        "volume is distribution, not a buying opportunity."
    )

    def scan(self, data: SymbolData, at: int | None = None) -> Setup | None:
        t = data.last if at is None else at
        cfg = self.risk.strategies.LEADER_PULLBACK
        rating = data.rs_ratings[t]
        if rating is None or rating < cfg.min_rs_rating or not self.passes_trend_template(data, t):
            return None
        bars = data.bars
        swing = max(range(max(0, t - cfg.swing_sessions), t + 1), key=lambda i: (bars[i].high, i))
        sessions = t - swing
        if sessions < cfg.min_pullback_sessions:
            return None
        atr = data.atr(cfg.atr_sessions)[t]
        if atr is None or atr <= 0:
            return None
        low = bars[t].low
        tested: tuple[int, float] | None = None
        for window in cfg.emas:
            ema = data.ema(window)[t]
            if ema is not None and abs(low - ema) <= cfg.max_atr_from_ema * atr:
                tested = (window, ema)
                break
        if tested is None:
            return None
        average = data.average_volume(cfg.volume_sessions)[t]
        if average is None or average <= 0:
            return None
        pullback = bars[swing + 1 : t + 1]
        ratio = sum(b.volume for b in pullback) / len(pullback) / average
        if ratio > cfg.max_volume_ratio:
            return None
        window, ema = tested
        pullback_low = min(b.low for b in pullback)
        if bars[swing].high - pullback_low < cfg.min_depth_atr * atr:
            return None
        return Setup(
            symbol=data.symbol,
            strategy=self.strategy,
            session=t,
            pivot=bars[t].high,
            structural_stop=min(pullback_low, ema),
            detail=PullbackDetail(
                swing_session=swing,
                swing_high=bars[swing].high,
                pullback_sessions=sessions,
                pullback_low=pullback_low,
                tested_ema=window,
                ema=ema,
                atr=atr,
                volume_ratio=ratio,
                rs_rating=rating,
                close=bars[t].close,
            ),
        )
