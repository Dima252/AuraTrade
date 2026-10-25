"""Reading the Parquet history back for backtests and the smoke run (README §15).

`load_series` pivots the per-session objects into one series per security. About 9 million
bars are in play, so the values sit in typed arrays (8 bytes each, not a Python float object)
and the dates are references to one shared list of sessions.
"""

from __future__ import annotations

import bisect
from array import array
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from auratrade.config import RiskConfig
from auratrade.history.build import SECURITIES
from auratrade.history.edgar import EARNINGS, FUNDAMENTALS
from auratrade.history.macro import MACRO
from auratrade.history.store import (
    BAR_COLUMNS,
    BAR_SCHEMA,
    bar_key,
    bar_sessions,
    from_bytes,
    get_table,
    membership_key,
    table_key,
)
from auratrade.indicators.earnings import EarningsTiming
from auratrade.indicators.relative_strength import percentile_ranks
from auratrade.regime.breadth import TickerHistory
from auratrade.regime.inputs import MacroSeries
from auratrade.storage import ObjectStore
from auratrade.strategies.ranking import Fundamentals
from auratrade.strategies.trend_template import rs_scores


@dataclass(frozen=True, slots=True)
class HistoryBar:
    """One bar, in the shape the indicators read (`indicators.bars.OHLCV`)."""

    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class SecuritySeries:
    """One security's bars, oldest first: split-adjusted OHLCV plus the close as traded."""

    security: str
    dates: list[date] = field(default_factory=list)
    open: array[float] = field(default_factory=lambda: array("d"))
    high: array[float] = field(default_factory=lambda: array("d"))
    low: array[float] = field(default_factory=lambda: array("d"))
    close: array[float] = field(default_factory=lambda: array("d"))
    raw_close: array[float] = field(default_factory=lambda: array("d"))
    volume: array[float] = field(default_factory=lambda: array("d"))

    def __len__(self) -> int:
        return len(self.dates)

    def bars(self) -> list[HistoryBar]:
        return [
            HistoryBar(o, h, lo, c, v)
            for o, h, lo, c, v in zip(
                self.open, self.high, self.low, self.close, self.volume, strict=True
            )
        ]

    def ticker_history(self) -> TickerHistory:
        return TickerHistory(
            dates=self.dates,
            highs=self.high,
            lows=self.low,
            closes=self.close,
            raw_closes=self.raw_close,
            volumes=self.volume,
        )


def load_series(
    store: ObjectStore,
    sessions: Sequence[date],
    *,
    securities: Collection[str] | None = None,
) -> dict[str, SecuritySeries]:
    """Every stored bar of `sessions` (consecutive, oldest first), by security. `securities`
    limits the read; a session without an object is skipped."""
    stored = set(bar_sessions(store))
    wanted = None if securities is None else pa.array(sorted(set(securities)), pa.string())
    out: dict[str, SecuritySeries] = {}
    for day in sessions:
        if day not in stored:
            continue
        table = from_bytes(store.get(bar_key(day))).select(list(BAR_COLUMNS)).cast(BAR_SCHEMA)
        if wanted is not None:
            table = table.filter(pc.is_in(table["security"], wanted))
        columns = table.to_pydict()
        for i, key in enumerate(columns["security"]):
            s = out.get(key)
            if s is None:
                s = out[key] = SecuritySeries(key)
            s.dates.append(day)
            s.open.append(columns["open"][i])
            s.high.append(columns["high"][i])
            s.low.append(columns["low"][i])
            s.close.append(columns["close"][i])
            s.raw_close.append(columns["raw_close"][i])
            s.volume.append(float(columns["volume"][i]))
    return out


def load_securities(store: ObjectStore) -> dict[str, dict[str, Any]]:
    table = get_table(store, table_key(SECURITIES))
    if table is None:
        raise FileNotFoundError("no securities table in the history: run the backfill")
    return {str(r["security"]): r for r in table.to_pylist()}


def load_membership(store: ObjectStore, years: Collection[int]) -> dict[date, set[str]]:
    """The universe per session (README §5.1, point in time)."""
    out: dict[date, set[str]] = {}
    for year in sorted(years):
        table = get_table(store, membership_key(year))
        if table is None:
            continue
        columns = table.to_pydict()
        for day, key in zip(columns["session_date"], columns["security"], strict=True):
            out.setdefault(day, set()).add(key)
    return out


