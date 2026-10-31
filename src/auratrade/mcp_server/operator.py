"""The operator tools (README §5.10): the database, the broker and the logs, read only.

Each takes an open connection (the server's engine is read-only, `sources.read_only_engine`)
and returns plain JSON: dates as ISO strings, non-finite numbers as null. No order ids, keys
or settings are returned.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, extract, func, orm, select
from sqlalchemy.dialects.postgresql import distinct_on

from auratrade.calendar.rebalance import rebalances
from auratrade.config import RiskConfig
from auratrade.db.flags import HALT_DRAWDOWN, read_flags
from auratrade.db.models import (
    AccountSnapshot,
    JobRunRow,
    LlmDecision,
    MarketSession,
    Mismatch,
    PortfolioJournal,
    PortfolioTarget,
    SystemFlag,
    TaxLot,
)
from auratrade.execution import funds, invariant
from auratrade.journal.tax_lots import open_lots, realized_by_year
from auratrade.mcp_server.sources import BrokerReader

ERROR_CHARS = 2000


def plain(value: Any) -> Any:
    """JSON-ready: dates as ISO strings, non-finite floats as None, containers recursed."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [plain(v) for v in value]
    return value


def record(value: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = plain(value)
    return out


@dataclass(frozen=True, slots=True)
class _Day:
    day: date
    is_early_close: bool


def next_rebalance(conn: Connection, today: date) -> dict[str, Any] | None:
    """The next R and E from the synced calendar: the first rebalance whose E isn't past."""
    days = [
        _Day(d, early)
        for d, early in conn.execute(
            select(MarketSession.session_date, MarketSession.is_early_close)
        )
    ]
    upcoming = next((r for r in rebalances(days) if r.execution >= today), None)
    if upcoming is None:
        return None
    return {
        "ranking_session": upcoming.ranking.isoformat(),
        "execution_session": upcoming.execution.isoformat(),
        "today_is": "R"
        if today == upcoming.ranking
        else "E"
        if today == upcoming.execution
        else None,
    }


def _run(row: JobRunRow, *, details: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job": row.job,
        "session": row.session_date,
        "status": row.status,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "trading_mode": row.trading_mode,
        "error": None if row.error is None else row.error[:ERROR_CHARS],
    }
    if details:
        out["details"] = row.details
    return record(out)


def status(conn: Connection, *, today: date, environment: str) -> dict[str, Any]:
    flags = read_flags(conn)
    halt = conn.execute(
        select(SystemFlag.value, SystemFlag.details).where(SystemFlag.name == HALT_DRAWDOWN)
    ).first()
    halt_details = dict(halt.details or {}) if halt is not None else {}
    session = orm.Session(conn)
    open_mismatches = session.scalars(
        select(Mismatch).where(Mismatch.resolved_at.is_(None)).order_by(Mismatch.id)
    ).all()
    last_runs = session.scalars(
        select(JobRunRow)
        .ext(distinct_on(JobRunRow.job))
        .order_by(JobRunRow.job, JobRunRow.started_at.desc())
    ).all()
    return record(
        {
            "environment": environment,
            "today": today,
            "kill_switch": "off (increases allowed)"
            if flags.trading_enabled
            else "ON (increases blocked)",
            "drawdown_halt": {
                # A missing row reads as on (fail closed, db/flags.py).
                "on": flags.halt_drawdown,
                "peak": halt_details.get("peak"),
                "since": halt_details.get("since"),
            },
            "open_mismatches": [
                {
                    "id": m.id,
                    "kind": m.kind,
                    "symbol": m.symbol,
                    "detected_at": m.detected_at,
                    "details": m.details,
                }
                for m in open_mismatches
            ],
            "next_rebalance": next_rebalance(conn, today),
            "last_runs": [_run(r, details=False) for r in last_runs],
        }
    )


def jobs(conn: Connection, *, job: str | None, status: str | None, limit: int) -> dict[str, Any]:
    query = select(JobRunRow).order_by(JobRunRow.started_at.desc()).limit(max(limit, 1))
    if job is not None:
        query = query.where(JobRunRow.job == job)
    if status is not None:
        query = query.where(JobRunRow.status == status.upper())
    rows = orm.Session(conn).scalars(query).all()
    return {"runs": [_run(r, details=True) for r in rows]}


def _last_close(conn: Connection) -> dict[str, Any] | None:
    snap = (
        orm.Session(conn)
        .scalars(select(AccountSnapshot).order_by(AccountSnapshot.session_date.desc()).limit(1))
        .first()
    )
    if snap is None:
        return None
    return record(
        {
            "session": snap.session_date,
            "equity": snap.equity,
            "cash": snap.cash,
            "peak_equity": snap.peak_equity,
            "drawdown": snap.drawdown,
            "holdings": snap.holdings,
            "tax_owed": snap.tax_owed,
            "equity_after_tax": None if snap.tax_owed is None else snap.equity - snap.tax_owed,
        }
    )


def portfolio(conn: Connection, risk: RiskConfig, broker: BrokerReader | None) -> dict[str, Any]:
    """The broker's account now, the stops' protection against the plan, the last close."""
    out: dict[str, Any] = {"last_close": _last_close(conn), "broker": None}
    if broker is None:
        return out
    account = broker.account()
    positions = broker.positions()
    orders = broker.open_orders()
    levels = funds.levels(conn, risk.portfolio, [p.symbol for p in positions])
    protections = invariant.check(
        positions, orders, levels, unstopped=funds.unstopped(risk.portfolio)
    )
    stop_ids = {o.id for p in protections.values() for o in p.stops}
    out["broker"] = plain(
        {
            "market_open": broker.is_market_open(),
            "equity": account.equity,
            "cash": account.cash,
            "long_market_value": account.long_market_value,
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "avg_entry_price": p.avg_entry_price,
                    "price": p.current_price,
                    "value": None if p.current_price is None else p.qty * p.current_price,
                }
                for p in sorted(positions, key=lambda p: p.symbol)
            ],
            "stops": [
                {
                    "symbol": s.symbol,
                    "qty": s.qty,
                    "covered": s.covered,
                    "plan_level": levels.get(s.symbol),
                    "protected": s.ok,
                    "problems": sorted(s.problems),
                    "orders": [
                        {
                            "qty": o.remaining_qty,
                            "stop_price": o.stop_price,
                            "time_in_force": o.time_in_force.value,
                            "status": o.status.value,
                        }
                        for o in s.stops
                    ],
                }
                for s in sorted(protections.values(), key=lambda s: s.symbol)
            ],
            "other_open_orders": [
                {
                    "symbol": o.symbol,
                    "side": o.side.value,
                    "type": o.type.value,
                    "qty": o.remaining_qty,
                    "limit_price": o.limit_price,
                    "status": o.status.value,
                }
                for o in orders
                if o.id not in stop_ids
            ],
        }
    )
    return out


