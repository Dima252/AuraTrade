"""The 09:36 check of an approved entry, on the real trigger, limit and stop (README §7.1, §8).

The Portfolio Manager's approval (``guardrails.evaluate``) sized the entry on a planned limit.
At 09:36 the opening range is known, so the trigger, the limit and a momentum / EP low-of-day
stop are computed in code (``execution.triggers``), the stop width is checked again, the order
type is chosen on the last price (a buy-stop-limit, a limit buy at the limit, or a skip), and
the entry is sized in whole shares within its approved risk and reserved exposure.
"""

from dataclasses import dataclass

from auratrade.config import RiskConfig, StopKind
from auratrade.execution.sizing import Tranches, planned_shares, split_tranches
from auratrade.execution.triggers import (
    EntryLevels,
    OpeningRange,
    OrderKind,
    Skip,
    SkipReason,
    entry_levels,
    low_of_day_stop,
    order_decision,
)
from auratrade.guardrails.evaluate import ApprovedEntry
from auratrade.guardrails.levels import StrategyRecord, stop_width
from auratrade.guardrails.model import Finding, GuardrailRule, Outcome, outcome_of, reject

_SKIP_RULES = {
    SkipReason.INVALID: GuardrailRule.INVALID_INPUT,
    SkipReason.NO_PRICE: GuardrailRule.INVALID_INPUT,
    SkipReason.MAX_CHASE: GuardrailRule.MAX_CHASE,
}


def _skipped(skip: Skip) -> Finding:
    return reject(_SKIP_RULES.get(skip.reason, GuardrailRule.PRICE_LEVELS), skip.message)


@dataclass(frozen=True, slots=True)
class OrderPlan:
    kind: OrderKind
    trigger: float
    limit: float
    stop: float
    tranches: Tranches
    """Tranche 1 goes in now; tranche 2 waits for the confirmation gate."""


@dataclass(frozen=True, slots=True)
class OrderCheck:
    plan: OrderPlan | None
    findings: tuple[Finding, ...] = ()

    @property
    def outcome(self) -> Outcome:
        return outcome_of(self.findings)


@dataclass(frozen=True, slots=True)
class MarketAtOpen:
    """What 09:36 knows about the symbol."""

    opening: OpeningRange | None
    price: float | None
    """The last price."""
    low_of_day: float | None
    """The session low so far (momentum / EP stops)."""
    code_pivot: float
    atr: float | None
    adr: float | None


def check_order(
    entry: ApprovedEntry,
    market: MarketAtOpen,
    *,
    equity: float,
    record: StrategyRecord | None,
    risk: RiskConfig,
) -> OrderCheck:
    g = risk.guardrails
    policy = risk.plans.entry[entry.strategy]
    if market.adr is None:
        return OrderCheck(None, (reject(GuardrailRule.INVALID_INPUT, "no ADR"),))
    levels = entry_levels(
        policy.trigger,
        level=entry.pivot,
        code_pivot=market.code_pivot,
        adr=market.adr,
        opening=market.opening,
        max_chase=g.max_chase_above_pivot,
        config=risk.entries,
    )
    if isinstance(levels, Skip):
        return OrderCheck(None, (_skipped(levels),))
    stop = _stop(entry, policy.stop, market.low_of_day)
    if isinstance(stop, Finding):
        return OrderCheck(None, (stop,))
    if too_wide := stop_width(
        entry.strategy,
        trigger=levels.trigger,
        limit=levels.limit,
        stop=stop,
        atr=market.atr,
        adr=market.adr,
        record=record,
        config=g,
    ):
        return OrderCheck(None, (too_wide,))
    kind = order_decision(market.price, levels)
    if isinstance(kind, Skip):
        return OrderCheck(None, (_skipped(kind),))
    return _sized(entry, levels, kind, stop, equity=equity, risk=risk)


def _stop(entry: ApprovedEntry, kind: StopKind, low_of_day: float | None) -> float | Finding:
    if kind is StopKind.STRUCTURAL:
        return entry.stop
    if low_of_day is None:
        return reject(GuardrailRule.INVALID_INPUT, "no low of the day for the stop")
    return low_of_day_stop(low_of_day, entry.stop)


def _sized(
    entry: ApprovedEntry,
    levels: EntryLevels,
    kind: OrderKind,
    stop: float,
    *,
    equity: float,
    risk: RiskConfig,
) -> OrderCheck:
    shares = planned_shares(
        risk_amount=entry.risk_amount,
        limit=levels.limit,
        stop=stop,
        max_position_value=risk.guardrails.max_position_size * equity,
        exposure_room=entry.reserved_exposure,
    )
    if shares == 0:
        return OrderCheck(
            None, (reject(GuardrailRule.WHOLE_SHARES, "the size is under one share"),)
        )
    tranches = split_tranches(shares, entry.tranches, risk.entries)
    return OrderCheck(OrderPlan(kind, levels.trigger, levels.limit, stop, tranches))
