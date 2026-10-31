"""`post_market` (16:20, the head of the evening chain; README §3, §5.7, §10).

    uv run python -m auratrade.jobs.post_market --session 2026-10-22 --dry-run

1. Under the order lock: reconcile, enforce the stop invariant (the market is closed: a missing
   stop is placed to rest for the morning, nothing is sold), and cancel any buy still resting:
   no buy order rests overnight (README §4).
2. **The tax lots** (README §5.7): `tax_lots` rebuilt whole from the broker's
   activities (`tax.live`); what doesn't add up (a sale beyond the lots, lots that differ from
   the positions, an entry the lots don't apply) goes in the daily report. If the activities
   can't be read, the lots stay as they were and the snapshot's tax is unknown.
3. The account snapshot (the funds' holdings and the tax a sale of everything would owe) and
   **the drawdown check** (README §7.3, `execution.halt`): equity at most 75% of its peak turns
   the halt on, and `execute_close` sells the leveraged fund into the market fund at 15:40 on
   the next session.
4. The Telegram daily report, and the nightly backup of the kept-forever tables to storage.

On success it starts `ingest` (the chain). Steps 2-4 run in dry_run too: they change no order,
and the drawdown check is reported but not written (in dry_run the account is the simulated
book, whose halt is simulated, #31).
"""

from __future__ import annotations

import sys

from auratrade.execution import halt
from auratrade.execution.broker import BrokerError
from auratrade.execution.runtime import (
    DepsFactory,
    enforce_invariant,
    enforce_kill_switch,
    order_section,
    production_deps,
)
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli
from auratrade.journal.backup import export_tables
from auratrade.journal.portfolio import fund_symbols
from auratrade.journal.report import daily_report, holdings, write_snapshot
from auratrade.journal.tax_lots import write_lots
from auratrade.tax.live import LiveTax, disagreements, replay, values_for_tax


def make_job(deps_factory: DepsFactory = production_deps) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        deps = deps_factory(ctx)
        with order_section(ctx, deps) as section:
            invariant = enforce_invariant(section)
            enforce_kill_switch(section)
            cancelled = 0
            if not section.dry_run:
                cancelled = section.desk.cancel_entries(why="post_market: no buy rests overnight")
            rec, alerts, actions = section.rec, section.alerts, section.desk.actions
        account = deps.broker.account()
        positions = deps.broker.positions()
        funds = fund_symbols(ctx.risk)
        live: LiveTax | None = None
        tax_notes: list[str] = []
        tax_owed = None
        try:
            live = replay(
                deps.broker.activities(), funds, ctx.risk.portfolio.tax_rate, ctx.session_date
            )
            held = holdings(positions, funds)
            tax_notes = live.warnings + disagreements(
                live.book, {s: h["units"] for s, h in held.items()}, funds
            )
            values = values_for_tax(live.book, {s: h["value"] for s, h in held.items()})
            tax_owed = live.book.tax_if_sold(ctx.session_date.year, values)
        except (BrokerError, ValueError) as exc:
            live = None
            tax_notes = [f"the lots weren't rebuilt: {type(exc).__name__}: {exc}"]
            ctx.log.error("tax_lots_failed", error=f"{type(exc).__name__}: {exc}")
        with deps.engine.begin() as conn:
            lots = None if live is None else write_lots(conn, live)
            snapshot = write_snapshot(
                conn, ctx.session_date, account, positions, funds=funds, tax_owed=tax_owed
            )
            drawdown = halt.check_at_close(
                conn,
                ctx.session_date,
                snapshot.equity,
                ctx.risk.portfolio,
                write=not ctx.dry_run,
            )
        if drawdown["turned_on"] and not ctx.dry_run:
            section.alert(
                f"DRAWDOWN HALT ON: equity {snapshot.equity:,.2f} is {drawdown['drawdown']:.1%} "
                f"below its peak {drawdown['peak']:,.2f}; the leveraged fund is sold into "
                f"{ctx.risk.portfolio.market} at 15:40 next session. Explain it with "
                "portfolio_manager --event HALT."
            )
        report = daily_report(
            snapshot,
            positions,
            rec.levels,
            invariant_ok=not invariant.still_unprotected and not rec.unknown,
            halted=bool(drawdown["on"]),
            alerts=alerts,
            tax_notes=tax_notes,
            mode=ctx.trading_mode.value,
        )
        try:
            deps.alerter.alert(f"[AuraTrade {ctx.settings.environment}] {report}")
        except Exception as exc:
            ctx.log.error("daily_report_not_sent", error=f"{type(exc).__name__}: {exc}")
        with deps.engine.connect() as conn:
            backup = export_tables(conn, ctx.storage, ctx.session_date)
        return {
            "dry_run": ctx.dry_run,
            **rec.details(),
            **invariant.details(),
            "buys_cancelled": cancelled,
            "equity": snapshot.equity,
            "drawdown": round(snapshot.drawdown, 4),
            "tax_owed": None if tax_owed is None else round(tax_owed, 2),
            "tax_lots": lots,
            "tax_notes": tax_notes,
            "halt": drawdown,
            "backup_rows": backup,
            "actions": actions,
            "alerts": alerts,
        }

    return JobSpec(name="post_market", policy=GuardPolicy.EVENING, run=run)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
