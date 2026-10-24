"""The real-data scan run end to end on a synthetic history built by the backfill's own stages."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pyarrow as pa

from auratrade.config import Strategy
from auratrade.data.alpaca_data import DailyBar
from auratrade.history.build import build_history, fetch_bars
from auratrade.history.macro import MACRO, MACRO_SCHEMA
from auratrade.history.securities import KnownAsset
from auratrade.history.store import put_table, table_key
from auratrade.indicators.earnings import EarningsTiming
from auratrade.ingest.classify import SecurityType
from auratrade.storage import LocalStore
from auratrade.strategies.ranking import Fundamentals
from scripts import scan_real_data as scans
from tests.ingest.fakes import RISK, FakeBars
from tests.test_smoke_real_data import SESSIONS, wave

GAP_AT = 450


def gapper(fake: FakeBars) -> None:
    """Flat at 30, then a 15% gap on 4x volume that holds."""
    series = fake.raw.setdefault("GAPR", {})
    for t, day in enumerate(SESSIONS):
        p, volume = (34.5, 8_000_000) if t >= GAP_AT else (30.0, 2_000_000)
        if t > GAP_AT:
            volume = 2_000_000
        series[day] = DailyBar("GAPR", day, p, p * 1.01, p * 0.99, p, volume)


def test_scan_run_on_a_synthetic_history(tmp_path: Path) -> None:
    fake = FakeBars()
    for i, drift in enumerate((0.003, 0.002, 0.001, 0.0, -0.001)):
        wave(fake, f"S{i}", drift, 9 + i)
    gapper(fake)
    wave(fake, "SPY", 0.0008, 25)
    wave(fake, "QQQ", 0.001, 21)
    symbols = sorted(fake.raw)
    assets = [
        KnownAsset(
            f"a-{s}",
            s,
            s,
            "NYSE",
            True,
            SecurityType.ETF if s in ("SPY", "QQQ") else SecurityType.COMMON,
        )
        for s in symbols
    ]
    risk = RISK.model_copy(
        update={"history": RISK.history.model_copy(update={"start": SESSIONS[0], "workers": 2})}
    )
    work, store = tmp_path / "work", LocalStore(tmp_path / "store")
    fetch_bars(fake, symbols, SESSIONS[0], SESSIONS[-1], risk, work)
    build_history(store, work, assets, SESSIONS, risk)
    macro = [
        {"series": series, "observation_date": d, "value": value, "source": "CBOE_HISTORY"}
        for d in SESSIONS
        for series, value in (("VIX", 15.0), ("VIX3M", 17.0))
    ]
    put_table(store, table_key(MACRO), pa.Table.from_pylist(macro, MACRO_SCHEMA))

    run = scans.run_scans(store, SESSIONS, dict.fromkeys(SESSIONS, 390), risk)
    assert len(run.regimes) == len(SESSIONS) - 1
    assert run.exceptions == []
    assert run.sessions[0] >= scans.FIRST_REPORTED
    eps = [f for f in run.found if f.setup.strategy is Strategy.EPISODIC_PIVOT]
    assert [(f.symbol, f.day) for f in eps] == [("GAPR", SESSIONS[GAP_AT])]
    assert eps[0].setup.pivot == 34.5
    assert eps[0].setup.structural_stop == 30.0

    report = scans.render(run, risk, "")
    assert "## Setups per year" in report
    assert "## Setups by regime" in report
    assert "## Ranking and the cap (3.3)" in report
    assert len(run.ranked) == len(run.sessions)
    assert "## Reading" in report
    scans.write_detail(run, tmp_path / "detail")
    assert (tmp_path / "detail" / "setups.csv").read_text(encoding="utf-8").count("\n") == len(
        run.found
    ) + 1


def test_reaction_sessions_and_point_in_time_fundamentals() -> None:
    days = [date(2021, 2, d) for d in (1, 2, 3, 4, 5, 8)]
    reports = [
        (date(2021, 2, 2), EarningsTiming.BEFORE_OPEN),  # reacts that session (1)
        (date(2021, 2, 3), EarningsTiming.AFTER_CLOSE),  # the next one (3)
        (date(2021, 2, 6), EarningsTiming.BEFORE_OPEN),  # a Saturday: Monday (5)
        (date(2021, 2, 8), EarningsTiming.AFTER_CLOSE),  # after the last bar: dropped
        (date(2021, 3, 1), EarningsTiming.BEFORE_OPEN),  # after the history: dropped
    ]
    assert scans.reaction_sessions(days, reports) == frozenset({1, 3, 5})
    q1, q2 = Fundamentals(0.3, None), Fundamentals(0.5, 0.2)
    quarters = [(date(2021, 2, 2), q1), (date(2021, 2, 5), q2)]
    assert scans.known_on(quarters, date(2021, 2, 2)) is None  # filed that day: not yet
    assert scans.known_on(quarters, date(2021, 2, 3)) == q1
    assert scans.known_on(quarters, date(2021, 2, 8)) == q2
