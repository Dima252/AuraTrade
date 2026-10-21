"""`execute_entries` (09:36; README §3, §5.7.4, §7.1): place today's entry signals.

    uv run python -m auratrade.jobs.execute_entries --session 2026-10-22 --dry-run

Under the order lock: reconcile, enforce the stop invariant, retry owed sales, then place the
entries unless something blocks them (fail closed: the kill switch, a halt, a calendar
disagreement, incomplete data, an open mismatch, a repaired invariant, an early close).

The skeleton places at most `skeleton.picks` entries, best rank first, skipping a symbol already
held, one that sizes to 0 shares, and one whose price is above its limit. Each entry is an OTO
with a GTC parent and a GTC stop leg (#113). Its client_order_id is written to the signal before
the submission and is deterministic, so a retried run finds the order instead of buying twice.
"""

from __future__ import annotations

import sys
from typing import Any

from auratrade.execution import ids, store
from auratrade.execution.broker import (
    BrokerError,
    BrokerOrder,
    BrokerUnavailableError,
    OrderStatus,
    OtoBuy,
    Side,
)
from auratrade.execution.runtime import (
    DepsFactory,
    Section,
    enforce_invariant,
    enforce_kill_switch,
    entry_blocks,
    order_section,
    production_deps,
    sell_owed,
)
from auratrade.execution.skeleton import entry_decision, size
from auratrade.execution.store import SignalRecord
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli


def _skip(section: Section, signal: SignalRecord, reason: str) -> dict[str, Any]:
    with section.deps.engine.begin() as conn:
        store.update_signal(
            conn, signal.id, section.deps.clock(), status="SKIPPED", status_reason=reason
        )
    return {"symbol": signal.symbol, "skipped": reason}


def _submitted_before(
    section: Section, signal: SignalRecord, open_entries: dict[str, BrokerOrder]
) -> BrokerOrder | None:
    if signal.client_order_id is None:
        return None
    found = open_entries.get(signal.client_order_id)
    if found is None:
        found = section.deps.broker.order_by_client_id(signal.client_order_id)
    return found if found is not None and found.status is not OrderStatus.REJECTED else None


def place_entries(section: Section, signals: list[SignalRecord]) -> list[dict[str, Any]]:
    ctx, deps, desk = section.ctx, section.deps, section.desk
    cfg = ctx.risk.skeleton
    account = deps.broker.account()
    open_orders = deps.broker.open_orders()
    entries = [o for o in open_orders if o.side is Side.BUY]
    held = set(section.rec.positions) | set(section.rec.broker_positions)
    pending = {o.symbol for o in entries}
    with deps.engine.connect() as conn:
        mismatched = {symbol for _, symbol in store.open_mismatches(conn) if symbol}
    # No margin (README §5.7.9): positions plus resting entries at their limit stay within
    # equity, and the slots count resting entries too.
    room = (
        account.equity
        - account.long_market_value
        - sum(o.remaining_qty * (o.limit_price or 0.0) for o in entries)
    )
    slots = cfg.max_positions - len(held | pending)
    prices = deps.prices.prices([s.symbol for s in signals])
    placed = 0
    out: list[dict[str, Any]] = []
    by_client_id = {o.client_order_id: o for o in entries}
    for signal in signals:
        mine = _submitted_before(section, signal, by_client_id)
        if mine is not None:
            # A run that died after submitting: adopt its order instead of skipping the
            # symbol as "already entering".
            with deps.engine.begin() as conn:
                store.update_signal(conn, signal.id, deps.clock(), status="SUBMITTED")
            placed += 1
            slots -= 1
            out.append({"symbol": signal.symbol, "adopted": mine.id})
            continue
        if placed >= cfg.picks or slots <= 0:
            out.append(_skip(section, signal, "picks or positions full"))
            continue
        if signal.symbol in held or signal.symbol in pending:
            out.append(_skip(section, signal, "already held or entering"))
            continue
        if signal.symbol in mismatched:
            out.append(_skip(section, signal, "open mismatch in the symbol"))
            continue
        shares = size(
            equity=account.equity,
            risk_fraction=signal.risk_fraction,
            limit=signal.limit_price,
            stop=signal.stop_price,
            max_position_fraction=ctx.risk.guardrails.max_position_size,
            room=room,
        )
        if shares == 0:
            out.append(_skip(section, signal, "sized to 0 shares"))
            continue
        decision = entry_decision(
            prices.get(signal.symbol), trigger=signal.trigger_price, limit=signal.limit_price
        )
        if decision.skip is not None:
            out.append(_skip(section, signal, decision.skip))
            continue
        client_order_id = signal.client_order_id or ids.entry_id(
            ctx.session_date, signal.symbol, tranche=signal.tranche
        )
        with deps.engine.begin() as conn:
            store.update_signal(
                conn, signal.id, deps.clock(), client_order_id=client_order_id, shares=shares
            )
        request = OtoBuy(
            signal.symbol,
            shares,
            limit_price=signal.limit_price,
            stop_loss=signal.stop_price,
            client_order_id=client_order_id,
            trigger=decision.trigger,
        )
        try:
            order = desk.submit_once(request)
        except BrokerUnavailableError as exc:
            # It may have reached the broker: the signal keeps its id, and the next
            # reconciliation finds the order if it exists.
            section.alert(f"entry {signal.symbol} unanswered: {exc}")
            out.append({"symbol": signal.symbol, "unanswered": str(exc)})
            continue
        except BrokerError as exc:
            section.alert(f"entry {signal.symbol} rejected: {exc}")
            out.append(_skip(section, signal, f"rejected: {exc}"))
            continue
        with deps.engine.begin() as conn:
            store.update_signal(conn, signal.id, deps.clock(), status="SUBMITTED")
        placed += 1
        slots -= 1
        room -= shares * signal.limit_price
        out.append(
            {
                "symbol": signal.symbol,
                "shares": shares,
                "order_id": order.id,
                "trigger": decision.trigger,
                "limit": signal.limit_price,
                "stop": signal.stop_price,
            }
        )
    return out


def make_job(deps_factory: DepsFactory = production_deps) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        deps = deps_factory(ctx)
        with order_section(ctx, deps) as section:
            invariant = enforce_invariant(section)
            enforce_kill_switch(section)
            sales = sell_owed(section)
            reasons = entry_blocks(section, invariant)
            if not section.desk.market_open:
                reasons.append("market closed")
            with deps.engine.connect() as conn:
                signals = store.signals_for(conn, ctx.session_date, ["PENDING"])
            if section.dry_run:
                reasons.append("dry_run")
            entries = place_entries(section, signals) if not reasons else []
            if reasons and signals:
                ctx.log.warning("entries_blocked", reasons=reasons, signals=len(signals))
            return {
                "dry_run": section.dry_run,
                **section.rec.details(),
                **invariant.details(),
                "sales": sales,
                "signals": len(signals),
                "blocked": reasons,
                "entries": entries,
                "actions": section.desk.actions,
                "alerts": section.alerts,
            }

    return JobSpec(name="execute_entries", policy=GuardPolicy.SESSION_TODAY, run=run)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
