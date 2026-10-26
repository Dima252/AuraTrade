"""Backfill stage: the portfolio backtest's long history
(`docs/backtests/portfolio-preregistration.md` §4): Kenneth French's daily market and T-bill
returns from 1926 and Shiller's monthly S&P dividends, as two tables in the Parquet history.
Free calls; re-fetched whole when the stage runs again (both files are revised in place). The
fetch time and the French file's CRSP note go into the market table's metadata, so the report
can name the release it scored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import pyarrow as pa

from auratrade.data.french import FrenchDaily
from auratrade.data.shiller import ShillerMonth
from auratrade.history.store import get_table, put_table, table_key
from auratrade.storage import ObjectStore

LONG_MARKET = "long_history_market"
LONG_DIVIDENDS = "long_history_dividends"
MARKET_SCHEMA = pa.schema(
    [("session_date", pa.date32()), ("market", pa.float64()), ("rf", pa.float64())]
)
DIVIDEND_SCHEMA = pa.schema(
    [("month", pa.date32()), ("price", pa.float64()), ("dividend", pa.float64())]
)


class FrenchSource(Protocol):
    def daily_factors(self) -> FrenchDaily: ...


class ShillerSource(Protocol):
    def monthly(self) -> list[ShillerMonth]: ...


@dataclass(frozen=True, slots=True)
class LongHistoryStored:
    sessions: int
    first_session: date
    last_session: date
    months: int
    last_dividend_month: date | None

    def details(self) -> dict[str, object]:
        return {
            "sessions": self.sessions,
            "first_session": self.first_session.isoformat(),
            "last_session": self.last_session.isoformat(),
            "months": self.months,
            "last_dividend_month": (
                self.last_dividend_month.isoformat() if self.last_dividend_month else None
            ),
        }


def long_history(
    store: ObjectStore, french: FrenchSource, shiller: ShillerSource, fetched_at: datetime
) -> LongHistoryStored:
    """Fetch both files and store them; what was stored, counted (no returns)."""
    daily = french.daily_factors()
    months = shiller.monthly()
    market = pa.Table.from_pylist(
        [{"session_date": d.day, "market": d.market, "rf": d.rf} for d in daily.days],
        MARKET_SCHEMA,
    ).replace_schema_metadata({"note": daily.note, "fetched_at": fetched_at.isoformat()})
    dividends = pa.Table.from_pylist(
        [{"month": m.month, "price": m.price, "dividend": m.dividend} for m in months],
        DIVIDEND_SCHEMA,
    )
    put_table(store, table_key(LONG_MARKET), market)
    put_table(store, table_key(LONG_DIVIDENDS), dividends)
    known = [m.month for m in months if m.dividend is not None]
    return LongHistoryStored(
        len(daily.days),
        daily.days[0].day,
        daily.days[-1].day,
        len(months),
        known[-1] if known else None,
    )


@dataclass(frozen=True, slots=True)
class LongHistory:
    note: str
    fetched_at: str
    days: list[date]
    market: list[float]
    """The market's total return per session, a fraction."""
    rf: list[float]
    """The T-bill return per session, a fraction."""
    months: list[date]
    prices: list[float]
    dividends: list[float | None]
    """Shiller's trailing 12-month dividend per month; None where not yet published."""


def load_long_history(store: ObjectStore) -> LongHistory:
    market = get_table(store, table_key(LONG_MARKET))
    dividends = get_table(store, table_key(LONG_DIVIDENDS))
    if market is None or dividends is None:
        raise FileNotFoundError(
            "no long history in the store: run `scripts/backfill.py longhistory`"
        )
    meta = market.schema.metadata or {}
    rows = market.sort_by("session_date").to_pylist()
    monthly = dividends.sort_by("month").to_pylist()
    return LongHistory(
        meta.get(b"note", b"").decode(),
        meta.get(b"fetched_at", b"").decode(),
        [r["session_date"] for r in rows],
        [float(r["market"]) for r in rows],
        [float(r["rf"]) for r in rows],
        [r["month"] for r in monthly],
        [float(r["price"]) for r in monthly],
        [None if r["dividend"] is None else float(r["dividend"]) for r in monthly],
    )
