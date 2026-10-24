"""Trades replayed through ``sim/`` (README §5.12), pure.

- **Sim R:** an LLM-book trade replayed as decided (its levels, exit plan and risk). Every
  comparison with the baseline uses it, never the paper fills (§5.12, #83).
- **Default-plan R** (the plan counterfactual): the same entry, same levels, under its
  strategy's default exit plan and the default risk. The gap to sim R measures the Portfolio
  Manager's exit-plan and sizing choices; the scorecard can't see them, since it uses defaults.

``sized_entry`` sizes an entry the way ``guardrails.evaluate`` approves one (limit, max position
size, whole shares, tranches, reserved exposure) without the portfolio checks: the scorecard
plays every verdict "as if it had been taken", whatever the book held.
"""

from dataclasses import dataclass, replace

from auratrade.config import ExitPlan, RiskConfig, Strategy
from auratrade.execution.sizing import planned_shares
from auratrade.execution.triggers import limit_price
from auratrade.guardrails.evaluate import ApprovedEntry
from auratrade.guardrails.levels import StrategyRecord
from auratrade.sim.trade import SimSession, TradeResult, TradeSetup, simulate_trade


def sized_entry(
    *,
    symbol: str,
    strategy: Strategy,
    pivot: float,
    stop: float,
    code_pivot: float,
    adr: float,
    risk_fraction: float,
    equity: float,
    risk: RiskConfig,
    exits: ExitPlan | None = None,
    conviction: int | None = None,
) -> ApprovedEntry | None:
    """The entry sized on its planned limit, or None when it is under one share."""
    g = risk.guardrails
    planned_limit = limit_price(
        pivot,
        adr=adr,
        code_pivot=code_pivot,
        max_chase=g.max_chase_above_pivot,
        config=risk.entries,
    )
    risk_amount = risk_fraction * equity
    shares = planned_shares(
        risk_amount=risk_amount,
        limit=planned_limit,
        stop=stop,
        max_position_value=g.max_position_size * equity,
        exposure_room=equity,
    )
    if shares == 0:
        return None
    tranches = risk.plans.entry[strategy].tranches
    if tranches == 2 and shares < risk.entries.min_two_tranche_shares:
        tranches = 1
    return ApprovedEntry(
        symbol=symbol,
        strategy=strategy,
        pivot=pivot,
        stop=stop,
        conviction=conviction,
        exits=exits if exits is not None else risk.plans.default_exit_plan(strategy),
        risk=risk_fraction,
        risk_amount=risk_amount,
        planned_limit=planned_limit,
        planned_shares=shares,
        tranches=tranches,
        reserved_exposure=shares * planned_limit,
    )


def with_defaults(
    entry: ApprovedEntry,
    *,
    code_pivot: float,
    adr: float,
    default_risk: float,
    equity: float,
    risk: RiskConfig,
) -> ApprovedEntry | None:
    """``entry`` with its strategy's default exit plan and ``default_risk``; same levels."""
    return sized_entry(
        symbol=entry.symbol,
        strategy=entry.strategy,
        pivot=entry.pivot,
        stop=entry.stop,
        code_pivot=code_pivot,
        adr=adr,
        risk_fraction=default_risk,
        equity=equity,
        risk=risk,
        conviction=entry.conviction,
    )


@dataclass(frozen=True, slots=True)
class Counterfactual:
    sim: TradeResult
    """The trade as decided."""
    default_plan: TradeResult | None
    """Under the default plan and risk; None when that size is under one share."""

    @property
    def sim_r(self) -> float | None:
        return self.sim.r

    @property
    def default_plan_r(self) -> float | None:
        return None if self.default_plan is None else self.default_plan.r


def counterfactual(
    setup: TradeSetup,
    sessions: list[SimSession],
    *,
    default_risk: float,
    equity: float,
    record: StrategyRecord | None,
    risk: RiskConfig,
) -> Counterfactual:
    """Sim R and default-plan R for one trade, on the same sessions and fill model."""
    sim = simulate_trade(setup, sessions, equity=equity, record=record, risk=risk)
    if setup.adr is None:
        return Counterfactual(sim, None)
    default = with_defaults(
        setup.entry,
        code_pivot=setup.code_pivot,
        adr=setup.adr,
        default_risk=default_risk,
        equity=equity,
        risk=risk,
    )
    if default is None:
        return Counterfactual(sim, None)
    played = simulate_trade(
        replace(setup, entry=default), sessions, equity=equity, record=record, risk=risk
    )
    return Counterfactual(sim, played)
