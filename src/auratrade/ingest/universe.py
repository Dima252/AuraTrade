"""The `tickers` table and the tracked set (README §5.1, #111).

- **Refresh:** Alpaca's active, tradable assets on the listed exchanges, named from the Nasdaq
  Trader directory where it lists them and classified by `ingest.classify`. A security is keyed
  by its Alpaca asset id: a symbol change updates its row, and a reused symbol gets a new row
  once the old security is marked delisted. An asset that leaves the list is delisted the
  session after its last stored bar. More than `max_delistings` at once is treated as a vendor
  glitch: nothing is written and the job fails closed.
- **Tracked set:** bars are stored only for universe-type tickers on universe exchanges that
  pass `track_margin` times the universe's price and dollar-volume filters on raw prices, plus
  the regime's index ETFs. The universe filters themselves are applied when the bars are read,
  point in time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Connection, bindparam, func, insert, select, update

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import UniverseConfig
from auratrade.data.alpaca_data import Asset, DailyBar
from auratrade.data.nasdaq import Listing
from auratrade.db.models import DailyBar as DailyBarRow
from auratrade.db.models import Ticker
from auratrade.ingest.classify import SecurityType, classify, refreshed

# Alpaca's exchange codes for listed US securities; ETFs trade on ARCA and BATS.
LISTED_EXCHANGES = ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS")


class UniverseError(RuntimeError):
    """The asset list can't be trusted tonight (fail closed)."""


@dataclass(frozen=True, slots=True)
class ListedSecurity:
    asset_id: str
    symbol: str
    name: str
    exchange: str
    security_type: SecurityType


def listed_securities(assets: Iterable[Asset], listings: Iterable[Listing]) -> list[ListedSecurity]:
    """Active, tradable listed assets with their directory name and type."""
    directory = {x.symbol: x for x in listings if not x.is_test_issue}
    out = []
    for a in assets:
        if a.status != "active" or not a.tradable or a.exchange not in LISTED_EXCHANGES:
            continue
        listing = directory.get(a.symbol)
        name = listing.name if listing is not None else a.name
        kind = classify(a.symbol, name, is_etf=listing is not None and listing.is_etf)
        out.append(ListedSecurity(a.id, a.symbol, name, a.exchange, kind))
    return out


@dataclass(frozen=True)
class TickerRefresh:
    inserted: int
    updated: int
    delisted: tuple[str, ...]


