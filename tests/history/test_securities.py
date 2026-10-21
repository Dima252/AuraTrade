from __future__ import annotations

from datetime import date

from auratrade.history.securities import (
    KnownAsset,
    Source,
    clean_symbol,
    securities_for,
    segments,
    sweep_tickers,
    sweep_windows,
)
from auratrade.ingest.classify import SecurityType

D = [date(2026, 1, d) for d in range(1, 32)]


def asset(asset_id: str, active: bool, kind: SecurityType = SecurityType.COMMON) -> KnownAsset:
    return KnownAsset(asset_id, "ABC", f"ABC {asset_id}", "NYSE", active, kind)


def test_clean_symbol() -> None:
    assert clean_symbol("TWTR_DELISTED") == "TWTR"
    assert clean_symbol("BRK.B") == "BRK.B"
    assert clean_symbol("0029900E0") is None
    assert clean_symbol("D012219") is None


def test_sweep_tickers() -> None:
    assert len(sweep_tickers()) == 26 + 26**2 + 26**3 + 26**4
    assert sweep_tickers(1)[:3] == ["A", "B", "C"]


def test_sweep_windows_cover_the_end() -> None:
    sessions = D[:10]
    assert sweep_windows(sessions, every=4, length=2) == [
        (D[0], D[1]),
        (D[4], D[5]),
        (D[8], D[9]),
    ]
    # The last window is cut short by the end, so a full one ending there is added.
    assert sweep_windows(D[:9], every=4, length=2)[-2:] == [(D[8], D[8]), (D[7], D[8])]
    assert sweep_windows([], every=4, length=2) == []


def test_segments_split_on_long_gaps() -> None:
    position = {d: i for i, d in enumerate(D)}
    days = [D[0], D[1], D[3], D[10], D[11]]
    # D[1] → D[3]: one session missing (kept); D[3] → D[10]: six missing (split at 5).
    assert segments(days, position, max_gap=5) == [(0, 2), (3, 4)]
    assert segments(days, position, max_gap=6) == [(0, 4)]
    assert segments([], position, max_gap=5) == []


def test_segments_split_on_level_jumps() -> None:
    position = {d: i for i, d in enumerate(D)}
    closes = [10.0, 10.5, 50.0, 49.0, 12.0, 11.0]  # x4.8 up, then down to a quarter
    assert segments(D[:6], position, 5, closes=closes, max_jump=4.0) == [(0, 1), (2, 3), (4, 5)]
    assert segments(D[:6], position, 5, closes=closes, max_jump=5.0) == [(0, 5)]


def test_one_piece_takes_the_active_asset() -> None:
    [sec] = securities_for("ABC", [(D[0], D[9])], [asset("a1", True)], trading_since=D[5])
    assert (sec.key, sec.alpaca_asset_id, sec.source, sec.active) == (
        "a1",
        "a1",
        Source.ACTIVE,
        True,
    )
    assert not sec.type_assumed


def test_reused_ticker_pieces_take_active_then_inactive_then_symbol_keys() -> None:
    pieces = [(D[0], D[2]), (D[5], D[7]), (D[10], D[12])]
    found = securities_for(
        "ABC", pieces, [asset("old", False), asset("new", True)], trading_since=D[11]
    )
    assert [s.key for s in found] == ["ABC:2026-01-01", "old", "new"]
    assert [s.source for s in found] == [Source.ENUMERATED, Source.INACTIVE, Source.ACTIVE]
    assert [s.active for s in found] == [False, False, True]
    assert found[0].type_assumed
    assert found[0].security_type is SecurityType.COMMON


def test_an_old_piece_is_not_todays_active_security() -> None:
    # The active asset is a new listing that hasn't traded in the history yet.
    found = securities_for("ABC", [(D[0], D[2])], [asset("new", True)], trading_since=D[20])
    assert [(s.key, s.active) for s in found] == [("ABC:2026-01-01", False)]


def test_inactive_only_and_unknown_symbols() -> None:
    [sec] = securities_for("ABC", [(D[0], D[2])], [asset("old", False)], trading_since=D[20])
    assert (sec.key, sec.source, sec.active) == ("old", Source.INACTIVE, False)
    [swept] = securities_for("ABC", [(D[3], D[4])], [], trading_since=D[0])
    assert (swept.key, swept.source, swept.name) == ("ABC:2026-01-04", Source.ENUMERATED, None)
