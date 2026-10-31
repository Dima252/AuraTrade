"""The live account's tax book from the broker's activity record (README §5.7, #35).

Pure: no I/O. `post_market` replays the whole record each evening (as `twin_book` is rewritten
whole), so the lots are never out of step with the broker:

- **Fills** in the funds open lots (each fill its own lot, tagged with its order) and close them
  FIFO through `TaxBook`; a fill outside the funds is not the portfolio's (a mismatch,
  README §5.6) and is skipped.
- **Distributions and interest** (``DIV*``, ``INT*``) received are income. Negative entries (a
  withholding, a fee on a distribution) are skipped: the model taxes the gross amount, and a
  foreign withholding is credited against it. Fees, deposits and journals aren't taxed.
- **Anything else in a fund** (a split, a merger, ...) is reported, not applied: the order layer
  handles a split by hand (README §5.6).
- **A sale beyond the known lots** realizes all its proceeds as gain on the units without a
  cost (as `fifo_gain`: an estimate is never low), and is reported.
- **Years** close when the record crosses into the next, and up to ``as_of``'s year.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

from auratrade.execution.broker import FILL, Activity, Side
from auratrade.tax.israel import UNITS_TOLERANCE, TaxBook

INCOME_KINDS = ("DIV", "INT")
"""Prefixes of the activity types that are income when positive."""
NOT_TAXED = frozenset({"FEE", "JNLC", "JNLS", "CSD", "CSW", "TRANS", "PTC", "PTR"})
CASH = "CASH"
"""The symbol of interest on cash, which names no fund."""


@dataclass(frozen=True, slots=True)
class LotRow:
    """One `tax_lots` row: an open lot, a closed piece of one, or income (README §10)."""

    kind: str
    """``LOT`` or ``INCOME``."""
    symbol: str
    opened: date
    units: float
    cost: float
    """USD; for income, the amount received."""
    closed: date | None = None
    proceeds: float | None = None
    realized_gain: float | None = None
    order_id: str | None = None


@dataclass
class LiveTax:
    book: TaxBook
    rows: list[LotRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_income(kind: str) -> bool:
    return kind.startswith(INCOME_KINDS)


def replay(
    activities: Iterable[Activity], funds: Iterable[str], rate: float, as_of: date
) -> LiveTax:
    """The account's tax book from its whole activity record, oldest first."""
    wanted = frozenset(funds)
    live = LiveTax(TaxBook(rate))
    year: int | None = None
    closed: list[LotRow] = []
    for a in sorted(activities, key=lambda a: (a.day, a.id)):
        if a.kind != FILL and not is_income(a.kind):
            if a.kind not in NOT_TAXED and a.symbol in wanted:
                live.warnings.append(f"{a.day} {a.kind} in {a.symbol}: not applied to the lots")
            continue
        # Interest names no fund; a fill always does.
        symbol = a.symbol if a.symbol is not None or a.kind == FILL else CASH
        if symbol is None or (symbol != CASH and symbol not in wanted):
            continue
        year = _close_through(live.book, year, a.day.year)
        if a.kind != FILL:
            if a.amount > 0:
                live.book.add_income(a.day, a.amount)
                live.rows.append(LotRow("INCOME", symbol, a.day, 0.0, a.amount))
            continue
        if a.side is Side.BUY:
            live.book.buy(symbol, a.day, a.qty, a.qty * a.price, ref=a.order_id)
            continue
        held = live.book.units(symbol)
        if a.qty > held + UNITS_TOLERANCE * max(1.0, held):
            unknown = a.qty - held
            live.warnings.append(
                f"{a.day} sale of {a.qty:g} {symbol} with {held:g} in the lots: "
                f"{unknown:g} counted at no cost"
            )
            live.book.buy(symbol, a.day, unknown, 0.0, ref=a.order_id)
        sale = live.book.sell(symbol, a.day, a.qty, a.qty * a.price)
        for piece in sale.closed:
            proceeds = sale.proceeds * piece.units / sale.units
            closed.append(
                LotRow(
                    "LOT",
                    symbol,
                    piece.opened,
                    piece.units,
                    piece.cost,
                    a.day,
                    proceeds,
                    proceeds - piece.cost,
                    piece.ref,
                )
            )
    _close_through(live.book, year, as_of.year)
    for symbol in live.book.symbols():
        for lot in live.book.lots(symbol):
            live.rows.append(
                LotRow("LOT", symbol, lot.opened, lot.units, lot.cost, order_id=lot.ref)
            )
    live.rows.extend(closed)
    return live


def _close_through(book: TaxBook, year: int | None, upto: int) -> int:
    """Close every year from ``year`` (the record's open year) before ``upto``."""
    if year is None:
        return upto
    while year < upto:
        book.close_year(year)
        year += 1
    return year


def disagreements(book: TaxBook, positions: Mapping[str, float], funds: Iterable[str]) -> list[str]:
    """Each fund whose units in the lots differ from the broker's position."""
    out = []
    for symbol in sorted(frozenset(funds) | frozenset(book.symbols())):
        lots, held = book.units(symbol), positions.get(symbol, 0.0)
        if not math.isclose(lots, held, abs_tol=UNITS_TOLERANCE * max(1.0, held)):
            out.append(f"{symbol}: {lots:g} units in the lots, {held:g} at the broker")
    return out


def values_for_tax(book: TaxBook, values: Mapping[str, float]) -> dict[str, float]:
    """The value of each symbol in the lots for `TaxBook.tax_if_sold`: the broker's market value,
    or the lots' cost (no gain) for one the broker doesn't hold (a disagreement, reported)."""
    return {s: values.get(s, book.cost_basis(s)) for s in book.symbols()}


def carried_loss(realized_by_year: Mapping[int, float], year: int) -> float:
    """The loss carried into ``year`` from the years before it (README §5.7: a net loss is
    carried forward without limit, against gains only)."""
    carry = 0.0
    for y in sorted(realized_by_year):
        if y < year:
            carry = max(carry - realized_by_year[y], 0.0)
    return carry
