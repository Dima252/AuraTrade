"""`portfolio_manager` (08:45 on E, and on events; README §3, §5.4): the LLM's decision,
through the guardrails, into `portfolio_targets`.

    uv run python -m auratrade.jobs.portfolio_manager --session 2026-11-02 --dry-run
    uv run python -m auratrade.jobs.portfolio_manager --event HALT --detail "..." \\
        --session 2026-11-12 --dry-run

On E it decides from the facts `allocate` wrote on R; on other sessions it does nothing. An
event (until task 5.x raises them, by hand) decides at once and may only reduce risk. **A
paid Claude call** (about $0.15-0.30, README §9). A failure holds (#164) and alerts; so do
the guardrails' clips and a decision above `llm.cost_alert_usd`. Orders aren't sent in any
mode until task 5.1; the targets are written.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import Engine

from auratrade.calendar.sessions import load_calendar
from auratrade.config import Settings, TradingMode
from auratrade.db.engine import engine_from_settings
from auratrade.jobs.allocate import alpaca
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli
from auratrade.journal.decisions import Morning, NewsSource, decide_event, decide_rebalance
from auratrade.journal.portfolio import FundBars
from auratrade.llm.client import ClaudeClient
from auratrade.llm.portfolio_facts import Event, EventKind
from auratrade.notify.alerts import Alerter


def _client(settings: Settings, ctx: JobContext) -> ClaudeClient:
    return ClaudeClient.from_settings(
        settings, ctx.risk.llm, model=settings.pm_model, effort=settings.pm_effort
    )


def _news(settings: Settings) -> NewsSource:
    from auratrade.data.alpaca_data import alpaca_http
    from auratrade.data.news import AlpacaNewsClient

    if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    return AlpacaNewsClient(
        alpaca_http(
            settings.alpaca_api_key.get_secret_value(),
            settings.alpaca_secret_key.get_secret_value(),
        )
    )


def _alerter(settings: Settings) -> Alerter:
    from auratrade.jobs.wiring import make_alerter

    return make_alerter(settings)


def make_job(
    *,
    event: Event | None = None,
    engine_factory: Callable[[Settings], Engine] = engine_from_settings,
    client_factory: Callable[[Settings, JobContext], ClaudeClient] = _client,
    funds_factory: Callable[[Settings], FundBars] = alpaca,
    news_factory: Callable[[Settings], NewsSource] = _news,
    alerter_factory: Callable[[Settings], Alerter] = _alerter,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        engine = engine_factory(ctx.settings)
        with engine.connect() as conn:
            calendar = load_calendar(conn)
        morning = Morning(
            client_factory(ctx.settings, ctx),
            funds_factory(ctx.settings),
            news_factory(ctx.settings),
            now(),
            live=ctx.settings.trading_mode is TradingMode.PAPER,
        )
        with engine.begin() as conn:
            if event is None:
                out = decide_rebalance(conn, calendar, ctx.risk, morning, ctx.session_date)
            else:
                out = decide_event(conn, calendar, ctx.risk, morning, ctx.session_date, event)
        ctx.log.info("portfolio_decided", **out)
        alert = _alert_text(out, ctx.risk.llm.cost_alert_usd)
        if alert:
            prefix = f"[AuraTrade {ctx.settings.environment}] portfolio_manager {ctx.session_date}"
            alerter_factory(ctx.settings).alert(f"{prefix}: {alert}")
        return out

    return JobSpec(name="portfolio_manager", policy=GuardPolicy.SESSION_TODAY, run=run)


def _alert_text(out: JobOutput, cost_alert: float) -> str:
    if not out or "skipped" in out:
        return ""
    parts = []
    if out.get("fell_back"):
        parts.append(f"the LLM failed ({out['fell_back']}): held last month's satellite")
    if out.get("clips"):
        parts.append(f"guardrail clips {', '.join(out['clips'])}")
    if out.get("cost_usd", 0.0) > cost_alert:
        parts.append(f"cost ${out['cost_usd']:.2f} above ${cost_alert:.2f}")
    return "; ".join(parts)


JOB = make_job()
handler = lambda_handler(JOB)


def main(argv: Sequence[str] | None = None) -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--event", type=EventKind)
    pre.add_argument("--detail", default="")
    known, rest = pre.parse_known_args(argv)
    if known.event is None:
        return run_cli(JOB, rest)
    return run_cli(make_job(event=Event(known.event, known.detail)), rest)


if __name__ == "__main__":
    sys.exit(main())
