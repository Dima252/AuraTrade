"""`Broker` and `PriceSource` on Alpaca (README §5.6): orders through alpaca-py's trading client,
always on the paper endpoint (CLAUDE.md §3.1); prices from IEX (#23).

Errors are mapped to the order layer's: a 5xx or a lost connection is `BrokerUnavailableError`
(the call may have been carried out), and the rejections spike 0.3b met each get their own
class, recognised by Alpaca's message.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass as AlpacaOrderClass
from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Clock, TradeAccount
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    StopOrderRequest,
)

from auratrade.config import DecisionPriceConfig
from auratrade.data.alpaca_data import AlpacaDataClient
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
from auratrade.execution.prices import decision_price

# Alpaca's page limit; the order layer's listings are far smaller.
MAX_ORDERS = 500
# The activities endpoint's largest page.
ACTIVITY_PAGE = 100
NY = ZoneInfo("America/New_York")

_ERRORS: tuple[tuple[str, type[BrokerError]], ...] = (
    ("client_order_id must be unique", DuplicateClientOrderIdError),
    ("stop price must be less than current price", StopThroughMarketError),
    ("insufficient qty", InsufficientQtyError),
    ("wash trade", WashTradeError),
    ("order chain not fully replaced", ChainPendingError),
    ("cannot be changed for advanced orders", AdvancedOrderChangeError),
    ("order not found", OrderNotFoundError),
)


def _message(exc: APIError) -> str:
    try:
        return str(json.loads(str(exc))["message"])
    except (ValueError, KeyError, TypeError):
        return str(exc)


def map_error(exc: Exception) -> BrokerError:
    if isinstance(exc, requests.RequestException) and not isinstance(exc, requests.HTTPError):
        return BrokerUnavailableError(f"{type(exc).__name__}: {exc}")
    if not isinstance(exc, APIError):
        return BrokerUnavailableError(f"{type(exc).__name__}: {exc}")
    status = exc.status_code
    message = _message(exc)
    if status is None or status >= 500 or status == 429:
        return BrokerUnavailableError(message, status=status)
    for fragment, kind in _ERRORS:
        if fragment in message.lower():
            return kind(message, status=status)
    if status == 404:
        return OrderNotFoundError(message, status=status)
    return BrokerError(message, status=status)


def _call[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except (APIError, requests.RequestException) as exc:
        raise map_error(exc) from exc


def _int(value: Any) -> int:
    return 0 if value in (None, "") else int(float(value))


def _float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


_TYPES = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
    "stop": OrderType.STOP,
    "stop_limit": OrderType.STOP_LIMIT,
    # A trailing stop placed by hand is still a resting sell stop.
    "trailing_stop": OrderType.STOP,
}


def to_broker_order(order: AlpacaOrder, parent_id: str | None = None) -> BrokerOrder:
    status_value = _enum_value(order.status)
    try:
        status = OrderStatus(status_value)
    except ValueError:
        status = OrderStatus.OTHER
    order_class = _enum_value(order.order_class)
    return BrokerOrder(
        id=str(order.id),
        client_order_id=str(order.client_order_id),
        symbol=str(order.symbol),
        side=Side(_enum_value(order.side)),
        type=_TYPES.get(_enum_value(order.type), OrderType.MARKET),
        # Anything but GTC expires (day, opg, cls, ioc, fok): the invariant treats it so.
        time_in_force=(
            TimeInForce.GTC if _enum_value(order.time_in_force) == "gtc" else TimeInForce.DAY
        ),
        order_class=OrderClass.OTO
        if order_class in ("oto", "bracket", "oco")
        else (OrderClass.SIMPLE),
        qty=_int(order.qty),
        filled_qty=_int(order.filled_qty),
        status=status,
        limit_price=_float(order.limit_price),
        stop_price=_float(order.stop_price),
        filled_avg_price=_float(order.filled_avg_price),
        parent_id=parent_id,
        replaces=None if order.replaces is None else str(order.replaces),
        created_at=order.created_at,
        filled_at=order.filled_at,
    )


def flatten(orders: Iterable[AlpacaOrder]) -> Iterator[BrokerOrder]:
    """Parents and their legs, each leg pointing at its parent."""
    for order in orders:
        yield to_broker_order(order)
        for leg in order.legs or ():
            yield to_broker_order(leg, parent_id=str(order.id))


def to_activity(raw: Mapping[str, Any]) -> Activity:
    """One entry of `GET /v2/account/activities`: a fill carries its execution's time, qty and
    price; a cash entry its date and net amount (shapes recorded from the paper account)."""
    kind = str(raw["activity_type"])
    if kind == FILL:
        at = datetime.fromisoformat(str(raw["transaction_time"]).replace("Z", "+00:00"))
        return Activity(
            id=str(raw["id"]),
            kind=kind,
            day=at.astimezone(NY).date(),
            symbol=str(raw["symbol"]),
            side=Side(str(raw["side"])),
            qty=float(raw["qty"]),
            price=float(raw["price"]),
            order_id=raw.get("order_id"),
        )
    return Activity(
        id=str(raw["id"]),
        kind=kind,
        day=date.fromisoformat(str(raw["date"])),
        symbol=raw.get("symbol"),
        qty=float(raw.get("qty") or 0.0),
        amount=float(raw.get("net_amount") or 0.0),
    )


def to_request(request: OrderRequest) -> Any:
    common: dict[str, Any] = {
        "symbol": request.symbol,
        "qty": request.qty,
        "client_order_id": request.client_order_id,
    }
    match request:
        case OtoBuy():
            return LimitOrderRequest(
                **common,
                side=OrderSide.BUY,
                time_in_force=AlpacaTimeInForce.GTC,
                order_class=AlpacaOrderClass.OTO,
                limit_price=request.limit_price,
                stop_loss=StopLossRequest(stop_price=request.stop_loss),
            )
        case StopSell():
            return StopOrderRequest(
                **common,
                side=OrderSide.SELL,
                time_in_force=AlpacaTimeInForce.GTC,
                stop_price=request.stop_price,
            )
        case LimitSell():
            return LimitOrderRequest(
                **common,
                side=OrderSide.SELL,
                time_in_force=AlpacaTimeInForce.DAY,
                limit_price=request.limit_price,
            )
        case MarketSell():
            return MarketOrderRequest(
                **common, side=OrderSide.SELL, time_in_force=AlpacaTimeInForce.DAY
            )
        case LimitBuy():
            return LimitOrderRequest(
                **common,
                side=OrderSide.BUY,
                time_in_force=AlpacaTimeInForce.DAY,
                limit_price=request.limit_price,
            )


class AlpacaBroker:
    def __init__(self, client: TradingClient) -> None:
        self._client = client

    @classmethod
    def paper(cls, api_key: str, secret_key: str) -> AlpacaBroker:
        # Paper only (CLAUDE.md §3.1): there is no live constructor.
        return cls(TradingClient(api_key, secret_key, paper=True))

    def account(self) -> Account:
        acct = _call(self._client.get_account)
        assert isinstance(acct, TradeAccount)  # noqa: S101 - alpaca-py's union return type
        return Account(
            equity=_float(acct.equity) or 0.0,
            cash=_float(acct.cash) or 0.0,
            long_market_value=_float(acct.long_market_value) or 0.0,
        )

    def is_market_open(self) -> bool:
        clock = _call(self._client.get_clock)
        assert isinstance(clock, Clock)  # noqa: S101
        return bool(clock.is_open)

    def positions(self) -> list[BrokerPosition]:
        held = _call(self._client.get_all_positions)
        out = []
        for p in held:
            assert isinstance(p, AlpacaPosition)  # noqa: S101
            out.append(
                BrokerPosition(
                    symbol=p.symbol,
                    qty=_int(p.qty),
                    avg_entry_price=_float(p.avg_entry_price) or 0.0,
                    current_price=_float(p.current_price),
                )
            )
        return out

    def _orders(self, request: GetOrdersRequest) -> list[AlpacaOrder]:
        orders = _call(lambda: self._client.get_orders(request))
        return [o for o in orders if isinstance(o, AlpacaOrder)]

    def open_orders(self) -> list[BrokerOrder]:
        nested = self._orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=MAX_ORDERS)
        )
        # A flat listing too: an open leg whose parent has filled might not be nested under it.
        flat = self._orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=False, limit=MAX_ORDERS)
        )
        merged = {o.id: o for o in flatten(nested)}
        for order in flatten(flat):
            merged.setdefault(order.id, order)
        return [o for o in merged.values() if o.is_open]

    def orders_since(self, after: datetime, symbols: Sequence[str]) -> list[BrokerOrder]:
        if not symbols:
            return []
        request = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            after=after.astimezone(UTC),
            symbols=list(symbols),
            nested=True,
            limit=MAX_ORDERS,
        )
        return list(flatten(self._orders(request)))

    def get_order(self, order_id: str) -> BrokerOrder:
        order = _call(lambda: self._client.get_order_by_id(order_id))
        assert isinstance(order, AlpacaOrder)  # noqa: S101
        return to_broker_order(order)

    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        try:
            order = self._client.get_order_by_client_id(client_order_id)
        except (APIError, requests.RequestException) as exc:
            error = map_error(exc)
            if isinstance(error, OrderNotFoundError):
                return None
            raise error from exc
        assert isinstance(order, AlpacaOrder)  # noqa: S101
        return to_broker_order(order)

    def submit(self, request: OrderRequest) -> BrokerOrder:
        order = _call(lambda: self._client.submit_order(to_request(request)))
        assert isinstance(order, AlpacaOrder)  # noqa: S101
        return to_broker_order(order)

    def replace(
        self,
        order_id: str,
        *,
        qty: int | None = None,
        stop_price: float | None = None,
        limit_price: float | None = None,
    ) -> BrokerOrder:
        request = ReplaceOrderRequest(qty=qty, stop_price=stop_price, limit_price=limit_price)
        order = _call(lambda: self._client.replace_order_by_id(order_id, request))
        assert isinstance(order, AlpacaOrder)  # noqa: S101
        return to_broker_order(order)

    def cancel(self, order_id: str) -> None:
        _call(lambda: self._client.cancel_order_by_id(order_id))

    def activities(self) -> list[Activity]:
        out: list[Activity] = []
        params: dict[str, Any] = {"direction": "asc", "page_size": ACTIVITY_PAGE}
        while True:
            page = _call(lambda: self._client.get("/account/activities", dict(params)))
            if not isinstance(page, list):
                raise BrokerError(f"activities: expected a list, got {type(page).__name__}")
            out.extend(to_activity(raw) for raw in page)
            if len(page) < ACTIVITY_PAGE:
                return out
            params["page_token"] = page[-1]["id"]


class AlpacaPrices:
    """The decision price from IEX: the last trade if fresh, else a fresh quote's midpoint."""

    def __init__(
        self,
        client: AlpacaDataClient,
        cfg: DecisionPriceConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._clock = clock

    def prices(self, symbols: Iterable[str]) -> dict[str, float | None]:
        wanted = sorted(set(symbols))
        if not wanted:
            return {}
        trades = self._client.latest_trades(wanted)
        quotes = self._client.latest_quotes(wanted)
        now = self._clock()
        return {s: decision_price(trades.get(s), quotes.get(s), now, self._cfg) for s in wanted}