def refresh_tickers(
    conn: Connection,
    securities: Sequence[ListedSecurity],
    calendar: MarketCalendar,
    session: date,
    *,
    now: datetime,
    max_delistings: int,
) -> TickerRefresh:
    """Bring `tickers` in line with tonight's asset list (module docstring)."""
    rows = conn.execute(
        select(
            Ticker.id,
            Ticker.symbol,
            Ticker.alpaca_asset_id,
            Ticker.name,
            Ticker.exchange,
            Ticker.security_type,
            Ticker.sic,
            Ticker.active,
        )
    ).all()
    by_asset = {r.alpaca_asset_id: r for r in rows if r.alpaca_asset_id is not None}
    # Rows without an asset id (hand-made or from the backfill) are adopted by symbol.
    orphans = {r.symbol: r for r in rows if r.alpaca_asset_id is None and r.active}
    listed = {s.asset_id for s in securities}
    gone = [r for r in rows if r.active and r.alpaca_asset_id and r.alpaca_asset_id not in listed]
    if len(gone) > max_delistings:
        raise UniverseError(
            f"{len(gone)} listed securities vanished from Alpaca's asset list "
            f"(limit {max_delistings}); nothing written"
        )

    to_delist = {r.id: r.symbol for r in gone}
    # An active row holding a symbol that now belongs to another security is delisted too.
    holders = {r.symbol: r for r in rows if r.active and r.alpaca_asset_id is not None}
    for s in securities:
        holder = holders.get(s.symbol)
        if (
            holder is not None
            and holder.alpaca_asset_id != s.asset_id
            and holder.alpaca_asset_id not in listed
        ):
            to_delist[holder.id] = holder.symbol
    last_bars = _last_bar_dates(conn, to_delist)
    delistings = [
        {
            "p_id": ticker_id,
            "p_delisted_at": (
                calendar.next_session(last_bars[ticker_id]).day
                if ticker_id in last_bars
                else session
            ),
        }
        for ticker_id in to_delist
    ]
    _update_many(conn, delistings, {"active": False, "updated_at": now})

    new_rows: list[dict[str, object]] = []
    changes: list[dict[str, object]] = []
    renamed: list[dict[str, object]] = []
    wanted_symbols = {s.symbol for s in securities}
    for s in securities:
        row = by_asset.get(s.asset_id) or orphans.get(s.symbol)
        if row is None:
            new_rows.append(
                {
                    "symbol": s.symbol,
                    "alpaca_asset_id": s.asset_id,
                    "name": s.name,
                    "exchange": s.exchange,
                    "security_type": s.security_type.value,
                    "active": True,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            continue
        values = {
            "symbol": s.symbol,
            "alpaca_asset_id": s.asset_id,
            "name": s.name,
            "exchange": s.exchange,
            "security_type": refreshed(s.security_type, row.security_type, row.sic).value,
            "active": True,
        }
        current = {k: getattr(row, k) for k in values}
        if values != current:
            changes.append({f"p_{k}": v for k, v in values.items()} | {"p_id": row.id})
            if row.symbol != s.symbol and row.symbol in wanted_symbols:
                # Two listed securities swapping symbols: park this one's old symbol first,
                # so the unique index on active symbols holds at every statement.
                renamed.append({"p_id": row.id, "p_symbol": f"{row.symbol}~{row.id}"})
    _update_many(conn, renamed, {})
    # A reactivated row loses its delisting date; an active one has none.
    _update_many(conn, changes, {"delisted_at": None, "updated_at": now})
    if new_rows:
        conn.execute(insert(Ticker), new_rows)
    return TickerRefresh(len(new_rows), len(changes), tuple(sorted(to_delist.values())))


def _update_many(
    conn: Connection, params: Sequence[dict[str, object]], fixed: Mapping[str, object]
) -> None:
    """One executemany UPDATE of `tickers` by id; `p_<column>` keys are the per-row values."""
    if not params:
        return
    columns = {k.removeprefix("p_") for k in params[0] if k != "p_id"}
    conn.execute(
        update(Ticker)
        .where(Ticker.id == bindparam("p_id"))
        .values({c: bindparam(f"p_{c}") for c in columns} | dict(fixed)),
        list(params),
    )


def _last_bar_dates(conn: Connection, ticker_ids: Iterable[int]) -> dict[int, date]:
    ids = list(ticker_ids)
    if not ids:
        return {}
    rows = conn.execute(
        select(DailyBarRow.ticker_id, func.max(DailyBarRow.session_date))
        .where(DailyBarRow.ticker_id.in_(ids))
        .group_by(DailyBarRow.ticker_id)
    ).all()
    return {row[0]: row[1] for row in rows}


# --- the tracked set ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    ticker_id: int
    symbol: str


def candidates(conn: Connection, universe: UniverseConfig) -> list[Candidate]:
    """Active universe-type tickers on universe exchanges."""
    rows = conn.execute(
        select(Ticker.id, Ticker.symbol)
        .where(
            Ticker.active,
            Ticker.security_type.in_(universe.security_types),
            Ticker.exchange.in_(universe.exchanges),
        )
        .order_by(Ticker.symbol)
    ).all()
    return [Candidate(r.id, r.symbol) for r in rows]


def active_ids(conn: Connection, symbols: Iterable[str]) -> dict[str, int]:
    wanted = list(symbols)
    rows = conn.execute(
        select(Ticker.symbol, Ticker.id).where(Ticker.active, Ticker.symbol.in_(wanted))
    ).all()
    return {row.symbol: row.id for row in rows}


@dataclass(frozen=True, slots=True)
class ScreenBar:
    """What the screen reads of a bar: the close as traded and the dollar volume."""

    raw_close: float
    dollar_volume: float


def screen_bars(raw_bars: Sequence[DailyBar]) -> list[ScreenBar]:
    """From raw (unadjusted) vendor bars."""
    return [ScreenBar(b.close, b.close * b.volume) for b in raw_bars]


def passes_screen(bars: Sequence[ScreenBar], universe: UniverseConfig, *, margin: float) -> bool:
    """Raw close and average dollar volume (over the last `dollar_volume_sessions` bars, or
    fewer for a new listing) at `margin` times the universe filters."""
    if not bars:
        return False
    recent = bars[-universe.dollar_volume_sessions :]
    dollar_volume = sum(b.dollar_volume for b in recent) / len(recent)
    return (
        bars[-1].raw_close >= universe.min_price * margin
        and dollar_volume >= universe.min_dollar_volume * margin
    )
