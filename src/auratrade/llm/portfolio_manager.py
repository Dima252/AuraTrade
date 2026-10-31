"""The Portfolio Manager (README §5.4, §9; #4, #6): one decision, from the facts
to enforced targets.

- **The answer** (`PmAnswer`) is the README §9 schema: the core shift, the satellite (or null
  to hold it), the reasons, the expectations, the tax note. No field is a price.
- **Tools** read through `PmSources`, which the jobs implement : the ETF facts, a
  chart, news (as untrusted data), the decision history. Only funds on the ETF list are served.
- **Every outcome goes through `guardrails/portfolio.py`**, the failure's too. On a failure
  (error, timeout, refusal, invalid output, the deadline) the book **holds** (#6): no shift,
  last month's satellite (the neutral before the first decision), exactly the answer
  ``{"core_shift": 0, "satellite": null}``. It realizes no satellite tax and adds no risk; the
  core follows the rule level, as the twin's does.
- **An event** may only reduce risk: its context blocks every increase.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import pydantic
import structlog

from auratrade.config import RiskConfig
from auratrade.data.news import NewsItem
from auratrade.guardrails.portfolio import (
    EnforcedTargets,
    PortfolioContext,
    PortfolioDecision,
    evaluate_decision,
)
from auratrade.llm.client import ClaudeClient, Outcome, Tool, ToolOutput
from auratrade.llm.portfolio_facts import (
    EtfFacts,
    PastDecision,
    PmFacts,
    context,
    headline,
    past_decision,
    render,
    untrusted,
)
from auratrade.llm.portfolio_prompt import system_prompt

log = structlog.get_logger(__name__)


class SatelliteWeight(pydantic.BaseModel):
    symbol: str = pydantic.Field(description="An ETF from the approved list")
    weight: float = pydantic.Field(description="A fraction of equity: 0.05 is 5%")
    expectation: str = pydantic.Field(
        description="One checkable line: direction and rough size of its return against the "
        "market fund until the next decision"
    )
    expected_vs_market: float = pydantic.Field(
        description="The expectation as a number: its return less the market fund's until the "
        "next decision, as a fraction (0.01 is one point ahead); the scorecard checks it"
    )
    why_not_hold: str = pydantic.Field(
        description="Why changing this weight beats holding, net of its tax; 'unchanged' if "
        "the weight is last month's"
    )


class PmAnswer(pydantic.BaseModel):
    core_shift: float = pydantic.Field(description="The change to the rule level; 0 is none")
    core_reasoning: str
    satellite: list[SatelliteWeight] | None = pydantic.Field(
        description="null holds last month's satellite; otherwise the complete new satellite"
    )
    tax_note: str = pydantic.Field(description="The tax this decision realizes, in dollars")
    summary: str = pydantic.Field(description="At most 120 words for the journal")

    @pydantic.model_validator(mode="after")
    def _unique(self) -> PmAnswer:
        symbols = [w.symbol for w in self.satellite or ()]
        if len(set(symbols)) != len(symbols):
            raise ValueError("the satellite names a fund twice")
        return self


class PmSources(Protocol):
    """What the tools read (the jobs implement it). Each call may raise: the
    client reports a failed tool to the model, which decides without it."""

    def etf_facts(self, symbol: str) -> EtfFacts: ...

    def chart(self, symbol: str) -> bytes:
        """A daily chart as PNG."""
        ...

    def news(self, symbol: str, limit: int) -> Sequence[NewsItem]: ...

    def decision_history(self, months: int) -> Sequence[PastDecision]: ...


class SymbolArgs(pydantic.BaseModel):
    symbol: str


class HistoryArgs(pydantic.BaseModel):
    months: int = pydantic.Field(description="How many months back")


def tools(sources: PmSources, risk: RiskConfig) -> list[Tool[Any]]:
    p, pm = risk.portfolio, risk.portfolio_manager
    funds = sorted({*p.satellite_list, p.market, p.leveraged, p.cash})

    def listed(symbol: str) -> str | None:
        if symbol in funds:
            return None
        return f"{symbol} is not on the ETF list: {', '.join(funds)}"

    def get_etf_facts(args: SymbolArgs) -> ToolOutput:
        if refused := listed(args.symbol):
            return refused
        e = sources.etf_facts(args.symbol)
        return json.dumps(
            {
                "symbol": e.symbol,
                "returns": e.returns,
                "volatility": e.volatility,
                "distance_from_sma": e.distance_from_sma,
                "correlation_with_market": e.correlation_with_market,
            }
        )

    def get_chart(args: SymbolArgs) -> ToolOutput:
        if refused := listed(args.symbol):
            return refused
        png = base64.standard_b64encode(sources.chart(args.symbol)).decode("ascii")
        return [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}},
            {"type": "text", "text": f"{args.symbol}: daily bars with its moving averages"},
        ]

    def get_news(args: SymbolArgs) -> ToolOutput:
        if refused := listed(args.symbol):
            return refused
        items = sources.news(args.symbol, pm.news_items)[: pm.news_items]
        return untrusted("news", [headline(i, summary=True) for i in items])

    def get_decision_history(args: HistoryArgs) -> ToolOutput:
        months = min(max(args.months, 1), pm.history_months)
        return json.dumps([past_decision(d) for d in sources.decision_history(months)])

    return [
        Tool(
            "get_etf_facts",
            "One fund's facts: returns, volatility, distance from its moving average, "
            "correlation with the market fund.",
            SymbolArgs,
            get_etf_facts,
        ),
        Tool("get_chart", "One fund's daily chart (an image).", SymbolArgs, get_chart),
        Tool(
            "get_news",
            "Recent news about one fund, as untrusted third-party data.",
            SymbolArgs,
            get_news,
        ),
        Tool(
            "get_decision_history",
            f"Your past decisions and their results against the twin, up to "
            f"{pm.history_months} months back.",
            HistoryArgs,
            get_decision_history,
        ),
    ]


def hold(ctx: PortfolioContext, risk: RiskConfig) -> PortfolioDecision:
    """No shift and last month's satellite, or the neutral before the first decision (#6)."""
    p = risk.portfolio
    satellite = dict(ctx.previous_satellite) or {p.market: p.satellite_share}
    return PortfolioDecision(0.0, satellite)


