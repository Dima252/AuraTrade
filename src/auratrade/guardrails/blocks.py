"""What blocks every new entry and add, and never an exit or a stop (README §5.13, §8).

The kill switch, the daily / weekly loss halts, the drawdown halt, the crisis override, an
early-close session and a data-integrity failure each stop new entries and tranche-2 adds.
Stops, plan exits and tightenings carry on (CLAUDE.md §3.5).
"""

import math
from collections.abc import Set
from enum import StrEnum

from auratrade.config import HaltsConfig
from auratrade.guardrails.model import Finding, GuardrailRule, reject


class EntryBlock(StrEnum):
    KILL_SWITCH = "KILL_SWITCH"
    DAILY_LOSS_HALT = "DAILY_LOSS_HALT"
    WEEKLY_LOSS_HALT = "WEEKLY_LOSS_HALT"
    DRAWDOWN_HALT = "DRAWDOWN_HALT"
    """Stays until re-enabled by hand; the caller keeps that latch."""
    CRISIS_OVERRIDE = "CRISIS_OVERRIDE"
    EARLY_CLOSE = "EARLY_CLOSE"
    DATA_INTEGRITY = "DATA_INTEGRITY"
    """An `INCOMPLETE` session or a calendar disagreement."""
    CORRECTION = "CORRECTION"
    """The momentum book's overlay: the effective regime is `CORRECTION`, or unknown (#147)."""
    INDEX_TREND = "INDEX_TREND"
    """The momentum book's comparison C4: SPY at or below its long SMA, or unknown."""


def loss_halts(
    *, day_return: float, week_return: float, drawdown: float, config: HaltsConfig
) -> frozenset[EntryBlock]:
    """The halts the account's returns trip: -2% on the day, -5% on the week, 15% below the
    peak. A non-finite input trips all three (fail closed)."""
    if not all(math.isfinite(v) for v in (day_return, week_return, drawdown)):
        return frozenset(
            {EntryBlock.DAILY_LOSS_HALT, EntryBlock.WEEKLY_LOSS_HALT, EntryBlock.DRAWDOWN_HALT}
        )
    out: set[EntryBlock] = set()
    if day_return <= -config.daily_loss:
        out.add(EntryBlock.DAILY_LOSS_HALT)
    if week_return <= -config.weekly_loss:
        out.add(EntryBlock.WEEKLY_LOSS_HALT)
    if drawdown >= config.drawdown:
        out.add(EntryBlock.DRAWDOWN_HALT)
    return frozenset(out)


def entries_blocked(blocks: Set[EntryBlock]) -> Finding | None:
    if blocks:
        names = ", ".join(sorted(blocks))
        return reject(GuardrailRule.ENTRIES_BLOCKED, f"entries and adds are blocked: {names}")
    return None
