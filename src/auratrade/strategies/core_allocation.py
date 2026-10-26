"""The core allocation: the trend dial (README §6-§7; #154; task 3.19).

Pure: no I/O. The live `allocate` job, the twin and the backtest (`sim/portfolio.py`) all call
these functions; the exact rules are fixed in `docs/backtests/portfolio-preregistration.md` §3.

- **The trend** (on R): the month-end total-return value against SMA10, the mean of the last
  ``sma_months`` month-end values including R's. The state turns DOWN below SMA10 x (1 - buffer)
  and UP above SMA10 x (1 + buffer), and otherwise stays; a run's first state has no buffer
  (UP if the value is at least SMA10).
- **The volatility cap** (on R): the sample standard deviation of the last ``vol_sessions`` daily
  returns x sqrt(252) above ``vol_cap``. Too few returns count as capped: fail closed.
- **The drawdown halt:** on at a close where equity is at most (1 - ``drawdown_halt``) of its
  running peak; off at the first R where the trend flips DOWN -> UP (the peak reset to equity)
  or where equity is back at its old peak.
- **The rule level:** ``up_level`` / ``down_level``, at most ``capped_level`` while capped or
  halted. **The LLM's band:** the rule level ± ``max_shift`` within [``min_level``, ``max_level``],
  and only downward while DOWN, capped or halted.
- **Holdings for a level x** on the leveraged fund's multiple L: above 1, the market fund
  1 - w and the leveraged fund w = (x - 1) / (L - 1) (so 1.3x on SSO is 70% SPY + 30% SSO);
  at or below 1, the market fund x and cash 1 - x.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from auratrade.config import PortfolioConfig

TRADING_DAYS = 252
# Weights that differ by less than this are the same weight (float noise in sums of fractions).
WEIGHT_TOLERANCE = 1e-12


class Trend(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


def sma(values: Sequence[float], months: int) -> float | None:
    """The mean of the last ``months`` values; None with fewer."""
    if len(values) < months:
        return None
    return math.fsum(values[-months:]) / months


def trend_state(
    previous: Trend | None, month_ends: Sequence[float], cfg: PortfolioConfig
) -> Trend | None:
    """The state on R from the month-end values through R's; None before SMA10 exists."""
    average = sma(month_ends, cfg.sma_months)
    if average is None:
        return None
    value = month_ends[-1]
    if previous is None:
        return Trend.UP if value >= average else Trend.DOWN
    if value < average * (1 - cfg.buffer):
        return Trend.DOWN
    if value > average * (1 + cfg.buffer):
        return Trend.UP
    return previous


def realized_volatility(returns: Sequence[float], cfg: PortfolioConfig) -> float | None:
    """The annualized sample standard deviation of the last ``vol_sessions`` daily returns."""
    if len(returns) < cfg.vol_sessions or cfg.vol_sessions < 2:
        return None
    return statistics.stdev(returns[-cfg.vol_sessions :]) * math.sqrt(TRADING_DAYS)


def vol_capped(volatility: float | None, cfg: PortfolioConfig) -> bool:
    """Capped above ``vol_cap``, and when the volatility is unknown (fail closed)."""
    return volatility is None or volatility > cfg.vol_cap


@dataclass(frozen=True, slots=True)
class Halt:
    on: bool
    peak: float
    """The running peak of equity (not raised while the halt is on)."""


def start_halt(equity: float) -> Halt:
    return Halt(False, equity)


def halt_at_close(halt: Halt, equity: float, cfg: PortfolioConfig) -> tuple[Halt, bool]:
    """The halt after a session's close, and whether it turned on at this close (the next
    session then sells the leveraged fund, pre-registration §3.4)."""
    if halt.on:
        return halt, False
    peak = max(halt.peak, equity)
    if equity <= peak * (1 - cfg.drawdown_halt):
        return Halt(True, peak), True
    return Halt(False, peak), False


def halt_on_ranking(halt: Halt, flipped_up: bool, equity: float) -> Halt:
    """The halt on R: off on a DOWN -> UP flip (the peak reset to equity) or a new peak."""
    if halt.on and (flipped_up or equity >= halt.peak):
        return Halt(False, equity)
    return halt


def rule_level(state: Trend, capped: bool, halted: bool, cfg: PortfolioConfig) -> float:
    base = cfg.up_level if state is Trend.UP else cfg.down_level
    return min(base, cfg.capped_level) if capped or halted else base


@dataclass(frozen=True, slots=True)
class Band:
    low: float
    high: float


def shift_band(
    state: Trend, capped: bool, halted: bool, level: float, cfg: PortfolioConfig
) -> Band:
    """The core levels the LLM may choose around the rule ``level`` (README §5.4)."""
    low = max(cfg.min_level, level - cfg.max_shift)
    upward = state is Trend.UP and not capped and not halted
    high = min(cfg.max_level, level + cfg.max_shift) if upward else level
    return Band(low, high)


def core_weights(level: float, cfg: PortfolioConfig) -> dict[str, float]:
    """The market fund, the leveraged fund and cash for an S&P exposure of ``level``, as
    fractions of the core; funds at 0 are left out."""
    if not 0 <= level <= cfg.leverage:
        raise ValueError(f"level {level} outside [0, {cfg.leverage}]")
    if level > 1:
        leveraged = (level - 1) / (cfg.leverage - 1)
        weights = {cfg.market: 1 - leveraged, cfg.leveraged: leveraged}
    else:
        weights = {cfg.market: level, cfg.cash: 1 - level}
    return {k: v for k, v in weights.items() if v > WEIGHT_TOLERANCE}


def exposure(weights: Mapping[str, float], cfg: PortfolioConfig) -> float:
    """The S&P exposure of ``weights`` (fractions of equity): the market fund at 1, the
    leveraged fund at its multiple; satellite funds don't count."""
    return weights.get(cfg.market, 0.0) + cfg.leverage * weights.get(cfg.leveraged, 0.0)


def twin_targets(level: float, cfg: PortfolioConfig) -> dict[str, float]:
    """The twin: the rule level on the whole account (README §5.5)."""
    return core_weights(level, cfg)


def book_targets(
    core_level: float, satellite: Mapping[str, float], cfg: PortfolioConfig
) -> dict[str, float]:
    """The book's targets as fractions of equity: the core's share at ``core_level`` plus the
    satellite's weights (already fractions of equity, checked by `guardrails/portfolio.py`)."""
    core_share = 1 - cfg.satellite_share
    out = {k: core_share * v for k, v in core_weights(core_level, cfg).items()}
    for symbol, weight in satellite.items():
        if weight > WEIGHT_TOLERANCE:
            out[symbol] = out.get(symbol, 0.0) + weight
    return out
