"""Order sequences (README §5.7): protect a position, the sell sequence, and selling at once
when the price is already through the stop. Every method runs under the single-writer lock
(`execution.lock`), so no other job changes orders meanwhile.

The desk reads the broker before each step rather than trusting what it did a moment ago: a
job retried by Lambda, a dropped connection or a fill in between all leave the broker as the
only truth.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

import structlog
from structlog.typing import FilteringBoundLogger

from auratrade.config import ExecutionConfig
from auratrade.execution import ids
from auratrade.execution.broker import (
    Broker,
    BrokerError,
    BrokerOrder,
    BrokerUnavailableError,
    DuplicateClientOrderIdError,
    LimitBuy,
    LimitSell,
    MarketSell,
    OrderNotFoundError,
    OrderRequest,
    OrderStatus,
    OtoBuy,
    PriceSource,
    Side,
    StopSell,
    StopThroughMarketError,
    TimeInForce,
)
from auratrade.execution.invariant import LEVEL_TOLERANCE, live_stops
from auratrade.execution.prices import marketable_sell_limit

log = structlog.get_logger(__name__)


class SaleStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLED = "UNFILLED"
    """The sale didn't fill; the stop rests again and the sale is owed (`exit_pending`)."""
    DEFERRED = "DEFERRED"
    """Not attempted: the market is closed, or a split makes the price unreliable."""
    NOTHING = "NOTHING"


@dataclass(frozen=True, slots=True)
class SaleResult:
    symbol: str
    requested: int
    sold: int
    status: SaleStatus

    @property
    def owed(self) -> int:
        return self.requested - self.sold


@dataclass(frozen=True, slots=True)
class BuyResult:
    symbol: str
    requested: int
    bought: int
    price: float | None = None
    """The average fill price."""
    failed: str | None = None

    @property
    def owed(self) -> int:
        return self.requested - self.bought


@dataclass(frozen=True, slots=True)
class ProtectResult:
    symbol: str
    changed: bool
    sale: SaleResult | None = None
    """The price was through the level: the uncovered shares were sold, or that is owed."""
    failed: str | None = None


