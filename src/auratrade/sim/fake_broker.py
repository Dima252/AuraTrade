"""The in-memory broker: the order layer's `Broker` and `PriceSource`, for tests (task 1.7).

It models what spike 0.3b found on Alpaca paper (README #113-116, #124):
- an OTO's stop leg takes the parent's time-in-force and quantity; neither can change later,
  only the stop price (`422 qty cannot be changed for advanced orders`)
- the leg stays `held` while the parent is partly filled, and goes live when it fills
- cancelling a held leg cancels the whole OTO
- a resting sell order reserves its shares (`403 insufficient qty available`)
- a plain buy while a sell stop rests is a potential wash trade (`403`); an OTO is accepted
- a sell stop at or above the market is rejected (`422 stop price must be less than current
  price`)
- on a closed market a replace leaves the old order `pending_replace`, still holding every
  share, until the open; a second replace is `422 order chain not fully replaced`; an order
  placed while closed is `accepted` and can be cancelled but not replaced
- client_order_ids are unique for the account's lifetime, even after a cancel
- on a split's ex-date resting sell stops are rejected, and the position isn't adjusted

What cancelling a partly filled parent does to its leg is untested on Alpaca (#115), so it is a
setting: the leg is cancelled, or goes live for the filled shares.

Fault injection: a call can fail before it is carried out (a 500), be carried out and then lose
its answer (a dropped connection), or kill the process after it returns (`ProcessKilled`, a
`BaseException`, so no `except Exception` in the order code swallows it). An order can be
deleted behind the order layer's back.

Orders fill by hand with `fill` (a test's market decides what fills, and at what price). Each
fill is also an entry in the account's activities, as Alpaca records it; `distribute` adds a cash
entry (task 5.3).
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from auratrade.execution.broker import (
    FILL,
    Account,
    Activity,
    AdvancedOrderChangeError,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerUnavailableError,
    ChainPendingError,
    DuplicateClientOrderIdError,
    InsufficientQtyError,
    LimitBuy,
    LimitSell,
    MarketSell,
    OrderClass,
    OrderNotFoundError,
    OrderRequest,
    OrderStatus,
    OrderType,
    OtoBuy,
    Side,
    StopSell,
    StopThroughMarketError,
    TimeInForce,
    WashTradeError,
)

# Open sell orders in these statuses hold their shares.
_RESERVING = frozenset(
    {
        OrderStatus.NEW,
        OrderStatus.PENDING_NEW,
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.PENDING_REPLACE,
        OrderStatus.PENDING_CANCEL,
    }
)
_REPLACEABLE = frozenset({OrderStatus.NEW, OrderStatus.HELD, OrderStatus.PARTIALLY_FILLED})
# Orders working in the market: what a bar can fill.
_WORKING = frozenset({OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED})

NY = ZoneInfo("America/New_York")

PartialCancelLeg = Literal["cancel", "activate"]
FaultKind = Literal["fail", "drop", "kill"]


def _level(price: float | None) -> float:
    """An order's limit or stop price, which every order of its type has."""
    if price is None:  # pragma: no cover - the broker refuses such orders on submit
        raise AssertionError("order without its price level")
    return price


class ProcessKilled(BaseException):
    """The process died right after a broker call returned (fault injection)."""


@dataclass
class _Fault:
    method: str
    kind: FaultKind
    skip: int
    error: BrokerError | None = None


@dataclass
class _Position:
    qty: int = 0
    avg: float = 0.0


@dataclass
class _Order:
    order: BrokerOrder
    leg_ids: list[str] = field(default_factory=list)


