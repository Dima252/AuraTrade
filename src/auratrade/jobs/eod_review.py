"""`eod_review` (15:40, 12:40 on half days; README §3, §5.9): the code half of the review.

    uv run python -m auratrade.jobs.eod_review --session 2026-10-22 --dry-run

Under the order lock: reconcile, enforce the stop invariant, cancel every entry parent still
unfilled (no entry rests overnight, #113), retry owed sales, then the skeleton's one soft rule:
a decision price below the trailing average (`skeleton.trail`, the 21 EMA) exits the whole
position through the sell sequence. The average is taken over the stored closes with the
decision price standing in for today's (`plans.levels.trail_levels`). No decision price: the
exit waits and the hard stop keeps resting (#120). A sale that doesn't fill stays owed, and the
next morning's 09:31 check run retries it.

It is scheduled at both 12:40 and 15:40 every weekday, since a scheduler can't follow the
calendar, and runs only within `jobs.eod_review_window_minutes` of the session's close: the
12:40 slot on half days, the 15:40 slot otherwise. The other slot records a skip.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import Connection, select

from auratrade.calendar.sessions import load_calendar
from auratrade.db.models import DailyBar
from auratrade.execution.orders import SaleStatus
from auratrade.execution.runtime import (
    DepsFactory,
    ExecutionDeps,
    Section,
    enforce_invariant,
    enforce_kill_switch,
    mark_exit_pending,
    order_section,
    production_deps,
    sell_owed,
)
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli
from auratrade.plans.levels import trail_levels

# Closes loaded for the trailing averages: an EMA needs several times its window to settle.
TRAIL_HISTORY_SESSIONS = 120


def recent_closes(
    conn: Connection, ticker_ids: list[int], before: date, sessions: int
) -> dict[int, list[float]]:
    rows = conn.execute(
        select(DailyBar.ticker_id, DailyBar.session_date, DailyBar.close)
        .where(DailyBar.ticker_id.in_(ticker_ids), DailyBar.session_date < before)
        .order_by(DailyBar.ticker_id, DailyBar.session_date)
    ).all()
    out: dict[int, list[float]] = defaultdict(list)
    for ticker_id, _, close in rows:
        out[ticker_id].append(float(close))
    return {k: v[-sessions:] for k, v in out.items()}


def trail_exits(section: Section) -> list[dict[str, Any]]:
    ctx, deps, desk = section.ctx, section.deps, section.desk
    held = {p.symbol: p.qty for p in deps.broker.positions() if p.qty > 0}
    positions = {
        s: p
        for s, p in section.rec.positions.items()
        if s in held and p.exit_pending is None and p.ticker_id is not None
    }
    if not positions:
        return []
    with deps.engine.connect() as conn:
        closes = recent_closes(
            conn,
            [p.ticker_id for p in positions.values() if p.ticker_id is not None],
            ctx.session_date,
            TRAIL_HISTORY_SESSIONS,
        )
    prices = deps.prices.prices(positions)
    trail = ctx.risk.skeleton.trail
    out: list[dict[str, Any]] = []
    for symbol, position in positions.items():
        price = prices.get(symbol)
        history = closes.get(position.ticker_id or 0, [])
        if price is None:
            out.append({"symbol": symbol, "decision": "wait: no decision price"})
            continue
        level = trail_levels([*history, price], ctx.risk.plans).get(trail)
        if level is None or price >= level:
            out.append({"symbol": symbol, "decision": "hold", "price": price, "trail": level})
            continue
        if section.dry_run:
            out.append({"symbol": symbol, "decision": "would exit", "price": price, "trail": level})
            continue
        result = desk.sell(symbol, held[symbol], "TRAIL", section.rec.levels[symbol])
        if result.status is not SaleStatus.FILLED:
            mark_exit_pending(section, symbol, "TRAIL")
        out.append(
            {
                "symbol": symbol,
                "decision": "exit",
                "price": price,
                "trail": level,
                "sold": result.sold,
                "status": result.status.value,
            }
        )
    return out


def minutes_to_close(deps: ExecutionDeps, day: date) -> float | None:
    with deps.engine.connect() as conn:
        session = load_calendar(conn).session_for(day)
    if session is None:
        return None
    return (session.close_at - deps.clock()).total_seconds() / 60


def make_job(
    deps_factory: DepsFactory = production_deps, *, near_close_only: bool = False
) -> JobSpec:
    """`near_close_only` is the rule above (the module's `JOB`, also run by hand); tests leave
    it off to run the review at any time."""

    def run(ctx: JobContext) -> JobOutput:
        deps = deps_factory(ctx)
        if near_close_only:
            window = ctx.risk.jobs.eod_review_window_minutes
            minutes = minutes_to_close(deps, ctx.session_date)
            if minutes is None or not 0 < minutes <= window:
                return {"skipped": f"not within {window} min of the close"}
        with order_section(ctx, deps) as section:
            invariant = enforce_invariant(section)
            enforce_kill_switch(section)
            cancelled = 0
            if not section.dry_run:
                cancelled = section.desk.cancel_entries(why="EOD: no entry rests overnight")
            sales = sell_owed(section)
            exits = trail_exits(section)
            return {
                "dry_run": section.dry_run,
                **section.rec.details(),
                **invariant.details(),
                "entries_cancelled": cancelled,
                "sales": sales,
                "exits": exits,
                "actions": section.desk.actions,
                "alerts": section.alerts,
            }

    return JobSpec(name="eod_review", policy=GuardPolicy.SESSION_TODAY, run=run)


JOB = make_job(near_close_only=True)
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
