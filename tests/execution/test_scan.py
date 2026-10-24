"""The skeleton's evening scan: ranking on synthetic histories, and the `scan` job on the test
database (signals for the next session, fail closed on incomplete data)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from auratrade.db.models import Mismatch, Signal, WatchlistRow
from auratrade.execution.signals import History, expire_signals, lookback_sessions, scan
from auratrade.indicators.bars import Bar
from auratrade.jobs import analyst_submit
from auratrade.jobs import scan as scan_job
from auratrade.regime.inputs import MissingDataError
from tests.execution.conftest import World

RISING = {"NVDA": 0.004, "AMD": 0.003, "MSFT": 0.002, "AAPL": 0.0, "META": -0.002}


def closes(step: float, n: int = 300) -> list[float]:
    return [50.0 * (1 + step) ** i for i in range(n)]


def history(ticker_id: int, symbol: str, step: float) -> History:
    cs = closes(step)
    return History(
        ticker_id=ticker_id,
        symbol=symbol,
        dates=[date(2025, 1, 1) + timedelta(days=i) for i in range(len(cs))],
        bars=[Bar(c, c * 1.01, c * 0.99, c, 1e6) for c in cs],
        raw_closes=cs,
    )


def test_scan_ranks_the_trend_template_passers_by_rs() -> None:
    from auratrade.config import load_risk_config

    risk = load_risk_config()
    histories = [history(i, s, step) for i, (s, step) in enumerate(RISING.items())]
    candidates = scan(histories, risk)
    # Flat and falling stocks fail rules 1-5; the RS percentile ranks the rest.
    assert [c.symbol for c in candidates] == ["NVDA", "AMD"]
    top = candidates[0]
    assert top.rs_rating == 100.0
    assert top.levels.stop < top.levels.trigger <= top.levels.limit


def test_scan_leaves_out_non_members() -> None:
    from auratrade.config import load_risk_config

    risk = load_risk_config()
    short = history(1, "NVDA", 0.004)
    short = History(1, "NVDA", short.dates[-100:], short.bars[-100:], short.raw_closes[-100:])
    cheap = history(2, "AMD", 0.003)
    cheap = History(2, "AMD", cheap.dates, cheap.bars, [5.0] * len(cheap.raw_closes))
    assert scan([short, cheap], risk) == []
    assert lookback_sessions(risk) > 252


def run_scan(world: World, session: date) -> dict[str, object]:
    spec = scan_job.make_job(engine_factory=lambda _s: world.engine, clock=world.clock)
    return dict(spec.run(world.context("scan", session=session)) or {})


def test_the_scan_job_writes_tomorrows_signals(world: World) -> None:
    for symbol, step in RISING.items():
        world.bars(symbol, closes(step))
    world.regime(date(2026, 10, 21))
    with world.engine.begin() as conn:
        conn.execute(insert(Mismatch).values(kind="RECONCILIATION", symbol="AMD"))
    out = run_scan(world, date(2026, 10, 21))
    assert out["universe_loaded"] == 5
    assert out["signals"] == 1  # AMD has an open mismatch
    assert out["trade_session"] == "2026-10-22"
    with world.engine.connect() as conn:
        row = conn.execute(select(Signal.symbol, Signal.session_date, Signal.rank)).one()
    assert tuple(row) == ("NVDA", date(2026, 10, 22), 1)
    assert run_scan(world, date(2026, 10, 21))["signals"] == 0  # a re-run adds nothing


def test_the_full_scan_writes_the_watchlist(world: World) -> None:
    for symbol, step in RISING.items():
        world.bars(symbol, closes(step))
    world.regime(date(2026, 10, 21))
    out = run_scan(world, date(2026, 10, 21))
    watchlist = out["watchlist"]
    assert isinstance(watchlist, dict)
    assert watchlist["regime"] == "CONFIRMED_UPTREND"
    with world.engine.connect() as conn:
        rows = conn.execute(select(WatchlistRow.symbol, WatchlistRow.rank)).all()
    assert len(rows) == watchlist["setups"]
    assert sum(r.rank is not None for r in rows) == watchlist["ranked_count"]
    run_scan(world, date(2026, 10, 21))  # a re-run replaces the session's rows
    with world.engine.connect() as conn:
        assert len(conn.execute(select(WatchlistRow.id)).all()) == len(rows)


def test_without_a_regime_the_full_scan_fails_after_the_skeletons_signals(world: World) -> None:
    for symbol, step in RISING.items():
        world.bars(symbol, closes(step))
    with pytest.raises(MissingDataError, match="no computed regime"):
        run_scan(world, date(2026, 10, 21))
    with world.engine.connect() as conn:
        assert conn.execute(select(Signal.symbol)).scalars().all() == ["NVDA", "AMD"]
        assert conn.execute(select(WatchlistRow.id)).all() == []


def test_the_scan_job_writes_nothing_on_incomplete_data(world: World) -> None:
    world.set_session(date(2026, 10, 21), data_status="INCOMPLETE")
    out = run_scan(world, date(2026, 10, 21))
    assert out == {"signals": 0, "skipped": "data status INCOMPLETE"}


def test_old_pending_signals_expire(world: World) -> None:
    world.signal("NVDA", session=date(2026, 10, 21))
    world.signal("AMD", session=date(2026, 10, 22))
    with world.engine.begin() as conn:
        assert expire_signals(conn, before_or_on=date(2026, 10, 21), now=datetime.now(UTC)) == 1
        statuses = {r.symbol: r.status for r in conn.execute(select(Signal.symbol, Signal.status))}
    assert statuses == {"NVDA": "EXPIRED", "AMD": "PENDING"}


def test_the_chain_placeholder_succeeds(world: World) -> None:
    job = analyst_submit.JOB
    assert "placeholder" in dict(job.run(world.context(job.name)) or {})
