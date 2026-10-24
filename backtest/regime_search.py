"""The 5-pillar regime re-run of the 3.8 baseline backtest (pre-registration step 3; finishes
2.4): the leadership-health pillar from the breakout tracker, and the 16-variant threshold
search over the tuning period.

**Search space** (fixed in `docs/backtests/baseline-preregistration.md`): `CONFIRMED_UPTREND`
from +2 or +3 (the CU -> UP debounce one below it and the `no_ftd` fallback at it, as 2.4's
variants move them); `CORRECTION` at <= -1 or <= -2; `correction_2` on or off; the distribution
pillar at -1 from 6 or 8 days. Everything else stays at its `risk.yaml` value.

**The loop.** Leadership health reads the tracker, the tracker reads every setup of the scan,
and the `EmergingLeader` scan reads the regime's `CORRECTION` spells. Each variant starts from
its 4-pillar regime (leadership 0) and alternates "scan `EmergingLeader` on the spells ->
tracker -> regime" until the regime stops changing. Every link is causal (the regime at T reads
breakouts completed by T, the scan at T reads spells as they stood at T), so the fixed point is
the one a session-by-session run gives.

**Choice:** the variant passing the most of 2.4's five targets; ties go to the fewest switches
a year, then to the fewest settings away from IBD's published ones (+3, <= -1, 6 days), then
to the immediate `CORRECTION` (IBD's).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Protocol

from auratrade.config import Regime, RegimeConfig, RiskConfig, Strategy
from auratrade.regime.classifier import RegimeResult, SessionInputs
from auratrade.regime.inputs import LoadedData, build_inputs
from auratrade.regime.pillars import TrackedBreakout
from auratrade.strategies.base import BaseStrategy
from auratrade.strategies.watchlist import correction_spells
from backtest import regime as bt
from backtest.history_scan import (
    Found,
    History,
    TrackerRow,
    breakouts_by_session,
    scan_pass,
    track,
)

MAX_ITERATIONS = 12
IBD = {"confirmed": 3, "correction": -1, "distribution_days": 6}


@dataclass(frozen=True, slots=True)
class Variant:
    confirmed: int
    correction: int
    correction_sessions: int
    distribution_days: int

    @property
    def name(self) -> str:
        return (
            f"cu{self.confirmed:+d}_corr{self.correction:+d}"
            f"_c{self.correction_sessions}_dist{self.distribution_days}"
        )

    @property
    def description(self) -> str:
        return (
            f"`CONFIRMED_UPTREND` from {self.confirmed:+d}, `CORRECTION` at <= "
            f"{self.correction:+d} after {self.correction_sessions} session(s), distribution -1 "
            f"from {self.distribution_days} days"
        )

    def config(self, cfg: RegimeConfig) -> RegimeConfig:
        transitions = cfg.transitions.model_copy(
            update={
                "confirmed_min_score": self.confirmed,
                "debounce_score": self.confirmed - 1,
                "no_ftd_min_score": self.confirmed,
                "correction_max_score": self.correction,
                "correction_sessions": self.correction_sessions,
            }
        )
        distribution = cfg.distribution.model_copy(update={"down_min_days": self.distribution_days})
        return cfg.model_copy(update={"transitions": transitions, "distribution": distribution})

    @property
    def ibd_distance(self) -> int:
        return (
            (self.confirmed != IBD["confirmed"])
            + (self.correction != IBD["correction"])
            + (self.distribution_days != IBD["distribution_days"])
        )


VARIANTS = tuple(
    Variant(cu, corr, sessions, days)
    for cu, corr, sessions, days in itertools.product((2, 3), (-1, -2), (1, 2), (6, 8))
)


def loaded_data(h: History) -> LoadedData:
    return LoadedData(
        sessions=list(h.sessions),
        session_minutes=dict(h.minutes),
        indexes={symbol: h.series[key].ticker_history() for symbol, key in h.indexes.items()},
        universe=[h.series[k].ticker_history() for k in h.scanned],
        macro=dict(h.macro),
    )


def regime_inputs(h: History, risk: RiskConfig) -> list[SessionInputs]:
    """The pillars' inputs from the second session, without breakouts (each run adds its own)."""
    return build_inputs(loaded_data(h), h.sessions[1:], risk.regime, risk.universe)


def with_breakouts(
    inputs: Sequence[SessionInputs], breakouts: Mapping[date, Sequence[TrackedBreakout]]
) -> list[SessionInputs]:
    return [replace(i, breakouts=tuple(breakouts.get(i.session, ()))) for i in inputs]


def regimes_of(results: Sequence[RegimeResult]) -> dict[date, Regime]:
    return {r.session: r.regime for r in results}


@dataclass(slots=True)
class Converged:
    variant: Variant
    results: list[RegimeResult]
    """The 5-pillar run."""
    four_pillar: list[RegimeResult]
    """The same thresholds with leadership at 0 (comparison 10's "off")."""
    emerging: list[Found]
    """The `EmergingLeader` setups on the final spells."""
    rows: list[TrackerRow]
    """The tracker the final run read."""
    iterations: int


def fixed_points(
    h: History,
    risk: RiskConfig,
    inputs: Sequence[SessionInputs],
    variants: Sequence[Variant],
    core_rows: Sequence[TrackerRow],
    emerging: BaseStrategy,
    *,
    macro_stale_limit: int,
    log: Callable[[str], None] = lambda _: None,
) -> dict[Variant, Converged]:
    """Each variant's 5-pillar regime at its fixed point. ``core_rows`` are the tracker rows of
    the strategies that don't read the regime."""
    if emerging.strategy is not Strategy.EMERGING_LEADER:
        raise ValueError("only EmergingLeader reads the regime")
    configs = {v: v.config(risk.regime) for v in variants}
    four = {v: bt.run(inputs, configs[v], macro_stale_limit) for v in variants}
    current = {v: four[v] for v in variants}
    done: dict[Variant, Converged] = {}
    for iteration in range(1, MAX_ITERATIONS + 1):
        pending = [v for v in variants if v not in done]
        if not pending:
            break
        spells = [correction_spells(regimes_of(current[v])) for v in pending]
        found = scan_pass(h, risk, [emerging], spells, features=False)
        for v, el in zip(pending, found, strict=True):
            rows = list(core_rows) + track(h, el, risk)
            breakouts = breakouts_by_session(rows, h.sessions, risk)
            results = bt.run(with_breakouts(inputs, breakouts), configs[v], macro_stale_limit)
            if regimes_of(results) == regimes_of(current[v]):
                done[v] = Converged(v, results, four[v], el, rows, iteration)
            current[v] = results
        log(f"iteration {iteration}: {len(done)} of {len(variants)} converged")
    missing = [v.name for v in variants if v not in done]
    if missing:
        raise RuntimeError(f"no fixed point after {MAX_ITERATIONS} iterations: {missing}")
    return done


class Judged(Protocol):
    """What the choice reads of a run (``backtest.regime.Summary``)."""

    @property
    def mean_switches(self) -> float: ...

    def checks(self) -> dict[str, bool]: ...


def choose(summaries: Mapping[Variant, Judged]) -> Variant:
    """The most targets passed, then the fewest switches a year, then the closest to IBD."""

    def key(v: Variant) -> tuple[int, float, int, int]:
        s = summaries[v]
        return (-sum(s.checks().values()), s.mean_switches, v.ibd_distance, v.correction_sessions)

    return min(summaries, key=key)