def load_macro(store: ObjectStore) -> dict[str, MacroSeries]:
    table = get_table(store, table_key(MACRO))
    if table is None:
        raise FileNotFoundError("no macro table in the history: run the backfill's macro stage")
    points: dict[str, tuple[list[date], list[float]]] = {}
    for r in table.sort_by([("observation_date", "ascending")]).to_pylist():
        days, values = points.setdefault(str(r["series"]), ([], []))
        days.append(r["observation_date"])
        values.append(float(r["value"]))
    return {name: MacroSeries(days, values) for name, (days, values) in points.items()}


def index_securities(
    securities: Mapping[str, Mapping[str, Any]], symbols: Collection[str]
) -> dict[str, str]:
    """The active security of each index symbol (the regime's ETFs)."""
    out = {}
    for key, s in securities.items():
        if s["symbol"] in symbols and s["active"]:
            out[str(s["symbol"])] = key
    missing = sorted(set(symbols) - out.keys())
    if missing:
        raise KeyError(f"index ETFs not in the history: {missing}")
    return out


def ranks_by_security(
    series: Mapping[str, SecuritySeries], members: Mapping[date, set[str]], risk: RiskConfig
) -> tuple[dict[str, list[float | None]], dict[str, list[float | None]]]:
    """Each security's RS rating and best momentum rank per bar, ranked within the session's
    universe (archived README §5.3). Non-members get None: they are never scanned."""
    lookbacks = risk.strategies.MOMENTUM_BREAKOUT.rank_lookbacks
    rs: dict[date, dict[str, float]] = defaultdict(dict)
    returns: dict[tuple[date, int], dict[str, float]] = defaultdict(dict)
    for key, s in series.items():
        closes = s.close
        for i, (d, score) in enumerate(
            zip(s.dates, rs_scores(closes, risk.trend_template), strict=True)
        ):
            if key not in members.get(d, ()):
                continue
            if score is not None:
                rs[d][key] = score
            for n in lookbacks:
                if i >= n:
                    returns[d, n][key] = closes[i] / closes[i - n] - 1
    rs_pct = {d: percentile_ranks(v) for d, v in rs.items()}
    mom_pct = {k: percentile_ranks(v) for k, v in returns.items()}
    rs_out: dict[str, list[float | None]] = {}
    mom_out: dict[str, list[float | None]] = {}
    for key, s in series.items():
        rs_out[key] = [rs_pct.get(d, {}).get(key) for d in s.dates]
        best: list[float | None] = []
        for d in s.dates:
            values = [r for n in lookbacks if (r := mom_pct.get((d, n), {}).get(key)) is not None]
            best.append(max(values) if values else None)
        mom_out[key] = best
    return rs_out, mom_out


def earnings_by_security(store: ObjectStore) -> dict[str, list[tuple[date, EarningsTiming]]]:
    table = get_table(store, table_key(EARNINGS))
    rows = [] if table is None else table.select(["security", "report_date", "timing"]).to_pylist()
    out: dict[str, list[tuple[date, EarningsTiming]]] = defaultdict(list)
    for row in rows:
        if row["timing"]:
            out[row["security"]].append((row["report_date"], EarningsTiming(row["timing"].lower())))
    return out


def fundamentals_by_security(store: ObjectStore) -> dict[str, list[tuple[date, Fundamentals]]]:
    """Each security's quarters by `filed` date, oldest first."""
    table = get_table(store, table_key(FUNDAMENTALS))
    columns = ["security", "filed_date", "eps_growth", "eps_acceleration"]
    rows = [] if table is None else table.select(columns).to_pylist()
    out: dict[str, list[tuple[date, Fundamentals]]] = defaultdict(list)
    for row in rows:
        out[row["security"]].append(
            (row["filed_date"], Fundamentals(row["eps_growth"], row["eps_acceleration"]))
        )
    for quarters in out.values():
        quarters.sort(key=lambda q: q[0])
    return out


def known_on(quarters: Sequence[tuple[date, Fundamentals]], day: date) -> Fundamentals | None:
    """The latest quarter filed before ``day`` (README §15: readable from the next session)."""
    i = bisect.bisect_left([q[0] for q in quarters], day)
    return quarters[i - 1][1] if i > 0 else None
