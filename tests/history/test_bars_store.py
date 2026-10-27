"""The Parquet bar store (`history.store`): one object per session, merged or replaced; the
evening retention archives Postgres's old bars through it."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from auratrade.history.store import bar_sessions, read_bars, write_bar_session
from auratrade.storage import LocalStore

D = [date(2026, 3, d) for d in range(2, 31)]


def row(day: date, security: str, symbol: str, close: float) -> dict[str, object]:
    return {
        "session_date": day,
        "security": security,
        "symbol": symbol,
        "alpaca_asset_id": None,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 10,
        "raw_close": close,
    }


def test_bar_sessions_write_merge_and_read(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    assert write_bar_session(store, D[0], [row(D[0], "b", "BBB", 1.0)], merge=False) == 1
    write_bar_session(store, D[1], [row(D[1], "b", "BBB", 2.0)], merge=False)
    # Merge: AAA replaces nothing, BBB is updated, the stored ZZZ stays.
    write_bar_session(store, D[1], [row(D[1], "z", "ZZZ", 9.0)], merge=True)
    assert (
        write_bar_session(
            store, D[1], [row(D[1], "a", "AAA", 3.0), row(D[1], "b", "BBB", 4.0)], merge=True
        )
        == 3
    )
    assert bar_sessions(store) == [D[0], D[1]]
    table = read_bars(store, D[0], D[1])
    assert [(r["security"], r["session_date"], r["close"]) for r in table.to_pylist()] == [
        ("a", D[1], 3.0),
        ("b", D[0], 1.0),
        ("b", D[1], 4.0),
        ("z", D[1], 9.0),
    ]
    assert read_bars(store, D[1], D[1], securities=["z"]).num_rows == 1
    assert read_bars(store, D[5], D[6]).num_rows == 0
    # Without merge the session is replaced.
    write_bar_session(store, D[1], [row(D[1], "a", "AAA", 5.0)], merge=False)
    assert read_bars(store, D[1], D[1]).num_rows == 1
