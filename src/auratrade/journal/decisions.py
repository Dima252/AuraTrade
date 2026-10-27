"""The Portfolio Manager's decision on the database (README §5.4, §5.7, §9; task 4.3).

- **On E** the ALLOCATED row `allocate` wrote on R gives the facts; the headlines are fetched
  now. The tools read the funds' bars through R, Alpaca's news and the decision history.
- **On an event** (the halt, the stop, a corporate action; from task 5.x, by hand until then)
  the facts are gathered at once: the book through the last close, the trend from the last R.
- **Every decision is recorded:** the prompt (`prompt_versions`), the call (`llm_decisions`:
  the answer or the failure, the answering model, the cost), the enforced targets and their
  clips (`portfolio_targets`), and for a rebalance the month's journal row.

Increases are allowed only with the kill switch off, R's data COMPLETE and E's calendar
cross-checked (README §8); otherwise the guardrails hold every fund at its current weight.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, orm, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from auratrade.calendar.sessions import MarketCalendar
from auratrade.charts import daily_chart
from auratrade.config import RiskConfig
from auratrade.data.news import NewsItem
from auratrade.db.flags import read_flags
from auratrade.db.models import LlmDecision, PortfolioJournal, PortfolioTarget, PromptVersion
from auratrade.guardrails.portfolio import satellite_tax
from auratrade.journal.portfolio import (
    FundBars,
    FundData,
    build_facts,
    decided_targets,
    fetch_funds,
    finite,
    past_decisions,
    rebalance_at,
    run_books,
    trial_start,
    window_start,
)
from auratrade.llm.client import ClaudeClient, Turn
from auratrade.llm.portfolio_facts import (
    EtfFacts,
    Event,
    PastDecision,
    PmFacts,
    context,
    etf_facts,
    facts_from_json,
    facts_to_json,
)
from auratrade.llm.portfolio_manager import PmAnswer, PmResult, decide
from auratrade.llm.portfolio_prompt import system_prompt

NY = ZoneInfo("America/New_York")
ROLE = "PORTFOLIO_MANAGER"


class NewsSource(Protocol):
    def news(
        self, symbols: Sequence[str] | None, start: datetime, end: datetime
    ) -> list[NewsItem]: ...


@dataclass
class LiveSources:
    """`PmSources` on the funds' bars through R, Alpaca's news and the decision history."""

    conn: Connection
    risk: RiskConfig
    data: FundData
    news_source: NewsSource
    news_start: datetime
    news_end: datetime
    session: date

    def etf_facts(self, symbol: str) -> EtfFacts:
        market = self.data.closes(self.risk.portfolio.market, total_return=True)
        mine = self.data.closes(symbol, total_return=True)
        days = [d for d in sorted(market) if d in mine]
        return etf_facts(
            symbol,
            [mine[d] for d in days],
            [market[d] for d in days],
            self.risk.portfolio_manager,
        )

    def chart(self, symbol: str) -> bytes:
        bars = self.data.bars[symbol]
        market = self.data.closes(self.risk.portfolio.market)
        bars = [b for b in bars if b.session in market]
        return daily_chart(
            symbol,
            [b.session for b in bars],
            bars,
            self.risk.charts,
            benchmark_closes=[market[b.session] for b in bars],
        )

    def news(self, symbol: str, limit: int) -> Sequence[NewsItem]:
        items = self.news_source.news([symbol], self.news_start, self.news_end)
        return list(reversed(items))[:limit]

    def decision_history(self, months: int) -> Sequence[PastDecision]:
        return past_decisions(self.conn, self.session, months)


def increases_allowed(
    conn: Connection, calendar: MarketCalendar, ranking: date, session: date
) -> bool:
    r, e = calendar.session_for(ranking), calendar.session_for(session)
    return (
        read_flags(conn).trading_enabled
        and r is not None
        and r.data_status == "COMPLETE"
        and e is not None
        and e.calendar_agrees
    )


def deadline(risk: RiskConfig, session: date, now: datetime) -> datetime | None:
    """12:00 ET on E (README §3). A replay of an earlier session (``--session`` in the past)
    has none: its deadline passed before it ran."""
    if now.astimezone(NY).date() > session:
        return None
    at: time = risk.portfolio_manager.deadline
    return datetime.combine(session, at, tzinfo=NY)


def headlines(news: NewsSource, risk: RiskConfig, start: datetime, end: datetime) -> list[NewsItem]:
    p = risk.portfolio
    symbols = sorted({p.market, p.leveraged, *p.satellite_list})
    items = news.news(symbols, start, end)
    return list(reversed(items))[: risk.portfolio_manager.headlines]


@dataclass(frozen=True)
class Morning:
    """What a decision needs besides the database."""

    client: ClaudeClient
    funds: FundBars
    news: NewsSource
    now: datetime


def decide_rebalance(
    conn: Connection, calendar: MarketCalendar, risk: RiskConfig, m: Morning, session: date
) -> dict[str, Any]:
    """On E: decide from R's ALLOCATED row; a DECIDED row is left as it is."""
    row = (
        orm.Session(conn)
        .scalars(
            select(PortfolioTarget).where(
                PortfolioTarget.kind == "REBALANCE", PortfolioTarget.execution_session == session
            )
        )
        .one_or_none()
    )
    if row is None:
        if is_execution(calendar, session):
            raise RuntimeError(f"no allocation for E {session}: allocate didn't run on R")
        return {"skipped": "not an execution session"}
    if row.status == "DECIDED":
        return {"skipped": "already decided"}
    facts = facts_from_json(row.facts)
    news_start, news_end = news_window(calendar, risk, session, m.now)
    facts = replace(facts, headlines=headlines(m.news, risk, news_start, news_end))
    first = window_start(calendar, trial_start(conn) or row.ranking_session, risk)
    data = fetch_funds(m.funds, risk, first, row.ranking_session)
    sources = LiveSources(conn, risk, data, m.news, news_start, news_end, session)
    allowed = increases_allowed(conn, calendar, row.ranking_session, session)
    result = decide(
        m.client,
        facts,
        risk,
        sources,
        increases_allowed=allowed,
        deadline=deadline(risk, session, m.now),
    )
    record(conn, risk, m.client, row.id, facts, result, journal=True)
    return summary(result, allowed)


