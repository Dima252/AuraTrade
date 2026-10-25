"""Minute bars for the simulator, cached in the object store (archived README §5.12).

The evening `simulate` job fetches session T's SIP and IEX minute bars for its symbols only (the
baseline book's entries and open trades, the scorecard's), and every later evening re-plays the
open trades from their entry session, so the bars are kept: one Parquet object per session and
feed, `sim/minute_bars/feed=<feed>/YYYY-MM-DD.parquet`. A symbol missing from a session's object
is fetched and merged in; the object records which symbols were fetched, so a symbol with no
trades on a feed isn't fetched again. Bars are as traded (unadjusted).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import pyarrow as pa

from auratrade.data.alpaca_data import Feed
from auratrade.history.store import get_table, put_table
from auratrade.sim.fills import MinuteBar
from auratrade.sim.sessions import Minutes
from auratrade.storage import ObjectStore

PREFIX = "sim/minute_bars"
IEX_HISTORY_START = date(2020, 7, 27)
"""Alpaca's IEX minute history begins here (spike 0.3a); before it SIP sets the levels."""
_FETCHED = b"fetched"

SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("start", pa.timestamp("s", tz="UTC")),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
    ]
)


@dataclass(frozen=True, slots=True)
class Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class MinuteSource(Protocol):
    """``AlpacaDataClient.minute_bars``."""

    def minute_bars(
        self, symbols: Iterable[str], start: datetime, end: datetime, *, feed: Feed
    ) -> Mapping[str, Sequence[MinuteBar]]: ...


def key(session: date, feed: Feed) -> str:
    return f"{PREFIX}/feed={feed.value}/{session.isoformat()}.parquet"


def _read(store: ObjectStore, session: date, feed: Feed) -> tuple[dict[str, list[Bar]], set[str]]:
    table = get_table(store, key(session, feed))
    if table is None:
        return {}, set()
    meta = table.schema.metadata or {}
    fetched = set(json.loads(meta.get(_FETCHED, b"[]")))
    out: dict[str, list[Bar]] = {s: [] for s in fetched}
    for row in table.to_pylist():
        out.setdefault(row["symbol"], []).append(
            Bar(row["start"], row["open"], row["high"], row["low"], row["close"], row["volume"])
        )
    return out, fetched


def _write(store: ObjectStore, session: date, feed: Feed, bars: Mapping[str, list[Bar]]) -> None:
    rows = [
        {
            "symbol": symbol,
            "start": b.start,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for symbol in sorted(bars)
        for b in bars[symbol]
    ]
    table = pa.Table.from_pylist(rows, SCHEMA).replace_schema_metadata(
        {_FETCHED: json.dumps(sorted(bars)).encode()}
    )
    put_table(store, key(session, feed), table)


class MinuteCache:
    def __init__(self, store: ObjectStore, source: MinuteSource) -> None:
        self._store = store
        self._source = source

    def bars(
        self,
        session: date,
        symbols: Iterable[str],
        *,
        open_at: datetime,
        close_at: datetime,
        feed: Feed,
    ) -> dict[str, list[Bar]]:
        """The session's minute bars for ``symbols`` on ``feed``, fetching what isn't cached."""
        wanted = set(symbols)
        cached, fetched = _read(self._store, session, feed)
        missing = sorted(wanted - fetched)
        if missing:
            got = self._source.minute_bars(missing, open_at, close_at, feed=feed)
            for symbol in missing:
                cached[symbol] = [
                    Bar(b.start, b.open, b.high, b.low, b.close, int(b.volume))
                    for b in got.get(symbol, ())
                ]
            _write(self._store, session, feed, cached)
        return {s: cached.get(s, []) for s in wanted}

    def minutes(
        self, session: date, symbols: Iterable[str], *, open_at: datetime, close_at: datetime
    ) -> dict[str, Minutes]:
        """SIP and IEX bars per symbol; a symbol with no SIP bar that session has none."""
        wanted = sorted(set(symbols))
        sip = self.bars(session, wanted, open_at=open_at, close_at=close_at, feed=Feed.SIP)
        iex: dict[str, list[Bar]] | None = None
        if session >= IEX_HISTORY_START:
            iex = self.bars(session, wanted, open_at=open_at, close_at=close_at, feed=Feed.IEX)
        return {
            s: Minutes(sip=sip[s], iex=None if iex is None else iex[s]) for s in wanted if sip[s]
        }
