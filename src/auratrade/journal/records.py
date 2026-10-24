"""Simulated trades as JSON: what `baseline_signals` and `verdict_outcomes` store, so each
evening can re-play a trade from its entry session and read back its result."""

from dataclasses import asdict, dataclass
from typing import Any

from auratrade.config import ExitPlan, Strategy
from auratrade.guardrails.evaluate import ApprovedEntry
from auratrade.guardrails.levels import StrategyRecord
from auratrade.guardrails.model import Finding
from auratrade.plans.model import PositionPlan, Rule
from auratrade.sim.trade import FillKind, TradeFill, TradeOutcome, TradeResult


def finding_json(f: Finding) -> dict[str, Any]:
    return {
        "rule": f.rule.value,
        "outcome": f.outcome.value,
        "message": f.message,
        "proposed": f.proposed,
        "enforced": f.enforced,
    }


@dataclass(frozen=True, slots=True)
class StoredEntry:
    """An approved entry and what the simulator needs to play it again."""

    entry: ApprovedEntry
    code_pivot: float
    atr: float | None
    adr: float | None
    dollar_volume: float | None
    equity: float
    """The equity it was sized and checked on at 09:36."""
    record: StrategyRecord | None
    tested_ma: float | None = None

    def to_json(self) -> dict[str, Any]:
        entry = asdict(self.entry)
        entry["strategy"] = self.entry.strategy.value
        entry["exits"] = self.entry.exits.model_dump(mode="json")
        return {
            "entry": entry,
            "code_pivot": self.code_pivot,
            "atr": self.atr,
            "adr": self.adr,
            "dollar_volume": self.dollar_volume,
            "equity": self.equity,
            "record": None if self.record is None else asdict(self.record),
            "tested_ma": self.tested_ma,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StoredEntry":
        e = dict(data["entry"])
        e["strategy"] = Strategy(e["strategy"])
        e["exits"] = ExitPlan.model_validate(e["exits"])
        record = data.get("record")
        return cls(
            entry=ApprovedEntry(**e),
            code_pivot=data["code_pivot"],
            atr=data["atr"],
            adr=data["adr"],
            dollar_volume=data["dollar_volume"],
            equity=data["equity"],
            record=None if record is None else StrategyRecord(**record),
            tested_ma=data.get("tested_ma"),
        )


def result_json(result: TradeResult) -> dict[str, Any]:
    """The result as stored; ``order`` and ``events`` are kept for reading, not re-played."""
    return {
        "outcome": result.outcome.value,
        "findings": [finding_json(f) for f in result.findings],
        "order": None
        if result.order is None
        else {
            "kind": result.order.kind.value,
            "trigger": result.order.trigger,
            "limit": result.order.limit,
            "stop": result.order.stop,
            "tranches": [result.order.tranches.first, result.order.tranches.second],
        },
        "fills": [
            {
                "session": f.session,
                "kind": f.kind.value,
                "shares": f.shares,
                "price": f.price,
                "rule": None if f.rule is None else f.rule.value,
            }
            for f in result.fills
        ],
        "plan": None if result.plan is None else result.plan.model_dump(mode="json"),
        "exit": None if result.exit is None else result.exit.value,
        "events": [
            {"session": n, "kind": e.kind.value, "rule": None if e.rule is None else e.rule.value}
            for n, e in result.events
        ],
        "risk": result.risk,
        "pnl": result.pnl,
        "marks": list(result.marks),
    }


def result_from_json(data: dict[str, Any]) -> TradeResult:
    """Enough of a stored result for the book's account: outcome, fills, plan, exit, R."""
    return TradeResult(
        outcome=TradeOutcome(data["outcome"]),
        fills=tuple(
            TradeFill(
                f["session"],
                FillKind(f["kind"]),
                f["shares"],
                f["price"],
                None if f["rule"] is None else Rule(f["rule"]),
            )
            for f in data["fills"]
        ),
        plan=None if data["plan"] is None else PositionPlan.model_validate(data["plan"]),
        exit=None if data["exit"] is None else Rule(data["exit"]),
        risk=data["risk"],
        pnl=data["pnl"],
        marks=tuple(data["marks"]),
    )
