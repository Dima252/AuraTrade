"""`EpisodicPivot`: Kullamägi's episodic pivot, as the **backtest proxy** (archived README §5.3,
§7.1).

Live, an EP is detected in the morning from the pre-market gap, the news and the earnings beat
(task 5.6). On daily bars the proxy comes in two views of the gap session:

- ``scan``, **after the close:** a gap of at least ``min_gap`` on at least ``min_volume_ratio``
  times the prior average volume. The breakout tracker and the evening watchlist read it.
- ``morning``, **at the open:** the gap alone, with nothing from the gap session but its open.
  The backtest pool trades these; it checks the opening volume from minute bars at 09:36
  (``min_opening_volume_ratio``, ``backtest.pool``) and reads the features at the prior close.
  The full-day volume of ``scan`` isn't known when the entry goes in (#143).

What the proxy can't check, and the report notes (archived README §5.3): the earnings **beat** and
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
from dataclasses import dataclass, replace

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
    volume_ratio: float | None
    """The gap session's volume / the prior average (known only after the close; None in the
    ``morning`` view)."""
    day_gain: float | None
    """Close / prior close - 1 on the gap session (known only after the close; None in the
    ``morning`` view)."""
    earnings: bool
    """The gap session is an earnings reaction session."""
    prior_moves: Mapping[int, float | None]
    """The return over each ``rally_lookbacks`` window, up to the prior close."""
    pre_gap_adr: float | None

    def facts(self) -> Mapping[str, Fact]:
        return {
            "gap": fraction_fact(self.gap),
            "volume_ratio": None if self.volume_ratio is None else round(self.volume_ratio, 2),
            "day_gain": None if self.day_gain is None else fraction_fact(self.day_gain),
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
        """The gap session on heavy full-day volume, read after its close."""
        t = data.last if at is None else at
        gap = self._gap(data, t)
        if gap is None:
            return None
        detail, average = gap
        bar, prior = data.bars[t], data.bars[t - 1].close
        volume_ratio = bar.volume / average
        if volume_ratio < self.risk.strategies.EPISODIC_PIVOT.min_volume_ratio:
            return None
        return self._setup(
            data,
            t,
            replace(detail, volume_ratio=volume_ratio, day_gain=bar.close / prior - 1),
        )

    def morning(self, data: SymbolData, at: int | None = None) -> Setup | None:
        """The gap session as its open shows it: the gap, and nothing later in the session."""
        t = data.last if at is None else at
        gap = self._gap(data, t)
        return None if gap is None else self._setup(data, t, gap[0])

    def _gap(self, data: SymbolData, t: int) -> tuple[EpisodicPivotDetail, float] | None:
        """The gap's detail without the after-close facts, and the prior average volume."""
        cfg = self.risk.strategies.EPISODIC_PIVOT
        bars = data.bars
        # The average of the sessions before the gap (as `indicators.earnings.earnings_gap`),
        # from the cached series so a backtest pays O(1) per session.
        average = data.average_volume(cfg.volume_sessions)[t - 1] if t > 0 else None
        if average is None or average <= 0:
            return None
        prior = bars[t - 1].close
        gap = bars[t].open / prior - 1
        if gap < cfg.min_gap:
            return None
        moves = {
            n: prior / bars[t - 1 - n].close - 1 if t - 1 - n >= 0 else None
            for n in cfg.rally_lookbacks
        }
        detail = EpisodicPivotDetail(
            gap=gap,
            volume_ratio=None,
            day_gain=None,
            earnings=t in data.earnings_sessions,
            prior_moves=moves,
            pre_gap_adr=data.adr(cfg.adr_sessions)[t - 1],
        )
        return detail, average

    def _setup(self, data: SymbolData, t: int, detail: EpisodicPivotDetail) -> Setup:
        return Setup(
            symbol=data.symbol,
            strategy=self.strategy,
            session=t,
            pivot=data.bars[t].open,
            structural_stop=data.bars[t - 1].close,
            detail=detail,
        )
