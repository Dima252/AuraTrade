"""The shadow baseline book (README §5.12, #29, #49), pure: the LLM book with rules in place of
the LLM.

- **Same inputs:** the same capped top-40 list (the scan's selection, in its order), regime
  gating, guardrails, sizing, entry policies, default exit plans, the confirmation gate for
  tranche 2 and the earnings cushion. EPs come from the morning candidate list, which task 5.6
  builds; until then the baseline has no EPs.
- **Rules instead of the LLM:** it proposes every listed setup **in rank order**, at the code
  pivot and structural stop, the default plan and the effective risk; ``guardrails.evaluate``
  approves them in that order, each approval counting against the next, so it takes setups
  until the limits are full. It holds no regime view: the computed regime governs.
- **No conviction:** where a regime asks for conviction >= 4, the baseline's "conviction >= 4"
  is the top quarter of the list by quality score (README §6). Those setups are proposed at 4,
  the rest at 3.
- **Its own account:** a starting equity (``baseline.start_equity``, the paper account's), its
  P&L at each close, its own progressive-exposure multiplier (replayed over its days) and its
  own strategy records for "keep losses small".
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from auratrade.config import RiskConfig, Strategy
from auratrade.guardrails.evaluate import Context, Entry, Evaluation, SetupFacts, evaluate
from auratrade.guardrails.levels import StrategyRecord
from auratrade.journal.numbers import ClosedTrade, strategy_record
from auratrade.journal.risk_multiplier import Day, replay
from auratrade.sim.trade import FillKind, TradeResult

TOP_CONVICTION = 4
OTHER_CONVICTION = 3


@dataclass(frozen=True, slots=True)
class Listed:
    """One setup of the capped list, in the scan's order."""

    facts: SetupFacts
    score: float


def convictions(scores: Sequence[float]) -> list[int]:
    """The top quarter by quality score (rounded up) at 4, the rest at 3."""
    top = math.ceil(len(scores) / 4)
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    best = set(order[:top])
    return [TOP_CONVICTION if i in best else OTHER_CONVICTION for i in range(len(scores))]


def proposals(listed: Sequence[Listed]) -> list[Entry]:
    return [
        Entry(
            symbol=item.facts.symbol,
            strategy=item.facts.strategy,
            pivot=item.facts.pivot,
            stop=item.facts.structural_stop,
            risk=None,
            conviction=conviction,
        )
        for item, conviction in zip(
            listed, convictions([item.score for item in listed]), strict=True
        )
    ]


def decide(listed: Sequence[Listed], context: Context, risk: RiskConfig) -> Evaluation:
    """The baseline's entries for the next session: the list through the guardrails."""
    return evaluate(proposals(listed), context, risk)


# ---- the account --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BookTrade:
    """A baseline trade as simulated so far."""

    strategy: Strategy
    sessions: Sequence[date]
    """The sessions it was simulated over, from the entry session (``result.marks`` align)."""
    result: TradeResult

    @property
    def closed(self) -> bool:
        return self.result.exit is not None

    @property
    def exit_session(self) -> date | None:
        return self.sessions[len(self.result.marks) - 1] if self.closed else None

    def pnl_at(self, day: date) -> float:
        """P&L at ``day``'s close: 0 before the entry, the final P&L from the exit on."""
        if not self.result.marks or day < self.sessions[0]:
            return 0.0
        held = [i for i, d in enumerate(self.sessions[: len(self.result.marks)]) if d <= day]
        return self.result.marks[held[-1]] * self.result.risk

    @property
    def cost(self) -> float:
        return sum(
            f.shares * f.price
            for f in self.result.fills
            if f.kind in (FillKind.ENTRY, FillKind.TRANCHE_2)
        )


def equity_on(start_equity: float, trades: Sequence[BookTrade], day: date) -> float:
    return start_equity + sum(t.pnl_at(day) for t in trades)


def multiplier(
    start_equity: float, trades: Sequence[BookTrade], sessions: Sequence[date], risk: RiskConfig
) -> float:
    """The book's multiplier after ``sessions`` (its days so far, oldest first): the README §8
    state machine replayed from a new account."""
    days = []
    for day in sessions:
        closed = [t for t in trades if t.exit_session == day and t.result.r is not None]
        days.append(
            Day(
                equity=equity_on(start_equity, trades, day),
                trade_rs=tuple(t.result.r for t in closed if t.result.r is not None),
            )
        )
    _, readings = replay(start_equity, days, risk.multiplier)
    if readings:
        return readings[-1].multiplier
    return risk.multiplier.prove_it_multiplier


def records(trades: Sequence[BookTrade], risk: RiskConfig) -> Mapping[Strategy, StrategyRecord]:
    """Each strategy's closed trades, oldest exit first, for "keep losses small"."""
    closed = sorted((t for t in trades if t.closed), key=lambda t: t.exit_session or date.min)
    history = [
        ClosedTrade(t.strategy, t.result.pnl / t.cost, t.result.r or 0.0)
        for t in closed
        if t.cost > 0
    ]
    window = risk.guardrails.keep_losses_small_trades
    return {s: strategy_record(history, s, window=window) for s in Strategy}
