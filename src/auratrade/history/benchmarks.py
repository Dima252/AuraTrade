"""Backfill stage: the momentum book's benchmarks, SPY and MTUM, as total-return daily bars
(`adjustment=all`: dividends reinvested; `docs/backtests/momentum-preregistration.md` §4).

They sit in their own table, apart from the split-only bars every other reader uses, so no
signal or fill ever reads a dividend-adjusted price. Free calls; fetched once, re-fetched whole
when the stage runs again (the adjustment rewrites history at every dividend).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import pyarrow as pa

from auratrade.data.alpaca_data import Adjustment, DailyBar
from auratrade.history.store import get_table, put_table, table_key
from auratrade.storage import ObjectStore

BENCHMARKS = "benchmarks"
BENCHMARK_SYMBOLS = ("SPY", "MTUM")
BENCHMARK_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("session_date", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
    ]
)


class BarSource(Protocol):
    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date, *, adjustment: Adjustment = ...
    ) -> dict[str, list[DailyBar]]: ...


def benchmark_history(
    store: ObjectStore,
    alpaca: BarSource,
    start: date,
    end: date,
    sessions: Collection[date],
    symbols: Sequence[str] = BENCHMARK_SYMBOLS,
) -> dict[str, int]:
    """Fetch and store the benchmarks' bars of ``sessions`` in [start, end]; the bars stored per
    symbol. A symbol the vendor doesn't return is an error: the gate can't run without it."""
    bars = alpaca.daily_bars(list(symbols), start, end, adjustment=Adjustment.ALL)
    missing = sorted(set(symbols) - bars.keys())
    if missing:
        raise ValueError(f"no total-return bars for {missing}")
    rows = [
        {
            "symbol": symbol,
            "session_date": b.session,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for symbol in symbols
        for b in bars[symbol]
        if b.session in sessions
    ]
    put_table(store, table_key(BENCHMARKS), pa.Table.from_pylist(rows, BENCHMARK_SCHEMA))
    return {symbol: sum(1 for r in rows if r["symbol"] == symbol) for symbol in symbols}


@dataclass(frozen=True, slots=True)
class Benchmark:
    symbol: str
    dates: list[date]
    closes: list[float]
    """Total-return closes, oldest first."""


def load_benchmarks(store: ObjectStore) -> dict[str, Benchmark]:
    table = get_table(store, table_key(BENCHMARKS))
    if table is None:
        raise FileNotFoundError(
            "no benchmarks table in the history: run `scripts/backfill.py benchmarks`"
        )
    out: dict[str, Benchmark] = {}
    ordered = table.sort_by([("symbol", "ascending"), ("session_date", "ascending")])
    for r in ordered.select(["symbol", "session_date", "close"]).to_pylist():
        b = out.setdefault(r["symbol"], Benchmark(r["symbol"], [], []))
        b.dates.append(r["session_date"])
        b.closes.append(float(r["close"]))
    return out
