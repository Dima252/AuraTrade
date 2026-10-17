from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from auratrade.config import RiskConfig
from auratrade.data.alpaca_data import Asset
from auratrade.data.macro import CboeHistory, CboeIndex
from auratrade.data.nasdaq import Listing
from auratrade.history.build import (
    CORRECTIONS,
    SECURITIES,
    StaleWorkError,
    build_history,
    fetch_bars,
)
from auratrade.history.discover import discover, known_assets
from auratrade.history.macro import MACRO, macro_history
from auratrade.history.store import get_table, membership_key, read_bars, table_key
from auratrade.ingest.classify import SecurityType
from auratrade.storage import LocalStore
from tests.calendar_fixture import alpaca_times
from tests.ingest.fakes import RISK, FakeBars

SESSIONS = [s.day for s in alpaca_times() if s.day >= date(2026, 1, 2)][:30]
S = SESSIONS
REU1, REU2 = f"REU:{S[0].isoformat()}", f"REU:{S[16].isoformat()}"


def risk() -> RiskConfig:
    universe = RISK.universe.model_copy(
        update={"min_history_sessions": 3, "dollar_volume_sessions": 2, "min_dollar_volume": 1000.0}
    )
    history = RISK.history.model_copy(
        update={
            "start": S[0],
            "max_gap_sessions": 5,
            "enumeration_every_sessions": 10,
            "enumeration_window_sessions": 5,
            "bar_chunk_symbols": 2,
            "workers": 2,
        }
    )
    regime = RISK.regime.model_copy(update={"indexes": ("SPY",)})
    return RISK.model_copy(update={"universe": universe, "history": history, "regime": regime})


@dataclass
class FakeAlpaca(FakeBars):
    listed: dict[str, list[Asset]] = field(default_factory=dict)

    def assets(self, *, status: str | None = "active") -> list[Asset]:
        return self.listed.get(status or "active", [])


@dataclass
class FakeDirectory:
    entries: list[Listing]

    def listings(self) -> list[Listing]:
        return self.entries


def alpaca_asset(asset_id: str, symbol: str, status: str = "active") -> Asset:
    return Asset(asset_id, symbol, f"{symbol} Inc", "NYSE", status, status == "active", ())


def vendor() -> tuple[FakeAlpaca, FakeDirectory]:
    fake = FakeAlpaca()
    fake.listed = {
        "active": [
            alpaca_asset("a-aaa", "AAA"),
            alpaca_asset("a-new", "NEW"),
            alpaca_asset("a-spy", "SPY"),
            alpaca_asset("a-etf", "ETFX"),
            alpaca_asset("a-unit", "UNI.U"),
        ],
        "inactive": [
            alpaca_asset("i-old", "OLD_DELISTED", "inactive"),
            alpaca_asset("i-junk", "0029900E0", "inactive"),
        ],
    }
    directory = FakeDirectory(
        [
            Listing("AAA", "AAA Inc - Common Stock", "N", False, False),
            Listing("NEW", "New Corp - Class A Common Stock", "N", False, False),
            Listing("SPY", "SPDR S&P 500 ETF Trust", "P", True, False),
            Listing("ETFX", "Some ETF", "P", True, False),
        ]
    )
    fake.add("AAA", S, price=50.0, volume=100)
    fake.add("OLD", S[:10], price=30.0, volume=100)
    fake.add("OLD", S[10:], price=30.0, volume=0)  # zombie bars after the delisting
    fake.add("REU", S[:6], price=20.0, volume=100)  # found only by the sweep...
    fake.add("REU", S[16:], price=40.0, volume=100)  # ...and reused after a 10-session gap
    fake.add("MID", S[6:9], price=20.0, volume=100)  # trades between sweeps: never seen
    fake.add("NEW", S[12:], price=25.0, volume=100)  # listed inside the history
    fake.add("SPY", S, price=500.0, volume=1000)
    fake.add("ETFX", S, price=10.0, volume=1000)
    fake.set_bar("AAA", S[20], high=90.0)  # a bad tick
    return fake, directory


