"""Backfill stages 2 and 3: fetch the bars, then build the history from them.

- **Fetch:** split-adjusted and raw SIP daily bars from the history start for every discovered
  symbol, `bar_chunk_symbols` a request over `workers` threads (the requests are bound by the
  server's response time; the shared limiter keeps them under Alpaca's 200 a minute). Each chunk
  is saved as it arrives, so an interrupted fetch resumes; the vendor's values are kept as they
  came, so the build can be re-run without fetching again.
- **Build:** each ticker's bars through `history.bars.prepare` (the ingest rules), split into
  securities (`history.securities`), then written one object per session, with the securities
  table, the bar corrections and the universe membership: the securities of a universe type (on
  a universe exchange where known) passing the README §5.1 filters at each session. A security
  that repeats another's bars (one company under its old and new ticker) is dropped first.
"""

from __future__ import annotations

import itertools
import json
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import structlog

from auratrade.config import RiskConfig
from auratrade.data.alpaca_data import Adjustment, DailyBar
from auratrade.history.bars import prepare
from auratrade.history.securities import DELISTED_FUNDS, KnownAsset, Security, securities_for
from auratrade.history.store import (
    BAR_SCHEMA,
    get_table,
    membership_key,
    put_table,
    table_key,
    write_bar_table,
)
from auratrade.indicators.universe import universe_flags
from auratrade.ingest.bars import IngestBar
from auratrade.storage import ObjectStore

log = structlog.get_logger(__name__)

STAGE_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("session_date", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("raw_close", pa.float64()),
    ]
)
MEMBER_SCHEMA = pa.schema([("session_date", pa.date32()), ("security", pa.string())])
SECURITY_SCHEMA = pa.schema(
    [
        ("security", pa.string()),
        ("symbol", pa.string()),
        ("alpaca_asset_id", pa.string()),
        ("name", pa.string()),
        ("exchange", pa.string()),
        ("security_type", pa.string()),
        ("type_assumed", pa.bool_()),
        ("source", pa.string()),
        ("first_session", pa.date32()),
        ("last_session", pa.date32()),
        ("active", pa.bool_()),
        ("bars", pa.int32()),
        # A later piece of a ticker (after a gap of more than `max_gap_sessions`): a relisting
        # or a reused ticker; its base count is unreliable (README §5.1).
        ("relisted", pa.bool_()),
        # The first public trading date when the history shows it (#126); the EDGAR stage
        # clears it when a periodic report predates it (an OTC uplisting).
        ("listed_at", pa.date32()),
        ("cik", pa.int64()),
        ("sic", pa.int32()),
    ]
)
CORRECTION_SCHEMA = pa.schema(
    [
        ("security", pa.string()),
        ("session_date", pa.date32()),
        ("field", pa.string()),
        ("original", pa.float64()),
        ("corrected", pa.float64()),
    ]
)
SECURITIES = "securities"
CORRECTIONS = "bar_corrections"


class BarSource(Protocol):
    def daily_bars(
        self,
        symbols: Iterable[str],
        start: date,
        end: date,
        *,
        adjustment: Adjustment = Adjustment.SPLIT,
    ) -> dict[str, list[DailyBar]]: ...


class StaleWorkError(RuntimeError):
    """The work folder holds a fetch for a different symbol list."""


# --- fetch -------------------------------------------------------------------------------


def chunks(symbols: Sequence[str], size: int) -> list[list[str]]:
    return [list(symbols[i : i + size]) for i in range(0, len(symbols), size)]


def _chunk_path(folder: Path, i: int) -> Path:
    return folder / f"chunk-{i:05d}.parquet"


def stage_table(
    adjusted: Mapping[str, Sequence[DailyBar]], raw: Mapping[str, Sequence[DailyBar]]
) -> pa.Table:
    raw_closes = {(s, b.session): b.close for s, bars in raw.items() for b in bars}
    rows = [
        {
            "symbol": symbol,
            "session_date": b.session,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "raw_close": raw_closes.get((symbol, b.session)),
        }
        for symbol, bars in sorted(adjusted.items())
        for b in bars
    ]
    return pa.Table.from_pylist(rows, STAGE_SCHEMA)


@dataclass
class FetchReport:
    chunks: int = 0
    fetched: int = 0
    resumed: int = 0


