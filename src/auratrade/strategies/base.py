"""The strategy interface and the inputs every scan reads (archived README §5.3, §6 "Strategy
registry").

Each strategy implements ``scan()``, ``build_llm_context()``, ``system_prompt``, ``entry_policy``,
``exit_policy`` and ``default_risk``. Scans are pure: one symbol's history in, at most one setup
out, with the **code-computed pivot and structural stop** that the Analyst's levels are checked
against (archived README §8, "Price levels").

A scan evaluates one session ``at`` of a ``SymbolData`` and reads sessions ``<= at`` only, so
the evening scan (``at`` = the last session) and a backtest (every ``at`` of one history) run the
same code. The series a scan needs are computed once per symbol and cached.

Cross-sectional inputs (RS rating, momentum rank) need the whole universe on the same session,
so the caller ranks them (``registry.cross_section`` for the evening scan) and passes each
symbol's values in, aligned with its bars.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from auratrade.config import EntryPolicy, ExitPlan, Regime, RiskConfig, Strategy
from auratrade.indicators.bars import OHLCV
from auratrade.indicators.moving_averages import ema, sma
from auratrade.indicators.series import Series, check_same_length, rolling_mean
from auratrade.indicators.volatility import adr_pct, atr
from auratrade.strategies.trend_template import TrendTemplate, passes_for, trend_template

type Fact = float | int | bool | str | None


@dataclass(frozen=True, slots=True)
class Correction:
    """A `CORRECTION` spell of the regime, in the symbol's bar positions (README §5.2)."""

    start: int
    end: int | None
    """Its last session; ``None`` while it lasts. A scan at an earlier session cuts it there."""


@dataclass(frozen=True, slots=True)
class SymbolData:
    """One symbol's history as the scans read it: split-adjusted bars, oldest first, with the
    cross-sectional values aligned to them (``None`` where the symbol wasn't ranked)."""

    symbol: str
    bars: Sequence[OHLCV]
    templates: Sequence[TrendTemplate]
    rs_ratings: Sequence[float | None]
    momentum_ranks: Sequence[float | None]
    """The best momentum-rank percentile over ``strategies.MOMENTUM_BREAKOUT.rank_lookbacks``."""
    earnings_sessions: frozenset[int] = frozenset()
    """Bar positions of earnings reaction sessions (``indicators.earnings.reaction_session``)."""
    benchmark_closes: Sequence[float] | None = None
    """SPY's closes on the same sessions, for the RS line and the market's drawdown."""
    corrections: tuple[Correction, ...] = ()
    """The regime's `CORRECTION` spells, oldest first, the same for every symbol."""
    _cache: dict[tuple[str, int], Series] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        check_same_length(self.bars, self.templates, self.rs_ratings, self.momentum_ranks)
        if self.benchmark_closes is not None:
            check_same_length(self.bars, self.benchmark_closes)

    @classmethod
    def build(
        cls,
        symbol: str,
        bars: Sequence[OHLCV],
        *,
        rs_ratings: Sequence[float | None],
        momentum_ranks: Sequence[float | None],
        risk: RiskConfig,
        earnings_sessions: frozenset[int] = frozenset(),
        benchmark_closes: Sequence[float] | None = None,
        corrections: tuple[Correction, ...] = (),
    ) -> "SymbolData":
        return cls(
            symbol=symbol,
            bars=bars,
            templates=trend_template(bars, rs_ratings, risk.trend_template),
            rs_ratings=rs_ratings,
            momentum_ranks=momentum_ranks,
            earnings_sessions=earnings_sessions,
            benchmark_closes=benchmark_closes,
            corrections=corrections,
        )

    @property
    def last(self) -> int:
        return len(self.bars) - 1

    def cached(self, kind: str, window: int, compute: Callable[[], Series]) -> Series:
        """A series computed once per symbol, keyed by its kind and window."""
        key = (kind, window)
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    def sma(self, window: int) -> Series:
        return self.cached("sma", window, lambda: sma([b.close for b in self.bars], window))

    def ema(self, window: int) -> Series:
        return self.cached("ema", window, lambda: ema([b.close for b in self.bars], window))

    def atr(self, window: int) -> Series:
        return self.cached("atr", window, lambda: atr(self.bars, window))

    def adr(self, window: int) -> Series:
        return self.cached("adr", window, lambda: adr_pct(self.bars, window))

    def average_volume(self, window: int) -> Series:
        """The average volume of the ``window`` sessions ending at each session."""
        return self.cached(
            "volume", window, lambda: rolling_mean([b.volume for b in self.bars], window)
        )


class SetupDetail(Protocol):
    """A strategy's own findings, as rounded facts for the Analyst (archived README §9, compact
    context)."""

    def facts(self) -> Mapping[str, Fact]: ...


@dataclass(frozen=True, slots=True)
class Setup:
    """A scan's result at one session: a candidate for the next session's entry."""

    symbol: str
    strategy: Strategy
    session: int
    """The bar position the scan evaluated (the evening scan's session T)."""
    pivot: float
    """The code pivot: the trigger level, the Analyst's pivot within ±3% of it, the max chase
    and the limit measured from it (archived README §7.1, §8)."""
    structural_stop: float
    """The support the setup fails below: the lowest stop the Analyst may give (tighter only).
    Strategies with a low-of-day stop (momentum, EP) set it at 09:36, never below this."""
    detail: SetupDetail


def price_fact(value: float) -> float:
    return round(value, 2)


def fraction_fact(value: float) -> float:
    return round(value, 4)


class BaseStrategy(ABC):
    """One strategy of the registry. Subclasses set ``strategy`` and ``system_prompt`` and
    implement ``scan``."""

    strategy: ClassVar[Strategy]
    system_prompt: ClassVar[str]
    """The strategy's section of the Analyst prompt: what the setup is and what makes it good.
    Task 4.2 writes the full prompts and versions them; this is the setup's definition."""

    def __init__(self, risk: RiskConfig) -> None:
        self.risk = risk

    @abstractmethod
    def scan(self, data: SymbolData, at: int | None = None) -> Setup | None:
        """The setup at session ``at`` (default: the last), or ``None``."""

    def passes_trend_template(self, data: SymbolData, at: int) -> bool:
        return passes_for(data.templates[at], self.strategy, self.risk.trend_template)

    def build_llm_context(self, setup: Setup) -> dict[str, Fact]:
        """The setup's facts for the Analyst: the code levels and the strategy's findings.
        The shared features (base count, RS line, ...) are added by the context builder (4.3)."""
        if setup.strategy is not self.strategy:
            raise ValueError(f"{setup.strategy} setup given to {self.strategy}")
        return {
            "strategy": self.strategy.value,
            "pivot": price_fact(setup.pivot),
            "structural_stop": price_fact(setup.structural_stop),
            "stop_width": fraction_fact(1 - setup.structural_stop / setup.pivot),
            **setup.detail.facts(),
        }

    @property
    def entry_policy(self) -> EntryPolicy:
        return self.risk.plans.entry[self.strategy]

    @property
    def exit_policy(self) -> ExitPlan:
        """The default exit plan (archived README §7.3): the strategy's trader set plus its
        overrides."""
        return self.risk.plans.default_exit_plan(self.strategy)

    def default_risk(self, regime: Regime) -> float | None:
        """The regime's base risk per trade if the strategy is fundable in it, else ``None``.
        Before the progressive-exposure multiplier, the `EARLY_UPTREND` step and the late-stage
        factor (archived README §6, §8)."""
        limits = getattr(self.risk.regimes, regime.value)
        funded = any(f.strategy is self.strategy for f in limits.strategies)
        return limits.base_risk if funded else None
