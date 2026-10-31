"""What every guardrail returns: a finding per rule that clipped or rejected (README §8).

Findings are recorded with each decision (`llm_decisions`, the journal's `clips`).
"""

from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    APPROVE = "APPROVE"
    CLIP = "CLIP"
    REJECT = "REJECT"


class GuardrailRule(StrEnum):
    """README §8, one per guardrail row (plus input validation)."""

    INVALID_INPUT = "INVALID_INPUT"
    # The portfolio manager's (README §8, #5).
    CORE_SHIFT = "CORE_SHIFT"
    """The core level outside the LLM's band."""
    SATELLITE_LIST = "SATELLITE_LIST"
    SATELLITE_WEIGHT = "SATELLITE_WEIGHT"
    SATELLITE_TOTAL = "SATELLITE_TOTAL"
    SATELLITE_HOLD = "SATELLITE_HOLD"
    """A satellite change smaller than ``satellite_min_change``: last month's target kept (#4)."""
    TAX_BUDGET = "TAX_BUDGET"
    """The satellite's sales over the year's tax budget: moved back toward holding (#4)."""
    NO_INCREASE = "NO_INCREASE"
    """Kill switch, incomplete data or a calendar disagreement: hold or reduce."""


type Value = float | int | str | None


@dataclass(frozen=True, slots=True)
class Finding:
    rule: GuardrailRule
    outcome: Outcome
    """CLIP or REJECT: a rule that passes writes no finding."""
    message: str
    proposed: Value = None
    enforced: Value = None


def reject(rule: GuardrailRule, message: str, proposed: Value = None) -> Finding:
    return Finding(rule, Outcome.REJECT, message, proposed)


def clip(rule: GuardrailRule, message: str, proposed: Value, enforced: Value) -> Finding:
    return Finding(rule, Outcome.CLIP, message, proposed, enforced)


def outcome_of(findings: tuple[Finding, ...] | list[Finding]) -> Outcome:
    """REJECT if any finding rejects, else CLIP if any clips, else APPROVE."""
    outcomes = {f.outcome for f in findings}
    if Outcome.REJECT in outcomes:
        return Outcome.REJECT
    return Outcome.CLIP if outcomes else Outcome.APPROVE
