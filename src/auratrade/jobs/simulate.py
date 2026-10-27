"""`simulate` (evening chain, after `ingest`, before `regime`; README §3, §5.12).

    uv run python -m auratrade.jobs.simulate --session 2026-10-23 --dry-run

On session T's bars:
0. **The twin and the book** (README §5.5; task 4.3, #166): both through `sim/portfolio.py` from
   the trial's first R to T, written whole to `twin_book` (nothing before the trial starts).
1. **Breakout tracker:** T's triggers of T-1's full scan (every setup, ungated) and every
   5-session result that is now complete, so `regime`'s leadership-health pillar sees results
   through T.
2. **Baseline book:** decides T's entries from T-1's capped list (rank order, through the
   guardrails), then simulates them and every open baseline trade through T from minute bars
   fetched for its symbols only (cached in the object store).
3. **Verdict scorecard:** plays every Analyst verdict forward under the default plan. Verdicts
   arrive in Phase 4 (`setups`); until then there are none to score.

Nothing is written when T's data isn't COMPLETE (fail closed). The job places no orders, so
`--dry-run` changes nothing here.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Sequence
from datetime import date

from sqlalchemy import Connection, Engine

from auratrade.calendar.sessions import MarketCalendar, load_calendar
from auratrade.config import Settings
from auratrade.db.engine import engine_from_settings
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli
from auratrade.journal.evening import TickerReview, run_baseline, score_verdicts, track_breakouts
from auratrade.journal.portfolio import FundBars, simulate_books
from auratrade.sim.minutes import MinuteCache, MinuteSource
from auratrade.storage import ObjectStore, make_store

type Reviews = Callable[[Connection, MarketCalendar, date], Sequence[TickerReview]]


def _no_reviews(_conn: Connection, _calendar: MarketCalendar, _day: date) -> list[TickerReview]:
    # The Analyst's verdicts (`setups`) arrive in Phase 4 (task 4.4).
    return []


def _alpaca(settings: Settings) -> MinuteSource:
    from auratrade.data.alpaca_data import AlpacaDataClient, alpaca_http

    if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    return AlpacaDataClient(
        alpaca_http(
            settings.alpaca_api_key.get_secret_value(),
            settings.alpaca_secret_key.get_secret_value(),
        )
    )


def _funds(settings: Settings) -> FundBars:
    from auratrade.jobs.allocate import alpaca

    return alpaca(settings)


def make_job(
    *,
    engine_factory: Callable[[Settings], Engine] = engine_from_settings,
    store_factory: Callable[[Settings], ObjectStore] = make_store,
    source_factory: Callable[[Settings], MinuteSource] = _alpaca,
    reviews: Reviews = _no_reviews,
    funds_factory: Callable[[Settings], FundBars] | None = None,
) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        engine = engine_factory(ctx.settings)
        started = time.monotonic()
        with engine.connect() as conn:
            calendar = load_calendar(conn)
        session = calendar.session_for(ctx.session_date)
        if session is None or session.data_status != "COMPLETE":
            status = None if session is None else session.data_status
            ctx.log.warning("simulate_skipped", data_status=status)
            return {"skipped": f"data status {status}"}
        seconds: dict[str, float] = {}
        funds = funds_factory(ctx.settings) if funds_factory else _funds(ctx.settings)
        with engine.begin() as conn:
            books = simulate_books(conn, calendar, ctx.risk, funds, ctx.session_date)
        seconds["books"] = round(time.monotonic() - started, 1)
        cache = MinuteCache(store_factory(ctx.settings), source_factory(ctx.settings))
        with engine.begin() as conn:
            tracker = track_breakouts(conn, calendar, ctx.session_date, ctx.risk)
        seconds["tracker"] = round(time.monotonic() - started - sum(seconds.values()), 1)
        with engine.begin() as conn:
            book = run_baseline(conn, calendar, ctx.session_date, ctx.risk, cache)
        seconds["baseline"] = round(time.monotonic() - started - sum(seconds.values()), 1)
        with engine.begin() as conn:
            scored = score_verdicts(
                conn,
                calendar,
                ctx.session_date,
                ctx.risk,
                cache,
                reviews(conn, calendar, ctx.session_date),
            )
        seconds["scorecard"] = round(time.monotonic() - started - sum(seconds.values()), 1)
        return {
            "books": books,
            "tracker": tracker,
            "baseline": book,
            "scorecard": scored,
            "seconds": seconds,
        }

    return JobSpec(name="simulate", policy=GuardPolicy.EVENING, run=run)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
