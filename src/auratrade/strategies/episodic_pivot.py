"""`EpisodicPivot`: Kullamägi's episodic pivot, as the **backtest proxy** (README §5.3, §7.1).

Live, an EP is detected in the morning from the pre-market gap, the news and the earnings beat
(task 5.6). On daily bars the proxy is a gap of at least ``min_gap`` on at least
``min_volume_ratio`` times the prior average volume, evaluated on the gap session itself.

What the proxy can't check, and the report notes (README §5.3): the earnings **beat** and
growth (no free surprise history back to 2016), and the news behind a non-earnings gap. The two
preferences, a gap on an earnings reaction session and no big rally over the prior 3-6 months,
are reported as facts for the ranking rather than filtered on.

Levels on daily bars: the live trigger is the gap day's 5-minute opening-range high, which needs
intraday bars, so the proxy's pivot is the gap day's open (the opening range's floor) and the
simulator (3.6) decides the fill. The structural stop is the prior close: an EP that fills its
gap has failed. The order-time stop is the low of the day at 09:36, capped at 1.5 x the pre-gap
ADR below the trigger. EPs are exempt from the Trend Template and the 50-day rule.
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
)


@dataclass(frozen=True, slots=True)
class EpisodicPivotDetail:
    gap: float
    """Open / prior close - 1."""
    volume_ratio: float
    day_gain: float
    """Close / prior close - 1 on the gap session (known only after the close)."""
    earnings: bool
    """The gap session is an earnings reaction session."""
    prior_moves: Mapping[int, float | None]
    """The return over each ``rally_lookbacks`` window, up to the prior close."""
    pre_gap_adr: float | None

    def facts(self) -> Mapping[str, Fact]:
        return {
            "gap": fraction_fact(self.gap),
            "volume_ratio": round(self.volume_ratio, 2),
            "day_gain": fraction_fact(self.day_gain),
            "earnings_gap": self.earnings,
            **{
                f"prior_move_{n}": None if move is None else fraction_fact(move)
                for n, move in self.prior_moves.items()
            },
            "pre_gap_adr": None if self.pre_gap_adr is None else fraction_fact(self.pre_gap_adr),
        }


class EpisodicPivot(BaseStrategy):
    strategy = Strategy.EPISODIC_PIVOT
    system_prompt = (
        "EpisodicPivot (Kullamägi): a gap of at least 10% on earnings or news, on heavy volume, "
        "that can re-rate a stock, best when the company shows strong growth and a beat and the "
        "stock hadn't rallied over the prior 3-6 months (a neglected name). The entry is the "
        "5-minute opening-range high on the gap day; the stop is the low of the day, at most "
        "1.5 x the pre-gap ADR below the trigger. It is exempt from the Trend Template and the "
        "50-day rule: the catalyst replaces the trend."
    )

    def scan(self, data: SymbolData, at: int | None = None) -> Setup | None:
        t = data.last if at is None else at
        cfg = self.risk.strategies.EPISODIC_PIVOT
        bars = data.bars
        # The average of the sessions before the gap (as `indicators.earnings.earnings_gap`),
        # from the cached series so a backtest pays O(1) per session.
        average = data.average_volume(cfg.volume_sessions)[t - 1] if t > 0 else None
        if average is None or average <= 0:
            return None
        prior = bars[t - 1].close
        bar = bars[t]
        gap, volume_ratio = bar.open / prior - 1, bar.volume / average
        if gap < cfg.min_gap or volume_ratio < cfg.min_volume_ratio:
            return None
        moves = {
            n: prior / bars[t - 1 - n].close - 1 if t - 1 - n >= 0 else None
            for n in cfg.rally_lookbacks
        }
        return Setup(
            symbol=data.symbol,
            strategy=self.strategy,
            session=t,
            pivot=bars[t].open,
            structural_stop=prior,
            detail=EpisodicPivotDetail(
                gap=gap,
                volume_ratio=volume_ratio,
                day_gain=bar.close / prior - 1,
                earnings=t in data.earnings_sessions,
                prior_moves=moves,
                pre_gap_adr=data.adr(cfg.adr_sessions)[t - 1],
            ),
        )
