"""The smoke run end to end on a synthetic history built by the backfill's own stages."""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa

from auratrade.data.alpaca_data import DailyBar
from auratrade.history.build import build_history, fetch_bars
from auratrade.history.macro import MACRO, MACRO_SCHEMA
from auratrade.history.securities import KnownAsset
from auratrade.history.store import put_table, table_key
from auratrade.ingest.classify import SecurityType
from auratrade.storage import LocalStore
from scripts import backfill
from scripts import smoke_real_data as smoke
from tests.ingest.fakes import RISK, FakeBars

N = 600


def weekdays(start: date, n: int) -> list[date]:
    out, day = [], start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


SESSIONS = weekdays(date(2016, 1, 4), N)


def wave(fake: FakeBars, symbol: str, drift: float, period: float, start: int = 0) -> None:
    """A trending price with pullbacks, so bases form and break out."""
    series = fake.raw.setdefault(symbol, {})
    for t, day in enumerate(SESSIONS[start:]):
        p = 20.0 * (1 + drift) ** t * (1 + 0.12 * math.sin(t / period))
        series[day] = DailyBar(symbol, day, p, p * 1.02, p * 0.98, p * 1.005, 2_000_000)


def test_smoke_run_on_a_synthetic_history(tmp_path: Path) -> None:
    fake = FakeBars()
    for i, drift in enumerate((0.003, 0.002, 0.001, 0.0, -0.001, 0.0025)):
        wave(fake, f"S{i}", drift, 9 + i)
    wave(fake, "IPO", 0.003, 11, start=300)  # listed inside the history
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

    result = smoke.run_smoke(store, SESSIONS, dict.fromkeys(SESSIONS, 390), risk)
    assert result.exceptions == []
    assert sum(result.non_finite.values()) == 0
    assert result.regime_error is None
    assert len(result.regime) == N - 1
    assert result.member_securities == 7
    assert max(result.tt_pass.values()) > 0
    assert result.breakouts > 0
    assert result.trades
    assert not any(str(t["exit"]).startswith("error") for t in result.trades)

    text = smoke.render(result, risk)
    for heading in ("## Inputs", "## Regime (2.3)", "## Trend Template (3.1)", "## Bases"):
        assert heading in text
    report = tmp_path / "smoke.md"
    hand = "## Reading\n\nBy hand.\n"
    report.write_text(f"{text}\n{hand}", encoding="utf-8")
    again = smoke.render(result, risk, smoke.kept_reading(report))
    assert again.endswith(hand)
    assert smoke.kept_reading(tmp_path / "missing.md") == ""
    smoke.write_detail(result, tmp_path / "detail")
    rows = (tmp_path / "detail" / "regime.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == N  # a header and one row per classified session


def test_last_final_session() -> None:
    from datetime import UTC, datetime

    from auratrade.calendar.sessions import MarketCalendar
    from tests.calendar_fixture import alpaca_times, to_session

    calendar = MarketCalendar(to_session(s) for s in alpaca_times())
    thursday = date(2026, 10, 15)
    # 16:10 New York: the close plus the SIP delay hasn't passed yet.
    assert backfill.last_final_session(
        calendar, datetime(2026, 10, 15, 20, 10, tzinfo=UTC)
    ) == date(2026, 10, 14)
    assert (
        backfill.last_final_session(calendar, datetime(2026, 10, 15, 20, 30, tzinfo=UTC))
        == thursday
    )
    # A Saturday: the Friday.
    assert backfill.last_final_session(calendar, datetime(2026, 10, 17, 15, 0, tzinfo=UTC)) == date(
        2026, 10, 16
    )