def fetch_bars(
    source: BarSource,
    symbols: Sequence[str],
    start: date,
    end: date,
    risk: RiskConfig,
    work: Path,
) -> FetchReport:
    cfg = risk.history
    folder = work / "bars"
    folder.mkdir(parents=True, exist_ok=True)
    plan = {"start": start.isoformat(), "chunk": cfg.bar_chunk_symbols, "symbols": list(symbols)}
    plan_path = folder / "plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise StaleWorkError(f"{folder} was fetched for another symbol list: delete it")
    else:
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
    parts = chunks(symbols, cfg.bar_chunk_symbols)
    report = FetchReport(chunks=len(parts))
    todo = [i for i in range(len(parts)) if not _chunk_path(folder, i).exists()]
    report.resumed = len(parts) - len(todo)

    def one(i: int) -> None:
        adjusted = source.daily_bars(parts[i], start, end, adjustment=Adjustment.SPLIT)
        raw = source.daily_bars(parts[i], start, end, adjustment=Adjustment.RAW)
        path = _chunk_path(folder, i)
        tmp = path.with_suffix(".tmp")
        pq.write_table(stage_table(adjusted, raw), tmp)
        tmp.replace(path)
        log.info("backfill_bars_chunk", chunk=i, of=len(parts))

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        for _ in pool.map(one, todo):
            report.fetched += 1
    return report


# --- build -------------------------------------------------------------------------------


@dataclass
class BuildReport:
    tickers: int = 0
    securities: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    active: int = 0
    type_assumed: int = 0
    relisted: int = 0
    bars: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    corrections: int = 0
    sessions_written: int = 0
    members: dict[str, int] = field(default_factory=dict)  # min / median / max per session
    # Dropped duplicate → the security kept (one company under two tickers).
    duplicates: dict[str, str] = field(default_factory=dict)
    duplicate_symbols: list[str] = field(default_factory=list)  # "OLD=NEW", the first 30

    def details(self) -> dict[str, object]:
        return {
            "tickers": self.tickers,
            "securities": self.securities,
            "by_source": self.by_source,
            "active": self.active,
            "type_assumed": self.type_assumed,
            "relisted": self.relisted,
            "bars": self.bars,
            "dropped": self.dropped,
            "corrections": self.corrections,
            "sessions_written": self.sessions_written,
            "members": self.members,
            "duplicates": len(self.duplicates),
            "duplicate_symbols": self.duplicate_symbols,
        }


@dataclass
class _Built:
    bars: list[dict[str, Any]]
    members: list[dict[str, Any]]
    securities: list[dict[str, Any]]
    corrections: list[dict[str, Any]]


def build_ticker(
    symbol: str,
    rows: Sequence[Mapping[str, Any]],
    assets: Sequence[KnownAsset],
    position: Mapping[date, int],
    first_session: date,
    trading_since: date,
    risk: RiskConfig,
    dropped: Counter[str],
) -> _Built:
    """One ticker's staged rows (oldest first) → its securities' bars, membership and rows."""
    u = risk.universe
    adjusted = [
        IngestBar(r["session_date"], r["open"], r["high"], r["low"], r["close"], r["volume"], 0.0)
        for r in rows
    ]
    raw = {r["session_date"]: r["raw_close"] for r in rows if r["raw_close"] is not None}
    prepared = prepare(
        adjusted,
        raw,
        position,
        max_gap=risk.history.max_gap_sessions,
        max_jump=risk.history.max_jump,
        outliers=risk.ingest.outliers,
    )
    dropped.update(prepared.dropped)
    pieces = prepared.pieces
    found = securities_for(
        symbol, [(p.first, p.last) for p in pieces], assets, trading_since=trading_since
    )
    out = _Built([], [], [], [])
    for n, (piece, sec) in enumerate(zip(pieces, found, strict=True)):
        out.bars += [
            {
                "session_date": b.session,
                "security": sec.key,
                "symbol": symbol,
                "alpaca_asset_id": sec.alpaca_asset_id,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "raw_close": b.raw_close,
            }
            for b in piece.bars
        ]
        out.corrections += [
            {
                "security": sec.key,
                "session_date": c.session,
                "field": c.field.value,
                "original": c.original,
                "corrected": c.corrected,
            }
            for c in piece.corrections
        ]
        if _universe_type(sec, risk):
            flags = universe_flags(
                [b.close for b in piece.bars],
                [b.raw_close for b in piece.bars],
                [float(b.volume) for b in piece.bars],
                min_price=u.min_price,
                min_dollar_volume=u.min_dollar_volume,
                dollar_volume_sessions=u.dollar_volume_sessions,
                min_history_sessions=u.min_history_sessions,
            )
            out.members += [
                {"session_date": b.session, "security": sec.key}
                for b, member in zip(piece.bars, flags, strict=True)
                if member
            ]
        out.securities.append(_security_row(sec, len(piece.bars), n > 0, first_session))
    return out


def _universe_type(sec: Security, risk: RiskConfig) -> bool:
    u = risk.universe
    return sec.security_type in u.security_types and (
        sec.exchange is None or sec.exchange in u.exchanges
    )


