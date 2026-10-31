"""The Israeli tax model (README §5.7; #3; `docs/backtests/portfolio-preregistration.md` §3.8).

Pure: no I/O. The backtest, the twin and the live journal keep one `TaxBook` each.

- **Lots:** every buy opens a lot (units, cost in USD); a sale closes lots **FIFO**, splitting a
  lot pro rata, and its gain is the proceeds less the cost of the units sold.
- **The year's tax:** ``rate`` on the year's net realized gains less the loss carried in; a net
  loss is carried forward without limit (against gains only). ``rate`` on the year's income
  (dividends and interest), which losses don't offset.
- Nominal USD throughout: a simplification of Israel's real gains in shekels (README §17).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

# A sale of the whole holding may differ from the units held by float noise.
UNITS_TOLERANCE = 1e-9


@dataclass(slots=True)
class Lot:
    opened: date
    units: float
    cost: float
    """What these units cost, in USD."""
    ref: str | None = None
    """Where the lot came from (the live journal's order id); the backtest leaves it None."""


def fifo_gain(lots: Sequence[Lot], units: float, price: float) -> float:
    """The gain a sale of ``units`` at ``price`` would realize, closing ``lots`` FIFO (the
    lots aren't changed). Units beyond the lots have no known cost: all their proceeds count
    as gain, so an estimate is never low."""
    left, cost = units, 0.0
    for lot in lots:
        if left <= 0:
            break
        taken = min(left, lot.units)
        cost += lot.cost * taken / lot.units
        left -= taken
    return units * price - cost


@dataclass(frozen=True, slots=True)
class Sale:
    symbol: str
    day: date
    units: float
    proceeds: float
    cost: float
    closed: tuple[Lot, ...] = field(default=(), compare=False)
    """The lot pieces the sale closed, oldest first (their units and cost)."""

    @property
    def gain(self) -> float:
        return self.proceeds - self.cost


@dataclass(frozen=True, slots=True)
class YearTax:
    year: int
    realized: float
    """The year's gains less its losses."""
    carry_in: float
    carry_out: float
    """The loss carried into the next year (never negative)."""
    gain_tax: float
    income: float
    income_tax: float

    @property
    def total(self) -> float:
        return self.gain_tax + self.income_tax


class TaxBook:
    def __init__(self, rate: float, carry: float = 0.0) -> None:
        if not 0 <= rate <= 1 or carry < 0:
            raise ValueError("rate must be a fraction and the carried loss non-negative")
        self.rate = rate
        self.carry = carry
        self._lots: dict[str, deque[Lot]] = {}
        self._realized: dict[int, float] = {}
        self._income: dict[int, float] = {}
        self._closed: int | None = None

    def buy(
        self, symbol: str, day: date, units: float, cost: float, *, ref: str | None = None
    ) -> None:
        if not (units > 0 and cost >= 0 and math.isfinite(units) and math.isfinite(cost)):
            raise ValueError(f"buy of {symbol}: units must be positive and the cost finite")
        self._open(day.year)
        self._lots.setdefault(symbol, deque()).append(Lot(day, units, cost, ref))

    def sell(self, symbol: str, day: date, units: float, proceeds: float) -> Sale:
        if proceeds < 0:
            raise ValueError(f"sale of {units} {symbol} for {proceeds}")
        self._open(day.year)
        cost, closed = self._take(symbol, units)
        sale = Sale(symbol, day, units, proceeds, cost, tuple(closed))
        self._realized[day.year] = self._realized.get(day.year, 0.0) + sale.gain
        return sale

    def withdraw(self, symbol: str, units: float) -> float:
        """Remove ``units`` FIFO without realizing a gain: the backtest's tax payment, sold
        without the further tax the sale would owe (pre-registration §3.8). The cost removed."""
        return self._take(symbol, units)[0]

    def _take(self, symbol: str, units: float) -> tuple[float, list[Lot]]:
        """Close ``units`` FIFO, splitting the last lot pro rata; the cost of what was closed,
        and the pieces."""
        held = self.units(symbol)
        # Relative: on a holding of millions of units, float noise exceeds any fixed tolerance.
        tolerance = UNITS_TOLERANCE * max(1.0, held)
        if not units > 0 or units > held + tolerance:
            raise ValueError(f"sale of {units} {symbol} with {held} held")
        lots = self._lots[symbol]
        left, cost = min(units, held), 0.0
        closed: list[Lot] = []
        while left > tolerance and lots:
            lot = lots[0]
            if lot.units <= left + tolerance:
                cost += lot.cost
                left -= lot.units
                closed.append(lots.popleft())
            else:
                share = left / lot.units
                piece = Lot(lot.opened, left, lot.cost * share, lot.ref)
                cost += piece.cost
                lot.cost -= piece.cost
                lot.units -= left
                closed.append(piece)
                left = 0.0
        if not lots:
            del self._lots[symbol]
        return cost, closed

    def add_income(self, day: date, amount: float) -> None:
        if amount < 0:
            raise ValueError("income can't be negative")
        self._open(day.year)
        self._income[day.year] = self._income.get(day.year, 0.0) + amount

    def units(self, symbol: str) -> float:
        return math.fsum(lot.units for lot in self._lots.get(symbol, ()))

    def cost_basis(self, symbol: str) -> float:
        return math.fsum(lot.cost for lot in self._lots.get(symbol, ()))

    def lots(self, symbol: str) -> list[Lot]:
        return [Lot(lot.opened, lot.units, lot.cost, lot.ref) for lot in self._lots.get(symbol, ())]

    def symbols(self) -> list[str]:
        return sorted(self._lots)

    def realized(self, year: int) -> float:
        return self._realized.get(year, 0.0)

    def year_tax(self, year: int) -> YearTax:
        """The tax ``year`` would owe now, without closing it."""
        realized = self.realized(year)
        income = self._income.get(year, 0.0)
        net = realized - self.carry
        return YearTax(
            year,
            realized,
            self.carry,
            max(-net, 0.0),
            self.rate * max(net, 0.0),
            income,
            self.rate * income,
        )

    def tax_if_sold(
        self, year: int, values: Mapping[str, float], pending_income: float = 0.0
    ) -> float:
        """The tax owed if every lot were sold at ``values`` (each symbol's market value) now:
        the year's tax so far, the sale's and the pending income's. What "after the estimated
        tax" takes off equity (README §5.5). A closed year has paid its tax, so the sale falls
        in the next."""
        missing = sorted(set(self._lots) - values.keys())
        if missing or pending_income < 0:
            raise ValueError(f"no value for {missing}, or negative pending income")
        realized = income = 0.0
        if self._closed is None or year > self._closed:
            realized, income = self.realized(year), self._income.get(year, 0.0)
        unrealized = math.fsum(values[s] - self.cost_basis(s) for s in self._lots)
        net = realized + unrealized - self.carry
        return self.rate * (max(net, 0.0) + income + pending_income)

    def close_year(self, year: int) -> YearTax:
        """Settle ``year``: its tax, and the loss it carries into the next."""
        if self._closed is not None and year <= self._closed:
            raise ValueError(f"{year} is already closed")
        tax = self.year_tax(year)
        self.carry = tax.carry_out
        self._closed = year
        return tax

    def _open(self, year: int) -> None:
        if self._closed is not None and year <= self._closed:
            raise ValueError(f"{year} is already closed")