class FakeBroker:
    def __init__(
        self,
        *,
        cash: float = 5000.0,
        market_open: bool = True,
        partial_cancel_leg: PartialCancelLeg = "cancel",
        now: datetime | None = None,
    ) -> None:
        self.cash = cash
        self.market_open = market_open
        self.partial_cancel_leg: PartialCancelLeg = partial_cancel_leg
        self.now = now or datetime(2026, 10, 22, 13, 36, tzinfo=UTC)
        self.calls: list[str] = []
        self.on_submit: list[Callable[[BrokerOrder], None]] = []
        self.on_sleep: list[Callable[[float], None]] = []
        self._orders: dict[str, _Order] = {}
        self._client_ids: set[str] = set()
        self._positions: dict[str, _Position] = {}
        self._prices: dict[str, float | None] = {}
        self._faults: list[_Fault] = []
        self._ids = itertools.count(1)
        self._resting: set[str] = set()
        """Buys that jumped past their limit and now rest as limit orders."""
        self._activities: list[Activity] = []

    # --- test controls -----------------------------------------------------------------

    def set_price(self, symbol: str, price: float | None) -> None:
        self._prices[symbol] = price

    def fail_next(self, method: str, error: BrokerError | None = None, *, skip: int = 0) -> None:
        """The next call to `method` (after `skip` of them) raises before it is carried out."""
        error = error or BrokerUnavailableError("500 internal server error", status=500)
        self._faults.append(_Fault(method, "fail", skip, error))

    def drop_next(self, method: str, *, skip: int = 0) -> None:
        """The next call to `method` is carried out, then its answer is lost."""
        self._faults.append(_Fault(method, "drop", skip))

    def kill_after(self, method: str, *, skip: int = 0) -> None:
        """The process dies right after the next call to `method` returns."""
        self._faults.append(_Fault(method, "kill", skip))

    def delete(self, order_id: str) -> None:
        """The order disappears at the broker (cancelled behind the order layer's back)."""
        self._set(order_id, status=OrderStatus.CANCELED)

    def fill(self, order_id: str, qty: int | None = None, price: float | None = None) -> None:
        held = self._orders[order_id].order
        if not held.is_open or held.status is OrderStatus.HELD:
            raise AssertionError(f"order {order_id} can't fill in status {held.status}")
        qty = held.remaining_qty if qty is None else qty
        if not 0 < qty <= held.remaining_qty:
            raise AssertionError(f"fill of {qty} on {held.remaining_qty} remaining")
        price = price if price is not None else self._fill_price(held)
        filled = held.filled_qty + qty
        avg = ((held.filled_avg_price or 0.0) * held.filled_qty + price * qty) / filled
        status = OrderStatus.FILLED if filled == held.qty else OrderStatus.PARTIALLY_FILLED
        self._set(order_id, filled_qty=filled, filled_avg_price=avg, status=status)
        if status is OrderStatus.FILLED:
            self._set(order_id, filled_at=self.now)
        self._book(held.symbol, held.side, qty, price)
        self._activities.append(
            Activity(
                id=self._activity_id(),
                kind=FILL,
                day=self.now.astimezone(NY).date(),
                symbol=held.symbol,
                side=held.side,
                qty=float(qty),
                price=price,
                order_id=order_id,
            )
        )
        if held.order_class is OrderClass.OTO and held.side is Side.BUY and filled == held.qty:
            for leg_id in self._orders[order_id].leg_ids:
                if self._orders[leg_id].order.status is OrderStatus.HELD:
                    self._set(leg_id, status=self._resting_status())

    def distribute(self, symbol: str | None, amount: float, *, kind: str = "DIV") -> None:
        """A cash entry today: a distribution (a withholding or a fee when negative), or
        interest (``INT``, no symbol). The cash is credited."""
        self.cash += amount
        self._activities.append(
            Activity(
                id=self._activity_id(),
                kind=kind,
                day=self.now.astimezone(NY).date(),
                symbol=symbol,
                amount=amount,
            )
        )

    def open_market(self) -> None:
        self.market_open = True
        for entry in list(self._orders.values()):
            o = entry.order
            if o.status is OrderStatus.PENDING_REPLACE:
                self._set(o.id, status=OrderStatus.REPLACED)
            elif o.status is OrderStatus.ACCEPTED:
                self._set(o.id, status=OrderStatus.NEW)

    def close_market(self) -> None:
        """The close: open `day` orders expire (legs included); GTC orders rest."""
        self.market_open = False
        for entry in list(self._orders.values()):
            o = entry.order
            if o.is_open and o.time_in_force is TimeInForce.DAY:
                self._set(o.id, status=OrderStatus.EXPIRED)

    def split_ex_date(self, symbol: str) -> None:
        """04:00 ET on a split's ex-date: resting sell stops are rejected; the position stays."""
        for entry in list(self._orders.values()):
            o = entry.order
            if o.symbol == symbol and o.is_open and o.is_sell_stop:
                self._set(o.id, status=OrderStatus.REJECTED)

    def sleep(self, seconds: float) -> None:
        """The order layer's sleep in tests: time passes and the test's hooks run."""
        self.now += timedelta(seconds=seconds)
        for hook in list(self.on_sleep):
            hook(seconds)

    def all_orders(self) -> list[BrokerOrder]:
        return [entry.order for entry in self._orders.values()]

    def position(self, symbol: str) -> int:
        held = self._positions.get(symbol)
        return 0 if held is None else held.qty

    # --- Broker --------------------------------------------------------------------------

    def account(self) -> Account:
        return self._call("account", self._account)

    def is_market_open(self) -> bool:
        return self._call("is_market_open", lambda: self.market_open)

    def positions(self) -> list[BrokerPosition]:
        return self._call("positions", self._list_positions)

    def open_orders(self) -> list[BrokerOrder]:
        return self._call("open_orders", lambda: [o for o in self.all_orders() if o.is_open])

    def orders_since(self, after: datetime, symbols: Sequence[str]) -> list[BrokerOrder]:
        wanted = set(symbols)
        return self._call(
            "orders_since",
            lambda: [
                o
                for o in self.all_orders()
                if o.symbol in wanted and o.created_at is not None and o.created_at > after
            ],
        )

    def get_order(self, order_id: str) -> BrokerOrder:
        return self._call("get_order", lambda: self._get(order_id))

    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        def find() -> BrokerOrder | None:
            for o in self.all_orders():
                if o.client_order_id == client_order_id:
                    return o
            return None

        return self._call("order_by_client_id", find)

    def submit(self, request: OrderRequest) -> BrokerOrder:
        return self._call("submit", lambda: self._submit(request))

    def replace(
        self,
        order_id: str,
        *,
        qty: int | None = None,
        stop_price: float | None = None,
        limit_price: float | None = None,
    ) -> BrokerOrder:
        return self._call("replace", lambda: self._replace(order_id, qty, stop_price, limit_price))

    def cancel(self, order_id: str) -> None:
        self._call("cancel", lambda: self._cancel(order_id))

    # --- PriceSource ---------------------------------------------------------------------

    def activities(self) -> list[Activity]:
        return self._call("activities", lambda: list(self._activities))

    def prices(self, symbols: Iterable[str]) -> dict[str, float | None]:
        return {s: self._prices.get(s) for s in symbols}

    # --- internals -----------------------------------------------------------------------

    def _call[T](self, method: str, action: Callable[[], T]) -> T:
        self.calls.append(method)
        fault = self._take_fault(method)
        if fault is not None and fault.kind == "fail":
            raise fault.error or BrokerUnavailableError("500 internal server error", status=500)
        result = action()
        if fault is not None and fault.kind == "drop":
            raise BrokerUnavailableError("connection dropped after the request was sent")
        if fault is not None and fault.kind == "kill":
            raise ProcessKilled(method)
        return result

    def _take_fault(self, method: str) -> _Fault | None:
        for fault in self._faults:
            if fault.method != method:
                continue
            if fault.skip > 0:
                fault.skip -= 1
                return None
            self._faults.remove(fault)
            return fault
        return None

    def _account(self) -> Account:
        value = sum(
            p.qty * (self._prices.get(s) or p.avg) for s, p in self._positions.items() if p.qty
        )
        return Account(equity=self.cash + value, cash=self.cash, long_market_value=value)

    def _list_positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(s, p.qty, p.avg, self._prices.get(s))
            for s, p in sorted(self._positions.items())
            if p.qty != 0
        ]

    def _get(self, order_id: str) -> BrokerOrder:
        entry = self._orders.get(order_id)
        if entry is None:
            raise OrderNotFoundError(f"order not found: {order_id}", status=404)
        return entry.order

    def _set(self, order_id: str, **changes: object) -> None:
        entry = self._orders[order_id]
        entry.order = replace(entry.order, **changes)  # type: ignore[arg-type]

    def _activity_id(self) -> str:
        # Alpaca's ids start with the time, so they sort in the order things happened.
        return f"{self.now:%Y%m%d%H%M%S}{len(self._activities):06d}::{uuid.uuid4()}"

    def _new_id(self) -> str:
        return f"fake-{next(self._ids):04d}"

    def _resting_status(self) -> OrderStatus:
        return OrderStatus.NEW if self.market_open else OrderStatus.ACCEPTED

    def _fill_price(self, order: BrokerOrder) -> float:
        for candidate in (order.limit_price, order.stop_price, self._prices.get(order.symbol)):
            if candidate is not None:
                return candidate
        raise AssertionError(f"no price to fill {order.id} at")

    def _book(self, symbol: str, side: Side, qty: int, price: float) -> None:
        held = self._positions.setdefault(symbol, _Position())
        if side is Side.BUY:
            held.avg = (held.avg * held.qty + price * qty) / (held.qty + qty)
            held.qty += qty
            self.cash -= qty * price
        else:
            if qty > held.qty:
                raise AssertionError(f"sold {qty} {symbol} with {held.qty} held")
            held.qty -= qty
            self.cash += qty * price
            if held.qty == 0:
                held.avg = 0.0

    def _reserved(self, symbol: str, *, excluding: str | None = None) -> int:
        orders = [o for o in self.all_orders() if o.symbol == symbol]
        pending = {o.id for o in orders if o.status is OrderStatus.PENDING_REPLACE}
        return sum(
            o.remaining_qty
            for o in orders
            if o.side is Side.SELL
            and o.status in _RESERVING
            and o.replaces not in pending
            and o.id != excluding
        )

    def _check_available(self, symbol: str, qty: int, *, excluding: str | None = None) -> None:
        available = self.position(symbol) - self._reserved(symbol, excluding=excluding)
        if qty > available:
            raise InsufficientQtyError(
                f"insufficient qty available for order (requested: {qty}, "
                f"available: {max(available, 0)})",
                status=403,
            )

    def _check_stop(self, symbol: str, stop_price: float) -> None:
        price = self._prices.get(symbol)
        if price is not None and stop_price >= price:
            raise StopThroughMarketError("stop price must be less than current price", status=422)

    def _new_order(self, **fields: object) -> BrokerOrder:
        values: dict[str, object] = {
            "id": self._new_id(),
            "filled_qty": 0,
            "status": self._resting_status(),
            "created_at": self.now,
            **fields,
        }
        order = BrokerOrder(**values)  # type: ignore[arg-type]
        self._orders[order.id] = _Order(order)
        return order

    def _submit(self, request: OrderRequest) -> BrokerOrder:
        if request.client_order_id in self._client_ids:
            raise DuplicateClientOrderIdError("client_order_id must be unique", status=422)
        if request.qty <= 0:
            raise BrokerError("qty must be > 0", status=422)
        common = {"client_order_id": request.client_order_id, "symbol": request.symbol}
        match request:
            case OtoBuy():
                if request.stop_loss >= request.limit_price:
                    raise BrokerError(
                        "stop_loss.stop_price must be below the base price", status=422
                    )
                parent = self._new_order(
                    **common,
                    side=Side.BUY,
                    type=OrderType.LIMIT if request.trigger is None else OrderType.STOP_LIMIT,
                    time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.OTO,
                    qty=request.qty,
                    limit_price=request.limit_price,
                    stop_price=request.trigger,
                )
                leg = self._new_order(
                    client_order_id=str(uuid.uuid4()),
                    symbol=request.symbol,
                    side=Side.SELL,
                    type=OrderType.STOP,
                    time_in_force=parent.time_in_force,
                    order_class=OrderClass.OTO,
                    qty=request.qty,
                    stop_price=request.stop_loss,
                    status=OrderStatus.HELD,
                    parent_id=parent.id,
                )
                self._orders[parent.id].leg_ids.append(leg.id)
                self._client_ids.add(leg.client_order_id)
                order = parent
            case LimitBuy():
                if any(
                    o.symbol == request.symbol
                    and o.is_open
                    and o.is_sell_stop
                    and o.status is not OrderStatus.HELD
                    for o in self.all_orders()
                ):
                    raise WashTradeError(
                        "potential wash trade detected. use complex orders", status=403
                    )
                order = self._new_order(
                    **common,
                    side=Side.BUY,
                    type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.SIMPLE,
                    qty=request.qty,
                    limit_price=request.limit_price,
                )
            case StopSell():
                self._check_available(request.symbol, request.qty)
                self._check_stop(request.symbol, request.stop_price)
                order = self._new_order(
                    **common,
                    side=Side.SELL,
                    type=OrderType.STOP,
                    time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.SIMPLE,
                    qty=request.qty,
                    stop_price=request.stop_price,
                )
            case LimitSell() | MarketSell():
                self._check_available(request.symbol, request.qty)
                order = self._new_order(
                    **common,
                    side=Side.SELL,
                    type=OrderType.LIMIT if isinstance(request, LimitSell) else OrderType.MARKET,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.SIMPLE,
                    qty=request.qty,
                    limit_price=request.limit_price if isinstance(request, LimitSell) else None,
                )
        self._client_ids.add(request.client_order_id)
        for hook in list(self.on_submit):
            hook(order)
        return self._orders[order.id].order

    def _replace(
        self,
        order_id: str,
        qty: int | None,
        stop_price: float | None,
        limit_price: float | None,
    ) -> BrokerOrder:
        old = self._get(order_id)
        pending = {o.id for o in self.all_orders() if o.status is OrderStatus.PENDING_REPLACE}
        if old.status is OrderStatus.PENDING_REPLACE or old.replaces in pending:
            raise ChainPendingError("order chain not fully replaced", status=422)
        if old.status not in _REPLACEABLE:
            raise BrokerError(f"cannot replace order in {old.status.value} status", status=422)
        if old.is_leg and qty is not None and qty != old.qty:
            raise AdvancedOrderChangeError("qty cannot be changed for advanced orders", status=422)
        new_qty = old.qty if qty is None else qty
        if old.side is Side.SELL and old.status is not OrderStatus.HELD:
            self._check_available(old.symbol, new_qty - old.filled_qty, excluding=old.id)
        if old.is_sell_stop and stop_price is not None and old.status is not OrderStatus.HELD:
            self._check_stop(old.symbol, stop_price)
        new = replace(
            old,
            id=self._new_id(),
            client_order_id=str(uuid.uuid4()),
            qty=new_qty,
            stop_price=old.stop_price if stop_price is None else stop_price,
            limit_price=old.limit_price if limit_price is None else limit_price,
            replaces=old.id,
            created_at=self.now,
        )
        if old.status is OrderStatus.HELD:
            status = OrderStatus.HELD
        elif self.market_open:
            status = OrderStatus.NEW if old.filled_qty == 0 else OrderStatus.PARTIALLY_FILLED
        else:
            status = OrderStatus.ACCEPTED
        new = replace(new, status=status)
        self._orders[new.id] = _Order(new, list(self._orders[old.id].leg_ids))
        self._client_ids.add(new.client_order_id)
        closed = OrderStatus.REPLACED if self.market_open else OrderStatus.PENDING_REPLACE
        self._set(old.id, status=closed)
        for leg_id in self._orders[new.id].leg_ids:
            self._set(leg_id, parent_id=new.id)
        if old.parent_id is not None:
            legs = self._orders[old.parent_id].leg_ids
            legs[legs.index(old.id)] = new.id
        return new

    def _cancel(self, order_id: str) -> None:
        o = self._get(order_id)
        if not o.is_open:
            raise BrokerError(f'order is already in "{o.status.value}" state', status=422)
        if o.is_leg and o.status is OrderStatus.HELD and o.parent_id is not None:
            # A held leg takes its whole OTO with it (spike 0.3b).
            self._cancel_oto(o.parent_id)
            return
        if o.order_class is OrderClass.OTO and o.side is Side.BUY:
            self._cancel_oto(o.id)
            return
        self._set(o.id, status=OrderStatus.CANCELED)

    def _cancel_oto(self, parent_id: str) -> None:
        parent = self._get(parent_id)
        if parent.is_open:
            self._set(parent_id, status=OrderStatus.CANCELED)
        for leg_id in self._orders[parent_id].leg_ids:
            leg = self._orders[leg_id].order
            if not leg.is_open:
                continue
            partly_filled = leg.status is OrderStatus.HELD and parent.filled_qty > 0
            if partly_filled and self.partial_cancel_leg == "activate":
                self._set(leg_id, qty=parent.filled_qty, status=self._resting_status())
                continue
            if leg.status is OrderStatus.HELD or parent.filled_qty == 0:
                self._set(leg_id, status=OrderStatus.CANCELED)
