"""The pool comparisons of the 3.8 baseline backtest (pre-registration "Comparisons"), on the
tuning period only: each variant changes one dimension of `risk.yaml` against the reference
(the defaults; for exits each strategy's single-trader set) and re-plays the pool.

**Diagnostics, not adoptions.** An option is adopted only if it beats the reference by more than
noise on the tuning period *and* keeps the sign on the holdout, which stays closed until the
pool passes its gate. Cells with fewer than 30 trades keep the default.

Not run here: #1 (pivot vs opening-range trigger on gap opens: the code has no trigger that
stays at the pivot on a gap open), #15 (the code exempts `MomentumBreakout` from the 50-day
rule outright, so "with" needs a code switch), and the book-measured #9-#12, #14 and the book
halves of #5 and #13 (the baseline book's portfolio simulation isn't built).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel

from auratrade.config import (
    ExitPlan,
    ExitSetName,
    GateKind,
    RiskConfig,
    Strategy,
    TrailMA,
)
from auratrade.journal.scorecard import Interval, block_bootstrap
from backtest.pool import (
    BOOTSTRAP_CONFIDENCE,
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    PoolTrade,
)

MIN_CELL_TRADES = 30
PIVOT_STRATEGIES = (Strategy.VCP_BREAKOUT, Strategy.LEADER_PULLBACK, Strategy.EMERGING_LEADER)
ALL = tuple(Strategy)

type Change = Callable[[RiskConfig], RiskConfig]


@dataclass(frozen=True, slots=True)
class Variant:
    number: int
    """The pre-registered comparison."""
    name: str
    description: str
    strategies: tuple[Strategy, ...]
    """The strategies it changes (the others play as the reference)."""
    change: Change


# --- Building the changed configs -------------------------------------------------------------


def _copy[M: BaseModel](model: M, **update: Any) -> M:
    return model.model_copy(update=update)


def plans(risk: RiskConfig, **update: Any) -> RiskConfig:
    return _copy(risk, plans=_copy(risk.plans, **update))


def exit_plan(risk: RiskConfig, strategy: Strategy, plan: ExitPlan) -> RiskConfig:
    """``strategy``'s default plan replaced by ``plan`` (as overrides of every field)."""
    default = risk.plans.default_plans[strategy]
    overrides = _copy(default.overrides, **{f: getattr(plan, f) for f in ExitPlan.model_fields})
    defaults = {**risk.plans.default_plans, strategy: _copy(default, overrides=overrides)}
    return plans(risk, default_plans=defaults)


def edit_exits(strategies: Sequence[Strategy], **fields: Any) -> Change:
    """Set ``fields`` on each strategy's default plan."""

    def change(risk: RiskConfig) -> RiskConfig:
        for s in strategies:
            current = risk.plans.default_exit_plan(s)
            risk = exit_plan(risk, s, _copy(current, **fields))
        return risk

    return change


def full_set(risk: RiskConfig) -> RiskConfig:
    """Every rule of the three sets on, with the strategy's own partial and trail."""
    for s in ALL:
        current = risk.plans.default_exit_plan(s)
        full = _copy(
            current,
            breakeven_after_partial=current.partial is not None,
            breakeven_at_r=True,
            round_trip=True,
            eight_week_hold=True,
            climax_sales=True,
            heavy_volume_reversal=True,
            late_stage_sale=True,
        )
        risk = exit_plan(risk, s, full)
    return risk


def floor(risk: RiskConfig) -> RiskConfig:
    """The hard stop and the strategy's trail only."""
    for s in ALL:
        trail = risk.plans.default_exit_plan(s).trail
        bare = ExitPlan(trail=trail, earnings_cushion=False, regime_tightening=False)
        risk = exit_plan(risk, s, bare)
    return risk


def partial_of(name: ExitSetName | None) -> Change:
    def change(risk: RiskConfig) -> RiskConfig:
        partial = None if name is None else risk.plans.exit_sets[name].partial
        return edit_exits(ALL, partial=partial, breakeven_after_partial=partial is not None)(risk)

    return change


def trail(kind: TrailMA) -> Change:
    return edit_exits(ALL, trail=kind)