def test_discover_fetch_and_build(tmp_path: Path) -> None:
    fake, directory = vendor()
    cfg = risk()
    work = tmp_path / "work"
    tickers = ["AAA", "OLD", "REU", "MID", "NEW", "SPY", "ETFX", "ZZZ"]
    found = discover(fake, directory, S, cfg, work, tickers=tickers)
    assert found.symbols == ["AAA", "NEW", "OLD", "REU", "SPY"]
    assert "MID" not in found.swept
    by_symbol = {a.symbol: a for a in found.assets}
    assert by_symbol["OLD"].active is False
    assert by_symbol["UNI.U"].security_type is SecurityType.UNIT
    assert "0029900E0" not in by_symbol

    # Resumed from the saved result: no vendor call.
    calls = len(fake.calls)
    assert discover(fake, directory, S, cfg, work, tickers=tickers) == found
    assert len(fake.calls) == calls

    fetched = fetch_bars(fake, found.symbols, S[0], S[-1], cfg, work)
    assert (fetched.chunks, fetched.fetched, fetched.resumed) == (3, 3, 0)
    again = fetch_bars(fake, found.symbols, S[0], S[-1], cfg, work)
    assert (again.fetched, again.resumed) == (0, 3)
    with pytest.raises(StaleWorkError):
        fetch_bars(fake, ["AAA"], S[0], S[-1], cfg, work)

    store = LocalStore(tmp_path / "store")
    report = build_history(store, work, found.assets, S, cfg)
    assert report.tickers == 5
    assert report.by_source == {"ACTIVE": 3, "INACTIVE": 1, "ENUMERATED": 2}
    assert (report.securities, report.active, report.type_assumed, report.relisted) == (6, 3, 2, 1)
    assert report.dropped == {"zero volume": 20}
    assert report.corrections == 1
    assert report.sessions_written == 30

    securities = {r["security"]: r for r in get_table(store, table_key(SECURITIES)).to_pylist()}
    assert set(securities) == {"a-aaa", "a-new", "a-spy", "i-old", REU1, REU2}
    assert securities["i-old"]["last_session"] == S[9]
    assert securities["a-new"]["listed_at"] == S[12]
    assert securities["a-aaa"]["listed_at"] is None  # trades from the history's first session
    assert securities[REU2]["relisted"] is True
    assert securities[REU2]["listed_at"] is None
    assert securities["a-spy"]["security_type"] == "ETF"

    bars = read_bars(store, S[0], S[-1]).to_pylist()
    assert len(bars) == 30 + 10 + 6 + 14 + 18 + 30
    aaa = [b for b in bars if b["security"] == "a-aaa"]
    assert aaa[20]["high"] < 90.0
    [correction] = get_table(store, table_key(CORRECTIONS)).to_pylist()
    assert (correction["security"], correction["original"]) == ("a-aaa", 90.0)

    members = get_table(store, membership_key(2026)).to_pylist()
    on = {d: {m["security"] for m in members if m["session_date"] == d} for d in S}
    # From each security's third bar; SPY (an ETF) never; OLD only while it traded.
    assert on[S[1]] == set()
    assert on[S[2]] == {"a-aaa", "i-old", REU1}
    assert on[S[14]] == {"a-aaa", "a-new"}
    assert on[S[29]] == {"a-aaa", "a-new", REU2}
    assert report.members == {"min": 1, "median": 3, "max": 3}  # only AAA in S[10:14]


def test_inactive_assets_are_classified_by_their_alpaca_name() -> None:
    fund = Asset("i-1", "XYZ_DELISTED", "XYZ Income Fund", "NYSE", "inactive", False, ())
    otc = Asset("i-2", "QQQQ", "Q Corp", "OTC", "inactive", False, ())
    [a] = known_assets([], [fund, otc], [])
    assert (a.symbol, a.active, a.security_type) == ("XYZ", False, SecurityType.FUND)


def test_macro_history_keeps_sessions_only(tmp_path: Path) -> None:
    holiday = date(2026, 1, 1)

    class Cboe:
        def history(self, index: CboeIndex) -> CboeHistory:
            return CboeHistory(index, {holiday: 1.0, S[0]: 15.0, S[1]: 16.0})

    store = LocalStore(tmp_path)
    assert macro_history(store, Cboe(), set(S)) == 4
    rows: list[dict[str, Any]] = get_table(store, table_key(MACRO)).to_pylist()
    assert [(r["series"], r["observation_date"]) for r in rows] == [
        ("VIX", S[0]),
        ("VIX", S[1]),
        ("VIX3M", S[0]),
        ("VIX3M", S[1]),
    ]
