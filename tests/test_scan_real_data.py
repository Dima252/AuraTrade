"""The real-data scan run end to end on a synthetic history built by the backfill's own stages."""

from __future__ import annotations

from pathlib import Path

from auratrade.config import Strategy
from auratrade.data.alpaca_data import DailyBar
from auratrade.history.build import build_history, fetch_bars
from auratrade.history.securities import KnownAsset
from auratrade.ingest.classify import SecurityType
from auratrade.storage import LocalStore
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
    symbols = sorted(fake.raw)
    assets = [KnownAsset(f"a-{s}", s, s, "NYSE", True, SecurityType.COMMON) for s in symbols]
    risk = RISK.model_copy(
        update={"history": RISK.history.model_copy(update={"start": SESSIONS[0], "workers": 2})}
    )
    work, store = tmp_path / "work", LocalStore(tmp_path / "store")
    fetch_bars(fake, symbols, SESSIONS[0], SESSIONS[-1], risk, work)
    build_history(store, work, assets, SESSIONS, risk)

    run = scans.run_scans(store, SESSIONS, risk)
    assert run.exceptions == []
    assert run.sessions[0] >= scans.FIRST_REPORTED
    eps = [f for f in run.found if f.setup.strategy is Strategy.EPISODIC_PIVOT]
    assert [(f.symbol, f.day) for f in eps] == [("GAPR", SESSIONS[GAP_AT])]
    assert eps[0].setup.pivot == 34.5
    assert eps[0].setup.structural_stop == 30.0

    report = scans.render(run, risk, "")
    assert "## Setups per year" in report
    assert "## Reading" in report
    scans.write_detail(run, tmp_path / "detail")
    assert (tmp_path / "detail" / "setups.csv").read_text(encoding="utf-8").count("\n") == len(
        run.found
    ) + 1