def _latest_target(conn: Connection, execution: date | None) -> PortfolioTarget | None:
    query = select(PortfolioTarget).order_by(
        PortfolioTarget.execution_session.desc(), PortfolioTarget.id.desc()
    )
    if execution is not None:
        query = query.where(PortfolioTarget.execution_session == execution)
    return orm.Session(conn).scalars(query.limit(1)).first()


def trend(conn: Connection) -> dict[str, Any]:
    """The latest trend reading (R's `allocate`, or the decision on E) and its facts."""
    row = _latest_target(conn, None)
    if row is None:
        return {"latest": None, "note": "no trend read yet: the first R's allocate writes it"}
    facts = row.facts or {}
    account = dict(facts.get("account") or {})
    for heavy in ("lots", "prices"):
        account.pop(heavy, None)
    return record(
        {
            "latest": {
                "kind": row.kind,
                "status": row.status,
                "ranking_session": row.ranking_session,
                "execution_session": row.execution_session,
                "trend": row.trend,
                "capped": row.capped,
                "halted": row.halted,
                "rule_level": row.rule_level,
                "level": row.level,
                "core_level": row.core_level,
                "satellite": row.satellite,
                "twin_targets": row.twin_targets,
            },
            "facts": {
                "trend": facts.get("trend"),
                "account": account,
                "regime": facts.get("regime"),
                "macro": facts.get("macro"),
                "event": facts.get("event"),
                "notes": facts.get("notes"),
            },
        }
    )