def one_tranche(risk: RiskConfig) -> RiskConfig:
    entry = dict(risk.plans.entry)
    for s in PIVOT_STRATEGIES:
        entry[s] = _copy(entry[s], tranches=1, gate=None)
    return plans(risk, entry=entry)


def pullback_breakout_gate(risk: RiskConfig) -> RiskConfig:
    entry = dict(risk.plans.entry)
    entry[Strategy.LEADER_PULLBACK] = _copy(entry[Strategy.LEADER_PULLBACK], gate=GateKind.BREAKOUT)
    return plans(risk, entry=entry)


def no_base_filter(risk: RiskConfig) -> RiskConfig:
    g = _copy(risk.guardrails, late_stage_risk_factor=1.0, max_base_count=99)
    return _copy(risk, guardrails=g)


def ep_stop_cap(multiple: float) -> Change:
    def change(risk: RiskConfig) -> RiskConfig:
        widths = dict(risk.guardrails.stop_widths)
        widths[Strategy.EPISODIC_PIVOT] = _copy(widths[Strategy.EPISODIC_PIVOT], max=multiple)
        return _copy(risk, guardrails=_copy(risk.guardrails, stop_widths=widths))

    return change


def under_pressure_all(risk: RiskConfig) -> RiskConfig:
    up = risk.regimes.UNDER_PRESSURE
    funded = tuple(
        _copy(f, min_conviction=None)
        if f.strategy in (Strategy.VCP_BREAKOUT, Strategy.LEADER_PULLBACK)
        else f
        for f in up.strategies
    )
    regimes = _copy(risk.regimes, UNDER_PRESSURE=_copy(up, strategies=funded))
    return _copy(risk, regimes=regimes)


def climax_sessions(n: int) -> Change:
    def change(risk: RiskConfig) -> RiskConfig:
        return _copy(risk, climax=_copy(risk.climax, fast_gain_min_move_sessions=n))

    return change


def _safety(field: str) -> Change:
    return edit_exits(ALL, **{field: False})


def variants(risk: RiskConfig) -> list[Variant]:
    """The pre-registered pool comparisons, in their order."""

    def lacking(field: str) -> tuple[Strategy, ...]:
        return tuple(s for s in ALL if not getattr(risk.plans.default_exit_plan(s), field))

    k = Strategy
    out = [
        Variant(2, "one_tranche", "one tranche instead of two", PIVOT_STRATEGIES, one_tranche),
        Variant(3, "full_set", "every rule of the three sets", ALL, full_set),
        Variant(3, "floor", "the hard stop and the trail only", ALL, floor),
    ]
    for field, label in (
        ("round_trip", "the round-trip rule"),
        ("breakeven_at_r", "breakeven at +3R"),
        ("time_stop", "the time stop (half at 10 sessions, out at 20)"),
        ("heavy_volume_reversal", "the heavy-volume-reversal exit"),
        ("climax_sales", "climax sales"),
        ("eight_week_hold", "the 8-week hold"),
    ):
        strategies = lacking(field)
        if strategies:
            add = edit_exits(strategies, **{field: True})
            out.append(Variant(4, f"add_{field}", f"{label} added", strategies, add))
    rows: list[tuple[int, str, str, tuple[Strategy, ...], Change]] = [
        (5, "no_earnings_cushion", "the earnings cushion off", ALL, _safety("earnings_cushion")),
        (5, "no_regime_tightening", "regime tightening off", ALL, _safety("regime_tightening")),
        (6, "partial_kullamagi", "Kullamägi's partial", ALL, partial_of(ExitSetName.KULLAMAGI)),
        (6, "partial_oneil", "O'Neil's partial", ALL, partial_of(ExitSetName.ONEIL)),
        (6, "partial_minervini", "Minervini's partial", ALL, partial_of(ExitSetName.MINERVINI)),
        (6, "partial_none", "no partial", ALL, partial_of(None)),
        (7, "trail_10_day", "the 10-day trail", ALL, trail(TrailMA.TEN_DAY)),
        (7, "trail_21_ema", "the 21-day EMA trail", ALL, trail(TrailMA.EMA_21)),
        (7, "trail_50_sma", "the 50-day trail", ALL, trail(TrailMA.SMA_50)),
        (8, "no_base_filter", "no base-count filter", ALL, no_base_filter),
        (
            13,
            "under_pressure_all",
            "`UNDER_PRESSURE` VCPs and pullbacks: all, not the top quarter",
            (k.VCP_BREAKOUT, k.LEADER_PULLBACK),
            under_pressure_all,
        ),
        (16, "ep_stop_1x", "EP stop cap 1 x the pre-gap ADR", (k.EPISODIC_PIVOT,), ep_stop_cap(1)),
        (
            17,
            "pullback_breakout_gate",
            "`LeaderPullback` on the breakout gate",
            (k.LEADER_PULLBACK,),
            pullback_breakout_gate,
        ),
        (18, "climax_10", "climax fast gain over >= 10 sessions", ALL, climax_sessions(10)),
        (18, "climax_20", "climax fast gain over >= 20 sessions", ALL, climax_sessions(20)),
    ]
    out += [Variant(*row) for row in rows]
    return out


