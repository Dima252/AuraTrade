"""`regime` (evening chain, after `simulate`): write the computed regime for the session.

    uv run python -m auratrade.jobs.regime --session 2026-10-09 --dry-run
    uv run python -m auratrade.jobs.regime --from 2025-10-01 --session 2026-10-08

The first form is the evening step: it continues the state machine from the last stored
`market_regime` row, classifying any sessions a missed evening left out on the way (reported as
`filled`), and fails closed when the table is empty. The second seeds that history: a replay that
starts `regime.replay_warmup_sessions` before `--from` in `CORRECTION` and writes the rows from
`--from` through `--session`. The replay doesn't go through the job runner (no heartbeat).

A fact for the Portfolio Manager; no rule reads it (README §5.2). Since task 5.2 it runs on three
pillars (index trend, volatility, distribution): breadth and leadership health read the stock
universe and the breakout tracker, both retired, and score 0. The effective regime is the
computed one.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import date

import structlog
from sqlalchemy import Engine

from auratrade.config import Settings, load_risk_config
from auratrade.db.engine import engine_from_settings
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli
from auratrade.notify.alerts import Alerter
from auratrade.regime.compute import catch_up, replay

log = structlog.get_logger(__name__)


def _alerter(settings: Settings) -> Alerter:
    from auratrade.jobs.wiring import make_alerter

    return make_alerter(settings)


def make_job(
    *,
    engine_factory: Callable[[Settings], Engine] = engine_from_settings,
    alerter_factory: Callable[[Settings], Alerter] = _alerter,
) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        engine = engine_factory(ctx.settings)
        with engine.begin() as conn:
            results = catch_up(conn, ctx.risk, ctx.session_date)
        result = results[-1]
        if "macro_stale" in result.crisis.reasons:
            limit = ctx.risk.data.macro_stale_sessions
            alerter_factory(ctx.settings).alert(
                f"[AuraTrade {ctx.settings.environment}] regime {ctx.session_date}: VIX/VIX3M "
                f"more than {limit} sessions stale in CBOE and FRED: the crisis override is on in "
                "the regime the Portfolio Manager sees."
            )
        details = {
            "computed_regime": result.regime.value,
            "previous_regime": None if result.previous is None else result.previous.value,
            "score": result.score,
            "crisis": list(result.crisis.reasons),
            "follow_through_day": result.follow_through is not None,
            "sessions_in_state": result.state.sessions_in_state,
            "filled": [r.session.isoformat() for r in results[:-1]],
        }
        ctx.log.info("regime_computed", **details)
        return details

    return JobSpec(name="regime", policy=GuardPolicy.EVENING, run=run)


JOB = make_job()
handler = lambda_handler(JOB)


def replay_main(first_label: date, argv: Sequence[str]) -> int:
    from auratrade.jobs.wiring import load_runtime_settings
    from auratrade.log import configure_logging

    parser = argparse.ArgumentParser(prog="python -m auratrade.jobs.regime --from DATE")
    parser.add_argument("--session", type=date.fromisoformat, required=True, help="last label")
    args = parser.parse_args(argv)
    settings = load_runtime_settings()
    configure_logging(settings.secret_values())
    risk = load_risk_config(settings.risk_config_path)
    engine = engine_from_settings(settings)
    try:
        with engine.begin() as conn:
            results = replay(conn, risk, first_label, args.session)
    finally:
        engine.dispose()
    last = results[-1] if results else None
    log.info(
        "regime_replayed",
        first=first_label.isoformat(),
        last=args.session.isoformat(),
        sessions=len(results),
        regimes=dict(Counter(r.regime.value for r in results)),
        final_regime=None if last is None else last.regime.value,
        final_score=None if last is None else last.score,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--from", dest="replay_from", type=date.fromisoformat)
    known, rest = pre.parse_known_args(argv)
    if known.replay_from is None:
        return run_cli(JOB, rest)
    return replay_main(known.replay_from, rest)


if __name__ == "__main__":
    sys.exit(main())
