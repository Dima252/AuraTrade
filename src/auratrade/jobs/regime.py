"""`regime` (evening chain, after `simulate`): write the computed regime for the session.

    uv run python -m auratrade.jobs.regime --session 2026-10-09 --dry-run
    uv run python -m auratrade.jobs.regime --from 2025-10-01 --session 2026-10-08

The first form is the evening step: it continues the state machine from the previous session's
`market_regime` row and fails closed without one. The second seeds that history: a replay that
starts `regime.replay_warmup_sessions` before `--from` in `CORRECTION` and writes the rows from
`--from` through `--session`. The replay doesn't go through the job runner (no heartbeat).

The LLM view and the effective regime's downgrade come in Phase 4 (README §5.2); until then the
effective regime is the computed one.
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
from auratrade.regime.compute import replay, run_session

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
            result = run_session(conn, ctx.risk, ctx.session_date)
        if "macro_stale" in result.crisis.reasons:
            limit = ctx.risk.data.macro_stale_sessions
            alerter_factory(ctx.settings).alert(
                f"[AuraTrade {ctx.settings.environment}] regime {ctx.session_date}: VIX/VIX3M "
                f"more than {limit} sessions stale in CBOE and FRED. Crisis override on: no new "
                "entries next session."
            )
        details = {
            "computed_regime": result.regime.value,
            "previous_regime": None if result.previous is None else result.previous.value,
            "score": result.score,
            "crisis": list(result.crisis.reasons),
            "follow_through_day": result.follow_through is not None,
            "sessions_in_state": result.state.sessions_in_state,
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
