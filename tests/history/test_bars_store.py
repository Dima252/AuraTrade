from __future__ import annotations

from datetime import date
from pathlib import Path

from auratrade.config import DEFAULT_RISK_CONFIG, load_risk_config
from auratrade.history.bars import NO_RAW, prepare
from auratrade.history.store import bar_sessions, read_bars, write_bar_session
from auratrade.ingest.bars import IngestBar
from auratrade.storage import LocalStore

RISK = load_risk_config(DEFAULT_RISK_CONFIG)
D = [date(2026, 3, d) for d in range(2, 31)]
POSITION = {d: i for i, d in enumerate(D)}


def bar(day: date, price: float = 50.0, volume: int = 1000, high: float | None = None) -> IngestBar:
    return IngestBar(day, price, high or price * 1.01, price * 0.99, price, volume, 0.0)


def test_prepare_drops_bad_bars_splits_gaps_and_corrects_outliers() -> None:
    days = D[:6] + D[12:16]
    bars = [bar(d) for d in days]
    bars[2] = bar(days[2], high=90.0)  # a bad tick: far above the body and both neighbours
    bars[4] = bar(days[4], volume=0)  # a zombie bar
    raw = {d: 100.0 for d in days if d != days[5]}  # no raw bar for one session
    out = prepare(bars, raw, POSITION, max_gap=5, max_jump=4.0, outliers=RISK.ingest.outliers)
    assert out.dropped == {NO_RAW: 1, "zero volume": 1}
    assert [(p.first, p.last) for p in out.pieces] == [(D[0], D[3]), (D[12], D[15])]
    first = out.pieces[0]
    assert [c.session for c in first.corrections] == [D[2]]
    assert first.bars[2].high == 50.0
    assert all(b.raw_close == 100.0 for p in out.pieces for b in p.bars)


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


def test_prepare_starts_a_new_piece_at_a_level_jump() -> None:
    # A reorganization: the old shares at $0.50, the new ones at $30, no gap in trading.
    bars = [bar(d, 0.5) for d in D[:5]] + [bar(d, 30.0) for d in D[5:10]]
    raw = {d: b.close for d, b in zip(D, bars, strict=False)}
    out = prepare(bars, raw, POSITION, max_gap=5, max_jump=4.0, outliers=RISK.ingest.outliers)
    assert [(p.first, p.last) for p in out.pieces] == [(D[0], D[4]), (D[5], D[9])]