# --- Measuring --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Difference:
    strategy: Strategy | None
    """None: every strategy the variant changes, together."""
    reference: float | None
    variant: float | None
    reference_trades: int
    variant_trades: int
    difference: float | None
    interval: Interval | None

    @property
    def beyond_noise(self) -> bool:
        return self.interval is not None and (self.interval.low > 0 or self.interval.high < 0)

    @property
    def enough(self) -> bool:
        return min(self.reference_trades, self.variant_trades) >= MIN_CELL_TRADES


def _by_session(trades: Iterable[PoolTrade]) -> dict[date, list[float]]:
    out: dict[date, list[float]] = defaultdict(list)
    for t in trades:
        if (r := t.r) is not None:
            out[t.entry_day].append(r)
    return out


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def difference(
    reference: Sequence[PoolTrade], variant: Sequence[PoolTrade], strategy: Strategy | None
) -> Difference:
    """The variant's expectancy minus the reference's, with the session-block bootstrap
    interval of the difference (both pools resampled on the same sessions)."""
    a, b = _by_session(reference), _by_session(variant)

    def stat(groups: Sequence[Sequence[float]]) -> float | None:
        ma, mb = _mean(groups[0]), _mean(groups[1])
        return None if ma is None or mb is None else mb - ma

    ra = [r for v in a.values() for r in v]
    rb = [r for v in b.values() for r in v]
    ma, mb = _mean(ra), _mean(rb)
    return Difference(
        strategy=strategy,
        reference=ma,
        variant=mb,
        reference_trades=len(ra),
        variant_trades=len(rb),
        difference=None if ma is None or mb is None else mb - ma,
        interval=block_bootstrap(
            [a, b],
            stat,
            samples=BOOTSTRAP_SAMPLES,
            confidence=BOOTSTRAP_CONFIDENCE,
            seed=BOOTSTRAP_SEED,
        ),
    )


def compare(
    variant: Variant, reference: Sequence[PoolTrade], played: Sequence[PoolTrade]
) -> list[Difference]:
    """Together over the strategies it changes, then each of them."""

    def of(trades: Sequence[PoolTrade], s: Strategy | None) -> list[PoolTrade]:
        wanted = variant.strategies if s is None else (s,)
        return [t for t in trades if t.strategy in wanted]

    return [difference(of(reference, s), of(played, s), s) for s in (None, *variant.strategies)]


def depth_buckets(trades: Iterable[PoolTrade]) -> dict[str, list[PoolTrade]]:
    """#19: momentum trades by their consolidation's depth."""
    from auratrade.strategies.momentum_breakout import MomentumDetail

    out: dict[str, list[PoolTrade]] = {"<= 15%": [], "15-25%": [], "> 25%": []}
    for t in trades:
        detail = t.listing.found.setup.detail
        if not isinstance(detail, MomentumDetail):
            continue
        key = "<= 15%" if detail.depth <= 0.15 else "15-25%" if detail.depth <= 0.25 else "> 25%"
        out[key].append(t)
    return out
