"""The walking skeleton's rule-only decisions (execution plan task 1.7), pure.

- **Scan-lite:** Trend Template rules 1-5 and 8; pivot = the 20-session high; structural stop =
  the 10-session low, at most 8% below the limit price. The real scans (3.2a) replace it.
- **Pick:** the top by RS rating; no LLM. Phase 5 replaces it with the Portfolio Manager.
- **Sizing:** a fixed risk on the worst fill (the limit price), in whole shares (#122). The
  guardrails' sizing (3.5b) replaces it.
- **Entry:** at 09:36, an OTO buy stop-limit at the trigger; a limit buy at the limit if the
  price is already past the trigger; skipped above the limit (archived README §7.1).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from auratrade.config import SkeletonConfig, TrendTemplateConfig
from auratrade.execution.prices import round_down, round_up
from auratrade.indicators.bars import OHLCV
from auratrade.indicators.volatility import adr_pct
from auratrade.plans.model import stop_tick_up
from auratrade.strategies.trend_template import trend_template


@dataclass(frozen=True, slots=True)
class Levels:
    pivot: float
    trigger: float
    limit: float
    stop: float
    adr: float


def entry_levels(bars: Sequence[OHLCV], cfg: SkeletonConfig, *, max_chase: float) -> Levels | None:
    """The trigger, limit and stop for a breakout above the last `pivot_sessions` high, or None
    when the history is too short or the levels come out inverted."""
    if len(bars) < max(cfg.pivot_sessions, cfg.stop_sessions, cfg.adr_sessions):
        return None
    adr = adr_pct(bars, cfg.adr_sessions)[-1]
    if adr is None:
        return None
    pivot = max(b.high for b in bars[-cfg.pivot_sessions :])
    low = min(b.low for b in bars[-cfg.stop_sessions :])
    trigger = round_up(pivot)
    above = min(cfg.limit_max_above_trigger, cfg.limit_adr_fraction * adr)
    limit = round_down(min(trigger * (1 + above), pivot * (1 + max_chase)))
    stop = stop_tick_up(max(low, limit * (1 - cfg.max_stop)))
    if not 0 < stop < trigger <= limit:
        return None
    return Levels(pivot=pivot, trigger=trigger, limit=limit, stop=stop, adr=adr)


def passes_scan(
    bars: Sequence[OHLCV], rs_rating: float | None, cfg: SkeletonConfig, tt: TrendTemplateConfig
) -> bool:
    """The skeleton's Trend Template rules on the last session."""
    if not bars:
        return False
    ratings: list[float | None] = [None] * (len(bars) - 1)
    ratings.append(rs_rating)
    return trend_template(bars, ratings, tt)[-1].passes(cfg.trend_template_rules)


def size(
    *,
    equity: float,
    risk_fraction: float,
    limit: float,
    stop: float,
    max_position_fraction: float,
    room: float,
) -> int:
    """Whole shares risking `risk_fraction` of equity on a fill at the limit, capped by the
    position-size limit and the exposure left (no margin: README §5.7.9)."""
    per_share = limit - stop
    if per_share <= 0 or limit <= 0 or equity <= 0:
        return 0
    shares = math.floor(equity * risk_fraction / per_share + 1e-9)
    shares = min(shares, math.floor(equity * max_position_fraction / limit))
    shares = min(shares, math.floor(max(room, 0.0) / limit))
    return max(shares, 0)


@dataclass(frozen=True, slots=True)
class EntryDecision:
    trigger: float | None
    """None: a limit buy at the limit (the price is already past the trigger)."""
    skip: str | None = None


def entry_decision(price: float | None, *, trigger: float, limit: float) -> EntryDecision:
    if price is None:
        return EntryDecision(None, skip="no decision price")
    if price > limit:
        return EntryDecision(None, skip=f"price {price:.2f} above the limit {limit:.2f}")
    if price > trigger:
        return EntryDecision(None)
    return EntryDecision(trigger)
