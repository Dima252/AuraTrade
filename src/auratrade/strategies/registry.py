"""The strategy registry (README §6): every strategy, the scans run together, and what each
regime funds.

**All scans run every evening**, whatever the regime: the breakout tracker and the watchlist
need every trigger (README §5.12). ``risk.yaml`` decides which strategies are fundable in each
regime; only those compete for the Analyst's slots (task 3.3).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from auratrade.config import FundedStrategy, Regime, RiskConfig, Strategy
from auratrade.indicators.relative_strength import momentum_ranks
from auratrade.strategies.base import BaseStrategy, Setup, SymbolData
from auratrade.strategies.emerging_leader import EmergingLeader
from auratrade.strategies.episodic_pivot import EpisodicPivot
from auratrade.strategies.leader_pullback import LeaderPullback
from auratrade.strategies.momentum_breakout import MomentumBreakout
from auratrade.strategies.trend_template import rs_ratings
from auratrade.strategies.vcp_breakout import VcpBreakout

STRATEGY_CLASSES: tuple[type[BaseStrategy], ...] = (
    VcpBreakout,
    MomentumBreakout,
    EpisodicPivot,
    LeaderPullback,
    EmergingLeader,
)


def build_strategies(risk: RiskConfig) -> dict[Strategy, BaseStrategy]:
    return {cls.strategy: cls(risk) for cls in STRATEGY_CLASSES}


def scan_all(
    strategies: Mapping[Strategy, BaseStrategy], data: SymbolData, at: int | None = None
) -> list[Setup]:
    """Every strategy's setup for one symbol at session ``at`` (default: the last)."""
    return [
        setup for strategy in strategies.values() if (setup := strategy.scan(data, at)) is not None
    ]


def fundable(risk: RiskConfig, regime: Regime) -> tuple[FundedStrategy, ...]:
    """The strategies ``regime`` funds, with their conviction bars (README §6)."""
    limits = getattr(risk.regimes, regime.value)
    funded: tuple[FundedStrategy, ...] = limits.strategies
    return funded


@dataclass(frozen=True, slots=True)
class CrossSection:
    """The universe's ranks on one session (the session every close series ends on)."""

    rs_ratings: dict[str, float]
    momentum_ranks: dict[str, float]
    """The best percentile over ``strategies.MOMENTUM_BREAKOUT.rank_lookbacks``."""


def cross_section(
    closes_by_symbol: Mapping[str, Sequence[float]], risk: RiskConfig
) -> CrossSection:
    """RS ratings and momentum ranks for the universe on its last session (README §5.3).

    ``closes_by_symbol`` must hold exactly the session's universe members: ranks are
    percentiles within it.
    """
    by_lookback = momentum_ranks(closes_by_symbol, risk.strategies.MOMENTUM_BREAKOUT.rank_lookbacks)
    best: dict[str, float] = {}
    for ranks in by_lookback.values():
        for symbol, rank in ranks.items():
            best[symbol] = max(rank, best.get(symbol, rank))
    return CrossSection(
        rs_ratings=rs_ratings(closes_by_symbol, risk.trend_template), momentum_ranks=best
    )