def decide_event(
    conn: Connection,
    calendar: MarketCalendar,
    risk: RiskConfig,
    m: Morning,
    session: date,
    event: Event,
) -> dict[str, Any]:
    """On an event: the book through the last close, the trend from the last R."""
    last = (
        orm.Session(conn)
        .scalars(
            select(PortfolioTarget)
            .where(PortfolioTarget.kind == "REBALANCE")
            .order_by(PortfolioTarget.ranking_session.desc())
            .limit(1)
        )
        .one_or_none()
    )
    if last is None:
        return {"skipped": "the trial hasn't started"}
    close = calendar.previous_session(session).day
    start = trial_start(conn) or last.ranking_session
    first = window_start(calendar, start, risk)
    data = fetch_funds(m.funds, risk, first, close)
    market = data.market(risk, first)
    # Ranked at the last close as if it were R, for the account; the trend is the last R's.
    books = run_books(market, risk, start, close, decided_targets(conn), rank_at_end=True)
    fresh = build_facts(conn, risk, data, books, close, session)
    previous = facts_from_json(last.facts)
    halted = fresh.trend.halted or event.kind == "HALT"
    trend = replace(previous.trend, halted=halted)
    news_start, news_end = news_window(calendar, risk, session, m.now)
    facts = replace(
        fresh,
        ranking_session=last.ranking_session,
        trend=trend,
        event=event,
        headlines=headlines(m.news, risk, news_start, news_end),
    )
    sources = LiveSources(conn, risk, data, m.news, news_start, news_end, session)
    result = decide(m.client, facts, risk, sources, increases_allowed=False, deadline=None)
    inserted = conn.execute(
        pg_insert(PortfolioTarget)
        .values(
            kind="EVENT",
            ranking_session=last.ranking_session,
            execution_session=session,
            status="ALLOCATED",
            trend=trend.state.value,
            capped=trend.capped,
            halted=halted,
            rule_level=result.enforced.rule_level,
            twin_targets=last.twin_targets,
            facts=finite(facts_to_json(facts)),
            risk_hash=risk.config_hash,
        )
        .returning(PortfolioTarget.id)
    ).scalar_one()
    record(conn, risk, m.client, inserted, facts, result, journal=False)
    return summary(result, False)


