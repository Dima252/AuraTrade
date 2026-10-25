"""Guardrails on actions for held positions: tranche 2, tightenings and exits (archived README §8).

- **Tranche 2:** only to a two-tranche entry not yet added to, through its confirmation gate, on
  sessions 1 to ``tranche2_max_sessions`` (the entry session is 1), and not after a dip below
  the pivot blocked it. The caller adds the blocks, fundability and the earnings blackout.
- **Tightenings** go through ``plans.versioning.tighten``: anything that loosens the plan
  (including the round-trip rule) is rejected.
- **Exits** need a held position and never sell more than it holds. Nothing blocks them.
"""

from pydantic import ValidationError

from auratrade.config import GuardrailsConfig, PlansConfig
from auratrade.guardrails.model import Finding, GuardrailRule, reject
from auratrade.plans.entry import GateResult
from auratrade.plans.model import PositionPlan
from auratrade.plans.versioning import LooseningError, Tightening, tighten


def tranche2_check(
    plan: PositionPlan,
    *,
    gate: GateResult,
    session: int,
    blocked_by_plan: bool,
    config: GuardrailsConfig,
) -> Finding | None:
    if plan.entry.tranches != 2 or plan.tranche2_filled:
        return reject(GuardrailRule.TRANCHE_2, "no tranche 2 left to add")
    if not gate.passed:
        return reject(
            GuardrailRule.TRANCHE_2, "the confirmation gate failed: " + "; ".join(gate.failures)
        )
    if not 1 <= session <= config.tranche2_max_sessions:
        return reject(
            GuardrailRule.TRANCHE_2,
            f"session {session} is outside the tranche-2 window (1-{config.tranche2_max_sessions})",
            session,
        )
    if blocked_by_plan:
        return reject(GuardrailRule.TRANCHE_2, "a close below the pivot blocked tranche 2")
    return None


def tighten_check(
    plan: PositionPlan, request: Tightening, *, price: float, config: PlansConfig
) -> PositionPlan | Finding:
    """The next plan version, or why the request is rejected. A request the plan model refuses
    (a stop below the initial stop) is a loosening too."""
    try:
        return tighten(plan, request, price=price, config=config)
    except (LooseningError, ValidationError) as exc:
        return reject(GuardrailRule.TIGHTEN_ONLY, str(exc))
    except ValueError as exc:
        return reject(GuardrailRule.INVALID_INPUT, str(exc), price)


def exit_check(shares: int | None, held: int) -> Finding | None:
    """``shares`` None sells the whole position."""
    if shares is not None and not 0 < shares <= held:
        return reject(
            GuardrailRule.INVALID_INPUT, f"cannot sell {shares} of {held} shares held", shares
        )
    return None
