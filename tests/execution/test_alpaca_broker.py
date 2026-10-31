"""The Alpaca adapter, offline: alpaca-py models built from the API's JSON (shapes recorded in
spike 0.3b) and a stand-in for the trading client. No network."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass as AlpacaOrderClass
from alpaca.trading.enums import OrderSide
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.models import Clock, TradeAccount
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)

from auratrade.config import load_risk_config
from auratrade.data.alpaca_data import Quote, Trade
from auratrade.execution.alpaca_broker import (
    ACTIVITY_PAGE,
    AlpacaBroker,
    AlpacaPrices,
    flatten,
    map_error,
    to_activity,
    to_broker_order,
    to_request,
)
from auratrade.execution.broker import (
    Activity,
    AdvancedOrderChangeError,
    BrokerError,
    BrokerUnavailableError,
    ChainPendingError,
    DuplicateClientOrderIdError,
    InsufficientQtyError,
    LimitBuy,
    LimitSell,
    MarketSell,
    OrderClass,
    OrderNotFoundError,
    OrderStatus,
    OrderType,
    OtoBuy,
    Side,
    StopSell,
    StopThroughMarketError,
    TimeInForce,
    WashTradeError,
)

NOW = datetime(2026, 10, 9, 13, 40, tzinfo=UTC)


def order_json(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "client_order_id": "at-20261009-F-b-1a2b3c4d",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "submitted_at": NOW.isoformat(),
        "symbol": "F",
        "asset_class": "us_equity",
        "qty": "6",
        "filled_qty": "0",
        "filled_avg_price": None,
        "order_class": "oto",
        "order_type": "stop_limit",
        "type": "stop_limit",
        "side": "buy",
        "time_in_force": "gtc",
        "limit_price": "12.13",
        "stop_price": "12.07",
        "status": "new",
        "extended_hours": False,
        "legs": None,
    }
    return base | overrides


def alpaca_order(**overrides: Any) -> AlpacaOrder:
    return AlpacaOrder(**order_json(**overrides))


def api_error(status: int | None, message: str, code: int = 42210000) -> APIError:
    http = None
    if status is not None:
        response = requests.Response()
        response.status_code = status
        http = requests.HTTPError(response=response)
    return APIError(json.dumps({"code": code, "message": message}), http)


# --- mapping ---------------------------------------------------------------------------------


def test_an_oto_parent_and_its_leg_are_flattened() -> None:
    leg = order_json(
        client_order_id=str(uuid.uuid4()),
        side="sell",
        type="stop",
        order_type="stop",
        limit_price=None,
        stop_price="11.34",
        status="held",
    )
    parent = alpaca_order(legs=[leg])
    flat = list(flatten([parent]))
    assert [o.side for o in flat] == [Side.BUY, Side.SELL]
    assert flat[1].parent_id == str(parent.id)
    assert flat[1].is_leg
    assert flat[1].status is OrderStatus.HELD
    assert (flat[0].qty, flat[0].limit_price, flat[0].stop_price) == (6, 12.13, 12.07)
    assert flat[0].time_in_force is TimeInForce.GTC


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"status": "suspended"}, {"status": OrderStatus.OTHER}),
        ({"time_in_force": "opg"}, {"time_in_force": TimeInForce.DAY}),
        ({"order_class": "simple"}, {"order_class": OrderClass.SIMPLE}),
        ({"order_class": "bracket"}, {"order_class": OrderClass.OTO}),
        (
            {"type": "trailing_stop", "order_type": "trailing_stop", "side": "sell"},
            {"type": OrderType.STOP},
        ),
        (
            {"filled_qty": "4", "filled_avg_price": "12.1", "status": "partially_filled"},
            {"filled_qty": 4, "filled_avg_price": 12.1},
        ),
    ],
)
def test_order_fields_map(fields: dict[str, Any], expected: dict[str, Any]) -> None:
    mapped = to_broker_order(alpaca_order(**fields))
    assert {k: getattr(mapped, k) for k in expected} == expected


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (api_error(422, "client_order_id must be unique"), DuplicateClientOrderIdError),
        (api_error(422, "stop price must be less than current price"), StopThroughMarketError),
        (
            api_error(403, "insufficient qty available for order (requested: 1, available: 0)"),
            InsufficientQtyError,
        ),
        (api_error(403, "potential wash trade detected. use complex orders"), WashTradeError),
        (api_error(422, "order chain not fully replaced"), ChainPendingError),
        (api_error(422, "qty cannot be changed for advanced orders"), AdvancedOrderChangeError),
        (api_error(404, "order not found"), OrderNotFoundError),
        (api_error(404, "something else"), OrderNotFoundError),
        (api_error(500, "internal server error"), BrokerUnavailableError),
        (api_error(429, "rate limit"), BrokerUnavailableError),
        (api_error(None, "no response"), BrokerUnavailableError),
        (api_error(422, "asset is not tradable"), BrokerError),
        (requests.ConnectionError("reset"), BrokerUnavailableError),
        (requests.Timeout("slow"), BrokerUnavailableError),
        (ValueError("anything else"), BrokerUnavailableError),
    ],
)
def test_errors_map(error: Exception, kind: type[BrokerError]) -> None:
    mapped = map_error(error)
    assert type(mapped) is kind


def test_a_plain_text_api_error_keeps_its_text() -> None:
    response = requests.Response()
    response.status_code = 403
    mapped = map_error(APIError("forbidden", requests.HTTPError(response=response)))
    assert (type(mapped), str(mapped), mapped.status) == (BrokerError, "forbidden", 403)


def test_requests_are_built_as_the_spike_proved() -> None:
    oto = to_request(OtoBuy("F", 6, 12.13, 11.34, "at-1"))
    assert isinstance(oto, LimitOrderRequest)
    assert (oto.order_class, oto.time_in_force) == (AlpacaOrderClass.OTO, AlpacaTimeInForce.GTC)
    assert oto.stop_loss is not None
    assert (oto.limit_price, oto.stop_loss.stop_price) == (12.13, 11.34)
    stop = to_request(StopSell("F", 6, 11.34, "at-3"))
    assert isinstance(stop, StopOrderRequest)
    assert (stop.side, stop.time_in_force) == (OrderSide.SELL, AlpacaTimeInForce.GTC)
    sale = to_request(LimitSell("F", 1, 12.0, "at-4"))
    assert isinstance(sale, LimitOrderRequest)
    assert sale.time_in_force is AlpacaTimeInForce.DAY
    assert isinstance(to_request(MarketSell("F", 1, "at-5")), MarketOrderRequest)
    buy = to_request(LimitBuy("F", 1, 12.0, "at-6"))
    assert isinstance(buy, LimitOrderRequest)
    assert buy.side is OrderSide.BUY


# --- the broker over a stand-in trading client ------------------------------------------------


class StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.parent = alpaca_order(
            legs=[order_json(side="sell", type="stop", order_type="stop", status="held")]
        )
        self.raise_on: dict[str, Exception] = {}

    def _maybe(self, name: str) -> None:
        if name in self.raise_on:
            raise self.raise_on[name]

    def get_account(self) -> TradeAccount:
        self._maybe("get_account")
        return TradeAccount(
            id=uuid.uuid4(),
            account_number="PA1",
            status="ACTIVE",
            equity="5000.5",
            cash="4000",
            long_market_value="1000.5",
        )

    def get_clock(self) -> Clock:
        return Clock(timestamp=NOW, is_open=True, next_open=NOW, next_close=NOW)

    def get_all_positions(self) -> list[AlpacaPosition]:
        return [
            AlpacaPosition(
                asset_id=uuid.uuid4(),
                symbol="F",
                exchange="NYSE",
                asset_class="us_equity",
                avg_entry_price="12.1",
                qty="6",
                side="long",
                cost_basis="72.6",
                current_price="12.3",
            )
        ]

    def get_orders(self, request: Any) -> list[AlpacaOrder]:
        self.calls.append(("get_orders", request))
        if request.nested:
            return [self.parent]
        # The flat listing repeats the leg without its parent and adds a standalone stop.
        leg = self.parent.legs[0]  # type: ignore[index]
        return [self.parent, leg, alpaca_order(side="sell", type="stop", order_type="stop")]

    def get_order_by_id(self, order_id: str) -> AlpacaOrder:
        return self.parent

    def get_order_by_client_id(self, client_id: str) -> AlpacaOrder:
        self._maybe("get_order_by_client_id")
        return self.parent

    def submit_order(self, request: Any) -> AlpacaOrder:
        self.calls.append(("submit_order", request))
        self._maybe("submit_order")
        return self.parent

    def replace_order_by_id(self, order_id: str, request: Any) -> AlpacaOrder:
        self.calls.append(("replace", (order_id, request)))
        return self.parent

    def cancel_order_by_id(self, order_id: str) -> None:
        self.calls.append(("cancel", order_id))
        self._maybe("cancel_order_by_id")


def test_the_broker_reads() -> None:
    client = StubClient()
    broker = AlpacaBroker(client)  # type: ignore[arg-type]
    assert broker.account().equity == 5000.5
    assert broker.is_market_open()
    assert [(p.symbol, p.qty, p.current_price) for p in broker.positions()] == [("F", 6, 12.3)]
    orders = broker.open_orders()
    leg = next(o for o in orders if o.is_leg)
    assert leg.parent_id == str(client.parent.id)  # the nested listing's parent link is kept
    assert len(orders) == 3
    assert broker.orders_since(NOW - timedelta(days=1), []) == []
    assert len(broker.orders_since(NOW - timedelta(days=1), ["F"])) == 2
    request = client.calls[-1][1]
    assert request.symbols == ["F"]
    assert broker.get_order("x").symbol == "F"
    assert broker.order_by_client_id("at-x") is not None


def test_the_broker_writes_and_maps_errors() -> None:
    client = StubClient()
    broker = AlpacaBroker(client)  # type: ignore[arg-type]
    broker.submit(StopSell("F", 6, 11.0, "at-s"))
    broker.replace("abc", stop_price=11.5)
    broker.cancel("abc")
    assert [c[0] for c in client.calls] == ["submit_order", "replace", "cancel"]
    client.raise_on["submit_order"] = api_error(422, "client_order_id must be unique")
    with pytest.raises(DuplicateClientOrderIdError):
        broker.submit(StopSell("F", 6, 11.0, "at-s"))
    client.raise_on["get_order_by_client_id"] = api_error(404, "order not found")
    assert broker.order_by_client_id("at-x") is None
    client.raise_on["get_order_by_client_id"] = api_error(500, "boom")
    with pytest.raises(BrokerUnavailableError):
        broker.order_by_client_id("at-x")
    client.raise_on["cancel_order_by_id"] = requests.ConnectionError("reset")
    with pytest.raises(BrokerUnavailableError):
        broker.cancel("abc")


def test_paper_is_the_only_endpoint() -> None:
    broker = AlpacaBroker.paper("key", "secret")
    assert broker._client._use_raw_data is False  # type: ignore[attr-defined]
    assert "paper" in broker._client._base_url  # type: ignore[attr-defined]


class StubData:
    def latest_trades(self, symbols: list[str]) -> dict[str, Trade]:
        return {"F": Trade("F", 12.3, 100, NOW - timedelta(seconds=30))}

    def latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {
            "AMD": Quote("AMD", 99.9, 100.1, 1, 1, NOW - timedelta(seconds=5)),
            "F": Quote("F", 12.2, 12.4, 1, 1, NOW),
        }


def test_prices_follow_the_decision_price_rule() -> None:
    cfg = load_risk_config().execution.decision_price
    prices = AlpacaPrices(StubData(), cfg, clock=lambda: NOW)  # type: ignore[arg-type]
    assert prices.prices(["F", "AMD", "SSO"]) == {"AMD": 100.0, "F": 12.3, "SSO": None}
    assert prices.prices([]) == {}


# --- the account's activities (task 5.3) -----------------------------------------------------

ACTIVITIES = Path(__file__).parents[1] / "fixtures" / "alpaca" / "account_activities.json"
# Alpaca's documented shape for a cash dividend (paper recorded none yet).
DIVIDEND = {
    "id": "20261102000000000::0c3c8bde-0000-4000-8000-000000000001",
    "activity_type": "DIV",
    "date": "2026-11-02",
    "net_amount": "3.48",
    "symbol": "BIL",
    "qty": "12",
    "per_share_amount": "0.29",
}


class ActivityClient(StubClient):
    def __init__(self, pages: list[Any]) -> None:
        super().__init__()
        self.pages = pages

    def get(self, path: str, params: dict[str, Any]) -> Any:
        self.calls.append(("get", (path, params)))
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def test_a_recorded_fill_and_cash_entries_map() -> None:
    raw = json.loads(ACTIVITIES.read_text(encoding="utf-8"))
    deposit, fee, partial, fill = (to_activity(r) for r in raw)
    assert (deposit.kind, deposit.day, deposit.amount, deposit.symbol) == (
        "JNLC",
        date(2026, 10, 8),
        5000.0,
        None,
    )
    assert (fee.kind, fee.amount) == ("FEE", -0.01)
    # 14:26 UTC is 10:26 in New York: the fill's session.
    assert partial == Activity(
        id=raw[2]["id"],
        kind="FILL",
        day=date(2026, 10, 26),
        symbol="SLN",
        side=Side.BUY,
        qty=6.0,
        price=19.69,
        order_id="b486c2a0-4fa9-48e8-bb22-5eb3b3cfa4c3",
    )
    assert fill.qty == 1.0
    assert fill.order_id == partial.order_id
    dividend = to_activity(DIVIDEND)
    assert (dividend.kind, dividend.symbol, dividend.qty, dividend.amount) == (
        "DIV",
        "BIL",
        12.0,
        3.48,
    )


def test_activities_are_read_page_by_page_oldest_first() -> None:
    raw = json.loads(ACTIVITIES.read_text(encoding="utf-8"))
    full = [raw[3] | {"id": f"20261026{i:09d}::x"} for i in range(ACTIVITY_PAGE)]
    client = ActivityClient([full, [DIVIDEND]])
    out = AlpacaBroker(client).activities()  # type: ignore[arg-type]
    assert len(out) == ACTIVITY_PAGE + 1
    assert out[-1].kind == "DIV"
    (_, (path, first)), (_, (_, second)) = client.calls
    assert path == "/account/activities"
    assert first == {"direction": "asc", "page_size": ACTIVITY_PAGE}
    assert second["page_token"] == full[-1]["id"]


def test_activities_fail_on_an_unexpected_body_or_an_error() -> None:
    with pytest.raises(BrokerError, match="expected a list"):
        AlpacaBroker(ActivityClient([{"message": "?"}])).activities()  # type: ignore[arg-type]
    with pytest.raises(BrokerUnavailableError):
        AlpacaBroker(ActivityClient([api_error(500, "boom")])).activities()  # type: ignore[arg-type]