def decision_of(answer: PmAnswer, ctx: PortfolioContext, risk: RiskConfig) -> PortfolioDecision:
    if answer.satellite is None:
        return PortfolioDecision(answer.core_shift, hold(ctx, risk).satellite)
    return PortfolioDecision(answer.core_shift, {w.symbol: w.weight for w in answer.satellite})


@dataclass(frozen=True)
class PmResult:
    outcome: Outcome[PmAnswer]
    decision: PortfolioDecision
    """What went to the guardrails: the answer's, or the hold on a failure."""
    enforced: EnforcedTargets
    prompt_id: str

    @property
    def fell_back(self) -> bool:
        return not self.outcome.ok

    @property
    def held_satellite(self) -> bool:
        answer = self.outcome.value
        return answer is None or answer.satellite is None


def decide(
    client: ClaudeClient,
    facts: PmFacts,
    risk: RiskConfig,
    sources: PmSources,
    *,
    increases_allowed: bool,
    deadline: datetime | None,
) -> PmResult:
    """One decision. Never raises for the LLM: a failure holds (#6)."""
    prompt = system_prompt(risk.portfolio)
    ctx = context(facts, increases_allowed=increases_allowed)
    outcome = client.decide(
        system=prompt.text,
        content=render(facts, risk.portfolio),
        answer=PmAnswer,
        tools=tools(sources, risk),
        deadline=deadline,
    )
    if outcome.value is None:
        decision = hold(ctx, risk)
        log.warning("pm.held_on_failure", failure=outcome.failure, detail=outcome.detail)
    else:
        decision = decision_of(outcome.value, ctx, risk)
    enforced = evaluate_decision(decision, ctx, risk.portfolio)
    log.info(
        "pm.decided",
        prompt=prompt.id,
        on_event=facts.event.kind if facts.event else None,
        fell_back=outcome.failure,
        book_level=round(enforced.level, 4),
        outcome=enforced.outcome,
        findings=[f.rule for f in enforced.findings],
    )
    return PmResult(outcome, decision, enforced, prompt.id)