def _security_row(sec: Security, bars: int, relisted: bool, first_session: date) -> dict[str, Any]:
    return {
        "security": sec.key,
        "symbol": sec.symbol,
        "alpaca_asset_id": sec.alpaca_asset_id,
        "name": sec.name,
        "exchange": sec.exchange,
        "security_type": sec.security_type.value,
        "type_assumed": sec.type_assumed,
        "source": sec.source.value,
        "first_session": sec.first_session,
        "last_session": sec.last_session,
        "active": sec.active,
        "bars": bars,
        "relisted": relisted,
        # Trading from the history's first session: it listed earlier, date unknown.
        "listed_at": None if relisted or sec.first_session <= first_session else sec.first_session,
        "cik": None,
        "sic": None,
    }


def build_history(
    store: ObjectStore,
    work: Path,
    assets: Sequence[KnownAsset],
    sessions: Sequence[date],
    risk: RiskConfig,
) -> BuildReport:
    """Build from every staged chunk. `sessions`: the calendar from the history start through
    the last session fetched."""
    position = {d: i for i, d in enumerate(sessions)}
    trading_since = sessions[max(len(sessions) - 1 - risk.history.max_gap_sessions, 0)]
    by_symbol: dict[str, list[KnownAsset]] = {}
    for a in assets:
        by_symbol.setdefault(a.symbol, []).append(a)
    staged = sorted((work / "bars").glob("chunk-*.parquet"))
    built = work / "built"
    built.mkdir(parents=True, exist_ok=True)
    report = BuildReport()
    dropped: Counter[str] = Counter()
    securities: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for i, path in enumerate(staged):
        table = pq.read_table(path).sort_by(
            [("symbol", "ascending"), ("session_date", "ascending")]
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in table.to_pylist():
            grouped.setdefault(row["symbol"], []).append(row)
        bars: list[dict[str, Any]] = []
        members: list[dict[str, Any]] = []
        for symbol, rows in grouped.items():
            report.tickers += 1
            one = build_ticker(
                symbol,
                rows,
                by_symbol.get(symbol, []),
                position,
                sessions[0],
                trading_since,
                risk,
                dropped,
            )
            bars += one.bars
            members += one.members
            securities += one.securities
            corrections += one.corrections
        pq.write_table(pa.Table.from_pylist(bars, BAR_SCHEMA), built / f"bars-{i:05d}.parquet")
        pq.write_table(
            pa.Table.from_pylist(members, MEMBER_SCHEMA), built / f"members-{i:05d}.parquet"
        )
        report.bars += len(bars)
        log.info("backfill_build_chunk", chunk=i, of=len(staged), bars=len(bars))

    report.duplicates = find_duplicates(built, sessions, securities, risk.history.duplicate_share)
    symbol_of = {s["security"]: s["symbol"] for s in securities}
    report.duplicate_symbols = [
        f"{symbol_of[lose]}={symbol_of[keep]}"
        for lose, keep in list(report.duplicates.items())[:30]
    ]
    gone = set(report.duplicates)
    securities = [s for s in securities if s["security"] not in gone]
    corrections = [c for c in corrections if c["security"] not in gone]
    _write_sessions(store, built, sessions, report, gone)
    _write_members(store, built, sessions, report, gone)
    put_table(store, table_key(SECURITIES), pa.Table.from_pylist(securities, SECURITY_SCHEMA))
    put_table(store, table_key(CORRECTIONS), pa.Table.from_pylist(corrections, CORRECTION_SCHEMA))
    report.securities = len(securities)
    report.by_source = dict(Counter(s["source"] for s in securities))
    report.active = sum(s["active"] for s in securities)
    report.type_assumed = sum(s["type_assumed"] for s in securities)
    report.relisted = sum(s["relisted"] for s in securities)
    report.dropped = dict(dropped)
    report.corrections = len(corrections)
    return report


def retype_funds(store: ObjectStore, risk: RiskConfig, through: date) -> list[str]:
    """Apply `DELISTED_FUNDS` to a history built before the list: retype those assumed-type
    securities and take every security outside the universe's types out of the membership
    tables. Idempotent; returns the symbols retyped."""
    table = get_table(store, table_key(SECURITIES))
    if table is None:
        raise FileNotFoundError("no securities table in the history: run the build")
    rows = table.to_pylist()
    retyped = []
    for row in rows:
        kind = DELISTED_FUNDS.get(row["symbol"])
        if row["type_assumed"] and kind is not None and row["security_type"] != kind.value:
            row["security_type"] = kind.value
            retyped.append(row["symbol"])
    allowed = {str(t) for t in risk.universe.security_types}
    outside = sorted(r["security"] for r in rows if r["security_type"] not in allowed)
    if retyped:
        put_table(store, table_key(SECURITIES), pa.Table.from_pylist(rows, SECURITY_SCHEMA))
    for year in range(risk.history.start.year, through.year + 1):
        members = get_table(store, membership_key(year))
        if members is not None:
            kept = _without(members, outside)
            if kept.num_rows != members.num_rows:
                put_table(store, membership_key(year), kept)
    return sorted(retyped)


def _year_filter(year: int) -> Any:
    column = pc.field("session_date")
    return (column >= date(year, 1, 1)) & (column < date(year + 1, 1, 1))


def _years(sessions: Sequence[date]) -> range:
    return range(sessions[0].year, sessions[-1].year + 1)


def find_duplicates(
    built: Path, sessions: Sequence[date], securities: Sequence[Mapping[str, Any]], share: float
) -> dict[str, str]:
    """Securities whose bars repeat another's on at least `share` of their sessions: the old
    ticker of a renamed company keeps serving the new ticker's bars up to the rename (PKI and
    RVTY, TASR and AXON; found by the 1.6 smoke run). The one to keep: an active security, then
    one with an asset id, then the longer history. Returns dropped → kept."""
    files = sorted(str(p) for p in built.glob("bars-*.parquet"))
    if not files:
        return {}
    dataset = ds.dataset(files, format="parquet", schema=BAR_SCHEMA)
    keys = ["session_date", "open", "high", "low", "close", "volume"]
    shared: Counter[tuple[str, str]] = Counter()
    for year in _years(sessions):
        table = dataset.to_table(columns=[*keys, "security"], filter=_year_filter(year))
        groups = table.group_by(keys).aggregate([("security", "list")])
        for group in groups.column("security_list").to_pylist():
            if len(group) > 1:
                shared.update(itertools.combinations(sorted(set(group)), 2))
    info = {s["security"]: s for s in securities}

    def rank(key: str) -> tuple[bool, bool, int, str]:
        s = info[key]
        return (bool(s["active"]), s["alpaca_asset_id"] is not None, int(s["bars"]), key)

    dropped: dict[str, str] = {}
    for (a, b), n in sorted(shared.items(), key=lambda kv: (-kv[1], kv[0])):
        if n < share * min(info[a]["bars"], info[b]["bars"]):
            continue
        keep, lose = sorted((a, b), key=rank, reverse=True)
        while keep in dropped:  # a chain of renames: keep the last survivor
            keep = dropped[keep]
        if lose not in dropped and lose != keep:
            dropped[lose] = keep
    return dropped


def _write_sessions(
    store: ObjectStore,
    built: Path,
    sessions: Sequence[date],
    report: BuildReport,
    gone: Collection[str] = (),
) -> None:
    files = sorted(str(p) for p in built.glob("bars-*.parquet"))
    if not files:
        return
    dataset = ds.dataset(files, format="parquet", schema=BAR_SCHEMA)
    for year in _years(sessions):
        table = _without(dataset.to_table(filter=_year_filter(year)), gone).sort_by(
            [("session_date", "ascending"), ("symbol", "ascending")]
        )
        for session, part in _by_session(table):
            write_bar_table(store, session, part, merge=False)
            report.sessions_written += 1


def _write_members(
    store: ObjectStore,
    built: Path,
    sessions: Sequence[date],
    report: BuildReport,
    gone: Collection[str] = (),
) -> None:
    files = sorted(str(p) for p in built.glob("members-*.parquet"))
    counts: list[int] = []
    if files:
        dataset = ds.dataset(files, format="parquet", schema=MEMBER_SCHEMA)
        for year in _years(sessions):
            table = _without(dataset.to_table(filter=_year_filter(year)), gone).sort_by(
                [("session_date", "ascending"), ("security", "ascending")]
            )
            put_table(store, membership_key(year), table)
            counts += [part.num_rows for _, part in _by_session(table)]
    if counts:
        ordered = sorted(counts)
        report.members = {
            "min": ordered[0],
            "median": ordered[len(ordered) // 2],
            "max": ordered[-1],
        }


def _without(table: pa.Table, gone: Collection[str]) -> pa.Table:
    if not gone:
        return table
    return table.filter(pc.invert(pc.is_in(table["security"], pa.array(sorted(gone), pa.string()))))


def _by_session(table: pa.Table) -> list[tuple[date, pa.Table]]:
    """Slices of a table sorted by `session_date`, one per session."""
    if table.num_rows == 0:
        return []
    days = table["session_date"].to_pylist()
    out = []
    start = 0
    for i in range(1, len(days) + 1):
        if i == len(days) or days[i] != days[start]:
            out.append((days[start], table.slice(start, i - start)))
            start = i
    return out