def record(
    conn: Connection,
    risk: RiskConfig,
    client: ClaudeClient,
    row_id: int,
    facts: PmFacts,
    result: PmResult,
    *,
    journal: bool,
) -> None:
    prompt = system_prompt(risk.portfolio)
    conn.execute(
        pg_insert(PromptVersion)
        .values(id=prompt.id, version=prompt.version, text=prompt.text)
        .on_conflict_do_nothing()
    )
    outcome = result.outcome
    decision_id = conn.execute(
        pg_insert(LlmDecision)
        .values(
            session_date=facts.session,
            role=ROLE,
            prompt_id=prompt.id,
            requested_model=client.model,
            model=outcome.model,
            effort=client.effort.value,
            failure=None if outcome.failure is None else outcome.failure.value,
            detail=outcome.detail,
            refusal_category=outcome.refusal_category,
            answer=None if outcome.value is None else finite(outcome.value.model_dump()),
            turns=[_turn(t) for t in outcome.turns],
            cost_usd=outcome.cost_usd,
            cost_alert=outcome.cost_alert,
            fallback=outcome.fallback,
        )
        .returning(LlmDecision.id)
    ).scalar_one()
    e = result.enforced
    ctx = context(facts, increases_allowed=True)
    findings = [
        {"rule": f.rule.value, "outcome": f.outcome.value, "message": f.message} for f in e.findings
    ]
    conn.execute(
        update(PortfolioTarget)
        .where(PortfolioTarget.id == row_id)
        .values(
            status="DECIDED",
            llm_decision_id=decision_id,
            level=e.level,
            core_level=e.core_level,
            satellite=e.satellite,
            targets=e.targets,
            findings=findings,
            held_satellite=result.held_satellite,
            fell_back=result.fell_back,
            satellite_tax=satellite_tax(e.satellite, ctx, risk.portfolio),
            decided_at=datetime.now(UTC),
        )
    )
    if journal:
        answer = outcome.value
        values = {
            "execution_session": facts.session,
            "targets_id": row_id,
            "summary": answer.summary if answer else f"Held: the LLM failed ({outcome.failure}).",
            "decision": finite(_decision(answer, result)),
            "clips": findings,
        }
        conn.execute(
            pg_insert(PortfolioJournal)
            .values(**values)
            .on_conflict_do_update(index_elements=["execution_session"], set_=values)
        )


def summary(result: PmResult, allowed: bool) -> dict[str, Any]:
    e = result.enforced
    return {
        "fell_back": result.outcome.failure,
        "held_satellite": result.held_satellite,
        "book_level": round(e.level, 4),
        "satellite": {k: round(v, 4) for k, v in e.satellite.items()},
        "clips": [f.rule.value for f in e.findings],
        "increases_allowed": allowed,
        "cost_usd": round(result.outcome.cost_usd, 4),
        "model": result.outcome.model,
        "prompt": result.prompt_id,
    }


def _decision(answer: PmAnswer | None, result: PmResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "core_shift": result.decision.core_shift,
        "satellite_proposed": dict(result.decision.satellite),
        "fell_back": result.outcome.failure,
    }
    if answer is not None:
        out |= answer.model_dump()
    return out


def _turn(t: Turn) -> dict[str, Any]:
    return {
        "model": t.model,
        "stop_reason": t.stop_reason,
        "seconds": t.seconds,
        "input_tokens": t.input_tokens,
        "cache_read_tokens": t.cache_read_tokens,
        "cache_write_tokens": t.cache_write_tokens,
        "output_tokens": t.output_tokens,
        "thinking_tokens": t.thinking_tokens,
        "tools": list(t.tools),
        "cost_usd": t.cost_usd,
        "fallback": t.fallback,
        "request_id": t.request_id,
    }


def news_window(
    calendar: MarketCalendar, risk: RiskConfig, session: date, now: datetime
) -> tuple[datetime, datetime]:
    """From ``headline_sessions`` back to now, but never past E's open: a replay of an earlier
    session must not read the news that came after it (look-ahead)."""
    back = calendar.n_sessions_back(session, risk.portfolio_manager.headline_sessions)
    e = calendar.session_for(session)
    end = now if e is None else min(now, e.open_at)
    return back.open_at, end


def is_execution(calendar: MarketCalendar, session: date) -> bool:
    """Whether ``session`` is an E: R is one of the sessions just before it (a half day
    between them moves E later)."""
    for back in range(1, 5):
        found = rebalance_at(calendar, calendar.n_sessions_back(session, back).day)
        if found is not None:
            return found.execution == session
    return False
