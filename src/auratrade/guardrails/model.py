"""What every guardrail returns: a finding per rule that clipped or rejected (README §8).

Findings become ``guardrail_events`` rows (rule, proposed vs enforced value) when Phase 5 wires
the guardrails into the live path.
"""

import math
from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    APPROVE = "APPROVE"
    CLIP = "CLIP"
    REJECT = "REJECT"


class GuardrailRule(StrEnum):
    """README §8, one per guardrail row (plus input validation)."""

    INVALID_INPUT = "INVALID_INPUT"
    ENTRIES_BLOCKED = "ENTRIES_BLOCKED"
    """Kill switch, loss or drawdown halts, crisis override, early close, data integrity."""
    TRADABLE_SYMBOL = "TRADABLE_SYMBOL"
    REGIME_CEILING = "REGIME_CEILING"
    FUNDABLE = "FUNDABLE"
    EXIT_PLAN = "EXIT_PLAN"
    """An exit-menu choice outside the bounds in ``risk.yaml``."""
    STOP_REQUIRED = "STOP_REQUIRED"
    PRICE_LEVELS = "PRICE_LEVELS"
    STOP_WIDTH = "STOP_WIDTH"
    KEEP_LOSSES_SMALL = "KEEP_LOSSES_SMALL"
    MAX_CHASE = "MAX_CHASE"
    EXTENSION = "EXTENSION"
    """More than 25% above the 50-day."""
    BASE_COUNT = "BASE_COUNT"
    EARNINGS_BLACKOUT = "EARNINGS_BLACKOUT"
    MAX_RISK = "MAX_RISK"
    HEAT = "HEAT"
    CLUSTER = "CLUSTER"
    MAX_POSITIONS = "MAX_POSITIONS"
    MAX_EXPOSURE = "MAX_EXPOSURE"
    NO_MARGIN = "NO_MARGIN"
    POSITION_SIZE = "POSITION_SIZE"
    WHOLE_SHARES = "WHOLE_SHARES"
    TIGHTEN_ONLY = "TIGHTEN_ONLY"
    TRANCHE_2 = "TRANCHE_2"


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


def finite_positive(*values: float | None) -> bool:
    return all(v is not None and math.isfinite(v) and v > 0 for v in values)