@dataclass
class OrderDesk:
    broker: Broker
    prices: PriceSource
    cfg: ExecutionConfig
    session: date
    market_open: bool
    sleep: Callable[[float], None]
    alert: Callable[[str], None]
    blocked_sales: set[str] = field(default_factory=set)
    """Symbols whose price can't be trusted (a split not yet applied, #116): no sale at once."""
    log: FilteringBoundLogger = field(default_factory=lambda: log)
    actions: list[dict[str, Any]] = field(default_factory=list)

    # --- reads ---------------------------------------------------------------------------

    def state(self, symbol: str) -> tuple[int, list[BrokerOrder]]:
        """The broker's position and open orders in `symbol`."""
        qty = next((p.qty for p in self.broker.positions() if p.symbol == symbol), 0)
        return qty, [o for o in self.broker.open_orders() if o.symbol == symbol]

    def price(self, symbol: str) -> float | None:
        return self.prices.prices([symbol]).get(symbol)

    def _record(self, action: str, symbol: str, **details: object) -> None:
        entry = {"action": action, "symbol": symbol, **details}
        self.actions.append(entry)
        self.log.info("order_action", **entry)

    # --- calls that survive a lost answer --------------------------------------------------

    def submit_once(self, request: OrderRequest) -> BrokerOrder:
        """Submit, retrying an unanswered call with the same client_order_id; a duplicate id
        means an earlier attempt (or an earlier run) got through, so that order is returned."""
        last: BrokerError | None = None
        for _ in range(self.cfg.call_attempts):
            try:
                return self.broker.submit(request)
            except DuplicateClientOrderIdError:
                found = self.broker.order_by_client_id(request.client_order_id)
                if found is None:
                    raise
                return found
            except BrokerUnavailableError as exc:
                last = exc
                found = self._lookup(request.client_order_id)
                if found is not None:
                    return found
        raise last or BrokerUnavailableError("no attempts made")

    def _lookup(self, client_order_id: str) -> BrokerOrder | None:
        try:
            return self.broker.order_by_client_id(client_order_id)
        except BrokerUnavailableError:
            return None

    def replace_once(self, order: BrokerOrder, **changes: Any) -> BrokerOrder:
        last: BrokerError | None = None
        for _ in range(self.cfg.call_attempts):
            try:
                return self.broker.replace(order.id, **changes)
            except BrokerUnavailableError as exc:
                last = exc
                done = self._replacement(order)
                if done is not None:
                    return done
        raise last or BrokerUnavailableError("no attempts made")

    def _replacement(self, order: BrokerOrder) -> BrokerOrder | None:
        try:
            return next((o for o in self.broker.open_orders() if o.replaces == order.id), None)
        except BrokerUnavailableError:
            return None

    def cancel_once(self, order: BrokerOrder) -> None:
        """Cancel; an order that is already closed (filled, cancelled) counts as done."""
        last: BrokerError | None = None
        for _ in range(self.cfg.call_attempts):
            try:
                self.broker.cancel(order.id)
                return
            except BrokerUnavailableError as exc:
                last = exc
            except OrderNotFoundError:
                return
            except BrokerError:
                if not self.broker.get_order(order.id).is_open:
                    return
                raise
            try:
                if not self.broker.get_order(order.id).is_open:
                    return
            except BrokerUnavailableError:
                pass
        raise last or BrokerUnavailableError("no attempts made")

    def wait_fill(self, order: BrokerOrder, seconds: float) -> BrokerOrder:
        """Poll until the order is filled or closed, or `seconds` have passed."""
        waited = 0.0
        current = order
        while current.status is not OrderStatus.FILLED and current.is_open and waited < seconds:
            self.sleep(self.cfg.fill_poll_seconds)
            waited += self.cfg.fill_poll_seconds
            current = self.broker.get_order(order.id)
        return current

    # --- protect -------------------------------------------------------------------------

    def protect(self, symbol: str, level: float) -> ProtectResult:
        """Make the position's live GTC stops cover its full quantity at `level` or above
        (README §5.7.1, #115): cancel the unfilled rest of a partly filled buy and any stray
        sale, fix or cancel stops that are below the level or not GTC, then place a standalone
        GTC stop for whatever is uncovered. If the price is already through the level, the
        uncovered shares are sold at once instead (step 2)."""
        changed = False
        qty, orders = self.state(symbol)
        stray = [
            o
            for o in orders
            if o.is_open
            and (
                (o.side is Side.BUY and o.filled_qty > 0)
                or (o.side is Side.SELL and not o.is_sell_stop)
            )
        ]
        for order in stray:
            self.cancel_once(order)
            self._record("cancel", symbol, order_id=order.id, why="partial entry or stray sale")
            changed = True
        if stray:
            qty, orders = self.state(symbol)

        stops = live_stops(orders)
        if qty <= 0:
            for stop in stops:
                self.cancel_once(stop)
                self._record("cancel", symbol, order_id=stop.id, why="no position")
            return ProtectResult(symbol, changed or bool(stops))

        fixed = False
        for stop in stops:
            if stop.time_in_force is not TimeInForce.GTC:
                self.cancel_once(stop)
                self._record("cancel", symbol, order_id=stop.id, why="not GTC")
                fixed = True
            elif stop.stop_price is None or stop.stop_price < level - LEVEL_TOLERANCE:
                try:
                    self.replace_once(stop, stop_price=level)
                except StopThroughMarketError:
                    return self._through(symbol, level, changed=True)
                self._record("raise_stop", symbol, order_id=stop.id, stop=level)
                fixed = True
        if fixed:
            changed = True
            qty, orders = self.state(symbol)
            stops = live_stops(orders)

        if sum(s.remaining_qty for s in stops) > qty:
            for stop in stops:
                self.cancel_once(stop)
            self._record("cancel_stops", symbol, why="over-covered")
            changed = True
            qty, orders = self.state(symbol)
            stops = live_stops(orders)

        missing = qty - sum(s.remaining_qty for s in stops)
        if missing <= 0:
            return ProtectResult(symbol, changed)
        price = self.price(symbol)
        if price is not None and price <= level:
            return self._through(symbol, level, changed=True, qty=missing)
        try:
            stop = self.submit_once(
                StopSell(symbol, missing, level, ids.stop_id(self.session, symbol))
            )
        except StopThroughMarketError:
            return self._through(symbol, level, changed=True, qty=missing)
        except BrokerError as exc:
            self.alert(f"could not place a stop for {missing} {symbol} at {level}: {exc}")
            return ProtectResult(symbol, changed, failed=str(exc))
        self._record("place_stop", symbol, order_id=stop.id, qty=missing, stop=level)
        return ProtectResult(symbol, True)

    def _through(
        self, symbol: str, level: float, *, changed: bool, qty: int | None = None
    ) -> ProtectResult:
        """The price is at or below the level: what the stop would have done is done now."""
        held, orders = self.state(symbol)
        if qty is None:
            # A stop couldn't be raised to the level: none of the shares is protected there.
            for stop in live_stops(orders):
                self.cancel_once(stop)
            qty = held
        sale = self.sell_now(symbol, qty, "STOP")
        return ProtectResult(symbol, changed, sale=sale)

    # --- sales ---------------------------------------------------------------------------

    def sell(self, symbol: str, qty: int, reason: str, level: float) -> SaleResult:
        """The sell sequence (README §5.7.2): reduce the stop (swapping legs for one standalone
        stop, #113), sell with a marketable limit, and restore the stop at once if the sale is
        rejected or doesn't fill. A full exit cancels the stops instead of reducing them."""
        if not self.market_open or symbol in self.blocked_sales:
            return SaleResult(symbol, qty, 0, SaleStatus.DEFERRED)
        held, orders = self.state(symbol)
        qty = min(qty, held)
        if qty <= 0:
            return SaleResult(symbol, 0, 0, SaleStatus.NOTHING)
        price = self.price(symbol)
        if price is None:
            self.alert(f"no price for {symbol}: its {reason} sale of {qty} waits")
            return SaleResult(symbol, qty, 0, SaleStatus.UNFILLED)

        try:
            self._free(symbol, held - qty, level, orders)
        except StopThroughMarketError:
            # The stops are cancelled and the price is through the level for the shares kept
            # too: sell them all now, as their stop would have.
            return self.sell_now(symbol, held, "STOP")
        try:
            order = self.submit_once(
                LimitSell(
                    symbol,
                    qty,
                    marketable_sell_limit(price, self.cfg.marketable_limit_slippage),
                    ids.exit_id(self.session, symbol, reason),
                )
            )
        except BrokerError as exc:
            self.alert(f"{reason} sale of {qty} {symbol} rejected: {exc}; restoring the stop")
            self.protect(symbol, level)
            return SaleResult(symbol, qty, 0, SaleStatus.UNFILLED)
        self._record("sell", symbol, order_id=order.id, qty=qty, reason=reason)
        order = self.wait_fill(order, self.cfg.sell_wait_seconds)
        if order.is_open:
            self.cancel_once(order)
            order = self.broker.get_order(order.id)
        restored = self.protect(symbol, level)
        sold = order.filled_qty + (restored.sale.sold if restored.sale is not None else 0)
        return _result(symbol, qty, sold)

    def _free(self, symbol: str, keep: int, level: float, orders: list[BrokerOrder]) -> None:
        """Leave stops on exactly `keep` shares, so the rest are free to sell."""
        stops = live_stops(orders)
        if keep > 0 and len(stops) == 1 and not stops[0].is_leg:
            self.replace_once(stops[0], qty=keep)
            self._record("reduce_stop", symbol, order_id=stops[0].id, qty=keep)
            return
        for stop in stops:
            self.cancel_once(stop)
        self._record("cancel_stops", symbol, count=len(stops), keep=keep)
        if keep > 0:
            # The legs' quantity is fixed: swap them for one standalone stop (#113).
            request = StopSell(symbol, keep, level, ids.stop_id(self.session, symbol))
            placed = self.submit_once(request)
            self._record("place_stop", symbol, order_id=placed.id, qty=keep, stop=level)

    def sell_now(self, symbol: str, qty: int, reason: str) -> SaleResult:
        """Sell shares that have no stop at once, escalating within the run: a marketable
        limit, one re-price, then a market order (README §5.7.2). Before the open or after the
        close nothing is sold: the 09:31 check run sells (#124)."""
        if qty <= 0:
            return SaleResult(symbol, 0, 0, SaleStatus.NOTHING)
        if not self.market_open or symbol in self.blocked_sales:
            self.alert(f"{symbol} is through its stop level; {qty} shares owed a sale")
            return SaleResult(symbol, qty, 0, SaleStatus.DEFERRED)
        if reason == "STOP":
            # What the stop would have done, done now: as loud as a stop fill (README §5.9).
            self.alert(f"{symbol} is through its stop level: selling {qty} at once")
        sold = 0
        order: BrokerOrder | None = None
        price = self.price(symbol)
        if price is not None:
            limit = marketable_sell_limit(price, self.cfg.marketable_limit_slippage)
            order = self.submit_once(
                LimitSell(symbol, qty, limit, ids.exit_id(self.session, symbol, reason))
            )
            self._record("sell_now", symbol, order_id=order.id, qty=qty, limit=limit)
            order = self.wait_fill(order, self.cfg.sell_wait_seconds)
            repriced = self.price(symbol)
            if order.is_open and repriced is not None:
                limit = marketable_sell_limit(repriced, self.cfg.marketable_limit_slippage)
                try:
                    order = self.replace_once(order, limit_price=limit)
                except BrokerError:
                    order = self.broker.get_order(order.id)
                else:
                    self._record("reprice", symbol, order_id=order.id, limit=limit)
                    order = self.wait_fill(order, self.cfg.sell_wait_seconds)
            sold = self._settle(order)
        if sold < qty:
            market = self.submit_once(
                MarketSell(symbol, qty - sold, ids.exit_id(self.session, symbol, reason))
            )
            self._record("sell_market", symbol, order_id=market.id, qty=qty - sold)
            market = self.wait_fill(market, self.cfg.market_order_wait_seconds)
            sold += self._settle(market)
        result = _result(symbol, qty, sold)
        if result.owed:
            self.alert(f"{symbol}: sold {sold} of {qty} through the stop level; the rest is owed")
        return result

    def sell_plain(self, symbol: str, qty: int, reason: str) -> SaleResult:
        """The sale of a fund that carries no stop (README §5.6.2, #155): a marketable limit;
        what hasn't filled after the wait is cancelled and owed."""
        if not self.market_open or symbol in self.blocked_sales:
            return SaleResult(symbol, qty, 0, SaleStatus.DEFERRED)
        held, _ = self.state(symbol)
        qty = min(qty, held)
        if qty <= 0:
            return SaleResult(symbol, 0, 0, SaleStatus.NOTHING)
        price = self.price(symbol)
        if price is None:
            self.alert(f"no price for {symbol}: its {reason} sale of {qty} waits")
            return SaleResult(symbol, qty, 0, SaleStatus.UNFILLED)
        limit = marketable_sell_limit(price, self.cfg.marketable_limit_slippage)
        try:
            order = self.submit_once(
                LimitSell(symbol, qty, limit, ids.exit_id(self.session, symbol, reason))
            )
        except BrokerError as exc:
            self.alert(f"{reason} sale of {qty} {symbol} rejected: {exc}")
            return SaleResult(symbol, qty, 0, SaleStatus.UNFILLED)
        self._record("sell", symbol, order_id=order.id, qty=qty, limit=limit, reason=reason)
        sold = self._settle(self.wait_fill(order, self.cfg.sell_wait_seconds))
        return _result(symbol, qty, sold)

    def buy(self, symbol: str, qty: int, limit: float, *, stop: float | None) -> BuyResult:
        """A marketable limit buy (README §5.6.3); never a market buy. With `stop` it is an OTO
        whose GTC stop leg rests at that level (the leveraged fund: Alpaca refuses a plain buy
        while a sell stop rests, #114). What hasn't filled after the wait is cancelled, and a
        stopped fund is then protected at `stop` for what did fill (a partly filled OTO's leg
        stays held, #115)."""
        if not self.market_open:
            return BuyResult(symbol, qty, 0, failed="market closed")
        client_order_id = ids.buy_id(self.session, symbol)
        request: OrderRequest = (
            LimitBuy(symbol, qty, limit, client_order_id)
            if stop is None
            else OtoBuy(symbol, qty, limit, stop, client_order_id)
        )
        try:
            order = self.submit_once(request)
        except BrokerError as exc:
            # An unanswered one may rest at the broker: the next run cancels it before it
            # decides again.
            self.alert(f"buy of {qty} {symbol} at {limit} failed: {exc}")
            if stop is not None:
                self.protect(symbol, stop)
            return BuyResult(symbol, qty, 0, failed=str(exc))
        self._record("buy", symbol, order_id=order.id, qty=qty, limit=limit, stop=stop)
        order = self.wait_fill(order, self.cfg.buy_wait_seconds)
        if order.is_open:
            self.cancel_once(order)
            self._record("cancel_buy", symbol, order_id=order.id, filled=order.filled_qty)
            order = self.broker.get_order(order.id)
        if stop is not None:
            self.protect(symbol, stop)
        return BuyResult(symbol, qty, order.filled_qty, order.filled_avg_price)

    def _settle(self, order: BrokerOrder | None) -> int:
        """Cancel what is left of a sale; its filled quantity."""
        if order is None:
            return 0
        if order.is_open:
            self.cancel_once(order)
            order = self.broker.get_order(order.id)
        return order.filled_qty

    def cancel_entries(self, symbols: Iterable[str] | None = None, *, why: str) -> int:
        """Cancel open entry parents that haven't filled at all (their held legs go with them).
        A partly filled one is left to `protect`, which keeps its filled shares covered."""
        wanted = None if symbols is None else set(symbols)
        count = 0
        for order in self.broker.open_orders():
            if (
                order.side is Side.BUY
                and order.filled_qty == 0
                and (wanted is None or order.symbol in wanted)
            ):
                self.cancel_once(order)
                self._record("cancel_entry", order.symbol, order_id=order.id, why=why)
                count += 1
        return count


def _result(symbol: str, qty: int, sold: int) -> SaleResult:
    if sold >= qty:
        return SaleResult(symbol, qty, sold, SaleStatus.FILLED)
    return SaleResult(symbol, qty, sold, SaleStatus.PARTIAL if sold else SaleStatus.UNFILLED)
