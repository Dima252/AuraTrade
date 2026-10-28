"""The live tax lots on the database (README §5.7, §10; task 5.3, #170).

`post_market` rebuilds `tax_lots` whole each evening from the broker's activities
(`tax.live.replay`); `allocate` on R and an event read the open lots and the realized gains back
for the Portfolio Manager's account facts on `paper`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict

from sqlalchemy import Connection, delete, extract, func, insert, select

from auratrade.db.models import TaxLot
from auratrade.tax.israel import Lot
from auratrade.tax.live import LiveTax


def write_lots(conn: Connection, live: LiveTax) -> int:
    conn.execute(delete(TaxLot))
    if live.rows:
        conn.execute(insert(TaxLot), [asdict(row) for row in live.rows])
    return len(live.rows)


def open_lots(conn: Connection) -> dict[str, list[Lot]]:
    rows = conn.execute(
        select(TaxLot.symbol, TaxLot.opened, TaxLot.units, TaxLot.cost, TaxLot.order_id)
        .where(TaxLot.kind == "LOT", TaxLot.closed.is_(None))
        .order_by(TaxLot.symbol, TaxLot.opened, TaxLot.id)
    )
    out: dict[str, list[Lot]] = defaultdict(list)
    for symbol, opened, units, cost, ref in rows:
        out[symbol].append(Lot(opened, units, cost, ref))
    return dict(out)


def realized_by_year(conn: Connection) -> dict[int, float]:
    """The net realized gain of each year with a sale."""
    year = extract("year", TaxLot.closed)
    rows = conn.execute(
        select(year, func.sum(TaxLot.realized_gain))
        .where(TaxLot.kind == "LOT", TaxLot.closed.is_not(None))
        .group_by(year)
    )
    return {int(y): float(total or 0.0) for y, total in rows}