def tax(conn: Connection, risk: RiskConfig, year: int) -> dict[str, Any]:
    lots = open_lots(conn)
    income = conn.execute(
        select(TaxLot.symbol, func.sum(TaxLot.cost))
        .where(TaxLot.kind == "INCOME", extract("year", TaxLot.opened) == year)
        .group_by(TaxLot.symbol)
    ).all()
    used = conn.scalar(
        select(func.coalesce(func.sum(PortfolioTarget.satellite_tax), 0.0)).where(
            PortfolioTarget.status == "DECIDED",
            extract("year", PortfolioTarget.execution_session) == year,
        )
    )
    last = _last_close(conn)
    fraction = risk.portfolio.satellite_tax_budget
    equity = None if last is None else last["equity"]
    budget = None if equity is None else fraction * equity
    return record(
        {
            "year": year,
            "open_lots": {
                symbol: {
                    "units": sum(lot.units for lot in symbol_lots),
                    "cost": sum(lot.cost for lot in symbol_lots),
                    "lots": [
                        {"opened": lot.opened, "units": lot.units, "cost": lot.cost}
                        for lot in symbol_lots
                    ],
                }
                for symbol, symbol_lots in lots.items()
            },
            "realized_gain_by_year": realized_by_year(conn),
            "income_this_year": {symbol: float(total or 0.0) for symbol, total in income},
            "tax_owed_if_sold_at_last_close": None if last is None else last["tax_owed"],
            "satellite_tax_budget": {
                "fraction_of_equity": fraction,
                "equity_at_last_close": equity,
                "budget": budget,
                "used": float(used or 0.0),
                "left": None if budget is None else budget - float(used or 0.0),
                "applies": "not while DOWN, capped or halted (#4)",
            },
        }
    )


def decision(conn: Connection, execution: date | None) -> dict[str, Any]:
    """One decision whole: the facts, the answer, the guardrails' findings, the runs, the cost."""
    row = _latest_target(conn, execution)
    if row is None:
        return {"decision": None, "note": "no decision for that session"}
    session = orm.Session(conn)
    llm = None if row.llm_decision_id is None else session.get(LlmDecision, row.llm_decision_id)
    journal = session.get(PortfolioJournal, row.execution_session)
    return record(
        {
            "decision": {
                "kind": row.kind,
                "status": row.status,
                "ranking_session": row.ranking_session,
                "execution_session": row.execution_session,
                "trend": row.trend,
                "capped": row.capped,
                "halted": row.halted,
                "rule_level": row.rule_level,
                "level": row.level,
                "core_level": row.core_level,
                "satellite": row.satellite,
                "targets": row.targets,
                "twin_targets": row.twin_targets,
                "held_satellite": row.held_satellite,
                "fell_back": row.fell_back,
                "satellite_tax": row.satellite_tax,
                "findings": row.findings,
                "executions": row.executions,
                "executed_at": row.executed_at,
                "decided_at": row.decided_at,
                "risk_hash": row.risk_hash,
            },
            "facts": row.facts,
            "llm": None
            if llm is None
            else {
                "model": llm.model,
                "requested_model": llm.requested_model,
                "effort": llm.effort,
                "prompt_id": llm.prompt_id,
                "answer": llm.answer,
                "failure": llm.failure,
                "detail": llm.detail,
                "refusal_category": llm.refusal_category,
                "turns": len(llm.turns),
                "cost_usd": llm.cost_usd,
                "fallback": llm.fallback,
            },
            "journal": None
            if journal is None
            else {
                "summary": journal.summary,
                "clips": journal.clips,
                "trades": journal.trades,
                "results": journal.results,
            },
        }
    )
