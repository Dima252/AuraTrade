"""The Israeli tax model (README §5.7; #154; `docs/backtests/portfolio-preregistration.md` §3.8).

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
from dataclasses import dataclass
from datetime import date

# A sale of the whole holding may differ from the units held by float noise.
UNITS_TOLERANCE = 1e-9


@dataclass(slots=True)
class Lot:
    opened: date
    units: float
    cost: float
    """What these units cost, in USD."""


@dataclass(frozen=True, slots=True)
class Sale:
    symbol: str
    day: date
    units: float
    proceeds: float
    cost: float

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

    def buy(self, symbol: str, day: date, units: float, cost: float) -> None:
        if not (units > 0 and cost >= 0 and math.isfinite(units) and math.isfinite(cost)):
            raise ValueError(f"buy of {symbol}: units must be positive and the cost finite")
        self._open(day.year)
        self._lots.setdefault(symbol, deque()).append(Lot(day, units, cost))

    def sell(self, symbol: str, day: date, units: float, proceeds: float) -> Sale:
        held = self.units(symbol)
        if not (units > 0 and proceeds >= 0) or units > held + UNITS_TOLERANCE:
            raise ValueError(f"sale of {units} {symbol} with {held} held")
        self._open(day.year)
        lots = self._lots[symbol]
        left, cost = min(units, held), 0.0
        while left > UNITS_TOLERANCE:
            lot = lots[0]
            if lot.units <= left + UNITS_TOLERANCE:
                cost += lot.cost
                left -= lot.units
                lots.popleft()
            else:
                share = left / lot.units
                cost += lot.cost * share
                lot.cost -= lot.cost * share
                lot.units -= left
                left = 0.0
        if not lots:
            del self._lots[symbol]
        sale = Sale(symbol, day, units, proceeds, cost)
        self._realized[day.year] = self._realized.get(day.year, 0.0) + sale.gain
        return sale

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
        return [Lot(lot.opened, lot.units, lot.cost) for lot in self._lots.get(symbol, ())]

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
