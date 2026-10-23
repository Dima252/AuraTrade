"""The real-data scan run end to end on a synthetic history built by the backfill's own stages."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pyarrow as pa

from auratrade.config import Regime, Strategy
from auratrade.data.alpaca_data import DailyBar
from auratrade.history.build import build_history, fetch_bars
from auratrade.history.macro import MACRO, MACRO_SCHEMA
from auratrade.history.securities import KnownAsset
from auratrade.history.store import put_table, table_key
from auratrade.ingest.classify import SecurityType
from auratrade.storage import LocalStore
from auratrade.strategies.base import Correction
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
    assert "## Reading" in report
    scans.write_detail(run, tmp_path / "detail")
    assert (tmp_path / "detail" / "setups.csv").read_text(encoding="utf-8").count("\n") == len(
        run.found
    ) + 1


def test_correction_spells_and_their_positions() -> None:
    days = [date(2020, 3, d) for d in range(2, 14)]
    labels = [Regime.CONFIRMED_UPTREND] * 2 + [Regime.CORRECTION] * 3 + [Regime.EARLY_UPTREND] * 4
    labels += [Regime.CORRECTION] * 3
    spells = scans.correction_spells(dict(zip(days, labels, strict=True)))
    assert spells == [(days[2], days[4]), (days[9], None)]
    # A security listed from the 4th session: the first spell is cut to its bars.
    assert scans.corrections_for(days[3:], spells) == (Correction(0, 1), Correction(6, None))
    # One that stopped trading before the second spell keeps only the first.
    assert scans.corrections_for(days[:6], spells) == (Correction(2, 4),)
    # One that starts after a spell ended drops it.
    assert scans.corrections_for(days[6:], spells) == (Correction(3, None),)
