"""The stop invariant (README §3 rule 5, §5.7.1), checked from broker state alone (pure).

Every position's live GTC sell stops together cover its full quantity, each at or above the
plan's hard-stop level, and a partly filled buy counts as unprotected (#115). `stop_guard` checks
it without plan levels (existence, quantity, GTC); the order jobs check it with them.

What counts as a live stop:
- a sell stop that will trigger or holds the shares (`LIVE_STOP_STATUSES`); a `held` leg won't
  trigger, its parent isn't completely filled
- not the replacement of an order still `pending_replace`: on a closed market the old order
  keeps every share until the replace goes through (spike 0.3b), so counting both would read
  as over-coverage
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from auratrade.execution.broker import (
    LIVE_STOP_STATUSES,
    BrokerOrder,
    BrokerPosition,
    OrderStatus,
    Side,
    TimeInForce,
)

# Stored levels are floats; a stop within this of its level is at it.
LEVEL_TOLERANCE = 1e-6


class Problem(StrEnum):
    PARTIAL_ENTRY = "PARTIAL_ENTRY"
    """A buy is partly filled: its leg stays held, so the filled shares have no live stop."""
    UNCOVERED = "UNCOVERED"
    """The valid stops cover fewer shares than the position."""
    OVERCOVERED = "OVERCOVERED"
    """The live stops are for more shares than the position."""
    NOT_GTC = "NOT_GTC"
    """A live stop that expires at the close."""
    BELOW_PLAN = "BELOW_PLAN"
    """A live stop under the plan's hard-stop level."""
    STRAY_SELL = "STRAY_SELL"
    """A working sale outside an order section: a job died between its sell and its restore."""
    ORPHAN_STOP = "ORPHAN_STOP"
    """A live sell stop in a symbol with no position."""


@dataclass(frozen=True, slots=True)
class Protection:
    symbol: str
    qty: int
    """The broker's position."""
    stops: tuple[BrokerOrder, ...]
    """The live sell stops."""
    covered: int
    """Shares under valid stops: live, GTC and at or above the level."""
    problems: frozenset[Problem]

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def missing(self) -> int:
        return max(self.qty - self.covered, 0)


def live_stops(orders: Iterable[BrokerOrder]) -> list[BrokerOrder]:
    orders = list(orders)
    pending = {o.id for o in orders if o.status is OrderStatus.PENDING_REPLACE}
    return [
        o
        for o in orders
        if o.is_sell_stop and o.status in LIVE_STOP_STATUSES and o.replaces not in pending
    ]


def valid_stop(order: BrokerOrder, level: float | None) -> bool:
    if order.time_in_force is not TimeInForce.GTC:
        return False
    return level is None or (
        order.stop_price is not None and order.stop_price >= level - LEVEL_TOLERANCE
    )


def check(
    positions: Iterable[BrokerPosition],
    orders: Iterable[BrokerOrder],
    levels: Mapping[str, float] | None = None,
) -> dict[str, Protection]:
    """One `Protection` per symbol with a position or a live sell stop.

    `levels` maps a symbol to its plan's hard stop; without it (`stop_guard`) a stop's level
    isn't checked, and a symbol missing from it isn't either."""
    orders = list(orders)
    by_symbol: dict[str, list[BrokerOrder]] = defaultdict(list)
    for order in orders:
        by_symbol[order.symbol].append(order)
    held = {p.symbol: p.qty for p in positions if p.qty != 0}

    out: dict[str, Protection] = {}
    for symbol in sorted(set(held) | {o.symbol for o in live_stops(orders)}):
        qty = held.get(symbol, 0)
        symbol_orders = by_symbol.get(symbol, [])
        stops = live_stops(symbol_orders)
        level = None if levels is None else levels.get(symbol)
        problems: set[Problem] = set()
        if qty <= 0:
            out[symbol] = Protection(symbol, qty, tuple(stops), 0, frozenset({Problem.ORPHAN_STOP}))
            continue
        if any(o.side is Side.BUY and o.is_open and o.filled_qty > 0 for o in symbol_orders):
            problems.add(Problem.PARTIAL_ENTRY)
        if any(o.side is Side.SELL and o.is_open and not o.is_sell_stop for o in symbol_orders):
            problems.add(Problem.STRAY_SELL)
        if any(o.time_in_force is not TimeInForce.GTC for o in stops):
            problems.add(Problem.NOT_GTC)
        if level is not None and any(
            o.stop_price is None or o.stop_price < level - LEVEL_TOLERANCE for o in stops
        ):
            problems.add(Problem.BELOW_PLAN)
        covered = sum(o.remaining_qty for o in stops if valid_stop(o, level))
        if covered < qty:
            problems.add(Problem.UNCOVERED)
        if sum(o.remaining_qty for o in stops) > qty:
            problems.add(Problem.OVERCOVERED)
        out[symbol] = Protection(symbol, qty, tuple(stops), covered, frozenset(problems))
    return out


def unprotected(protections: Mapping[str, Protection]) -> list[Protection]:
    return [p for p in protections.values() if not p.ok]
