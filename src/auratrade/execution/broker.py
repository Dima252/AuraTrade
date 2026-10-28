"""The broker interface the order layer is written against (README §5.7).

Two implementations: `execution.alpaca_broker.AlpacaBroker` (paper, through alpaca-py) and the
in-memory `sim.fake_broker.FakeBroker`, which models what spike 0.3b found so the order code is
tested against the same behaviour before it runs unattended.

Orders are flat: an OTO entry is a buy parent plus one sell stop leg, each its own
`BrokerOrder`, the leg pointing at its parent. Quantities are whole shares (#122).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"


class OrderClass(StrEnum):
    SIMPLE = "simple"
    OTO = "oto"


class OrderStatus(StrEnum):
    NEW = "new"
    PENDING_NEW = "pending_new"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    DONE_FOR_DAY = "done_for_day"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REPLACED = "replaced"
    PENDING_CANCEL = "pending_cancel"
    PENDING_REPLACE = "pending_replace"
    HELD = "held"
    REJECTED = "rejected"
    OTHER = "other"
    """A status the order layer doesn't act on (`suspended`, `stopped`, ...): treated as open, so
    the shares it may hold are never assumed free."""


CLOSED_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
        OrderStatus.REJECTED,
    }
)

# A resting sell stop in one of these will trigger (or already holds the shares). A `held` leg
# won't: its parent isn't completely filled (spike 0.3b).
LIVE_STOP_STATUSES = frozenset(
    {
        OrderStatus.NEW,
        OrderStatus.PENDING_NEW,
        OrderStatus.ACCEPTED,
        OrderStatus.PENDING_REPLACE,
        OrderStatus.PENDING_CANCEL,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.OTHER,
    }
)


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    id: str
    client_order_id: str
    symbol: str
    side: Side
    type: OrderType
    time_in_force: TimeInForce
    order_class: OrderClass
    qty: int
    filled_qty: int
    status: OrderStatus
    limit_price: float | None = None
    stop_price: float | None = None
    filled_avg_price: float | None = None
    parent_id: str | None = None
    replaces: str | None = None
    created_at: datetime | None = None
    filled_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status not in CLOSED_STATUSES

    @property
    def is_leg(self) -> bool:
        """An OTO's stop leg. Its quantity and time-in-force are fixed (spike 0.3b)."""
        return self.order_class is OrderClass.OTO and self.side is Side.SELL

    @property
    def is_sell_stop(self) -> bool:
        return self.side is Side.SELL and self.type in (OrderType.STOP, OrderType.STOP_LIMIT)

    @property
    def remaining_qty(self) -> int:
        return max(self.qty - self.filled_qty, 0)


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    symbol: str
    qty: int
    avg_entry_price: float
    current_price: float | None = None


@dataclass(frozen=True, slots=True)
class Account:
    equity: float
    cash: float
    long_market_value: float


FILL = "FILL"


@dataclass(frozen=True, slots=True)
class Activity:
    """One entry of the account's activity record: a fill (each partial fill its own), or a
    cash entry (a distribution, interest, a withholding, a fee, a deposit), for the tax lots
    (README §5.7)."""

    id: str
    """The broker's id; ids sort in the order the activities happened."""
    kind: str
    """``FILL``, or the broker's activity type (``DIV``, ``DIVNRA``, ``INT``, ``FEE``, ...)."""
    day: date
    """The session (New York) of a fill; the date of a cash entry."""
    symbol: str | None = None
    side: Side | None = None
    qty: float = 0.0
    price: float = 0.0
    amount: float = 0.0
    """A cash entry's net amount (negative for a withholding or a fee); 0 for a fill."""
    order_id: str | None = None


# --- requests ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OtoBuy:
    """An entry: a GTC buy parent with one GTC sell stop leg (#113, #114).

    `trigger` set: a buy stop-limit (trigger, limit). `trigger` None: a limit buy at `limit`,
    for a price already past the trigger but within the limit (archived README §7.1)."""

    symbol: str
    qty: int
    limit_price: float
    stop_loss: float
    client_order_id: str
    trigger: float | None = None


@dataclass(frozen=True, slots=True)
class StopSell:
    """A standalone GTC sell stop."""

    symbol: str
    qty: int
    stop_price: float
    client_order_id: str


@dataclass(frozen=True, slots=True)
class LimitSell:
    symbol: str
    qty: int
    limit_price: float
    client_order_id: str


@dataclass(frozen=True, slots=True)
class MarketSell:
    symbol: str
    qty: int
    client_order_id: str


@dataclass(frozen=True, slots=True)
class LimitBuy:
    """A plain (simple) buy. The order layer never sends one: while a sell stop rests, Alpaca
    rejects it as a potential wash trade (#114). It exists so the fake broker can show that."""

    symbol: str
    qty: int
    limit_price: float
    client_order_id: str


OrderRequest = OtoBuy | StopSell | LimitSell | MarketSell | LimitBuy


# --- errors --------------------------------------------------------------------------------


class BrokerError(Exception):
    """The broker refused or couldn't process a call."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class BrokerUnavailableError(BrokerError):
    """A 5xx, a timeout or a dropped connection: the call may or may not have been carried out."""


class DuplicateClientOrderIdError(BrokerError):
    """`client_order_id must be unique`: ids are unique for the account's lifetime (0.3b)."""


class StopThroughMarketError(BrokerError):
    """`stop price must be less than current price`: the price is already through the stop."""


class InsufficientQtyError(BrokerError):
    """The shares are reserved by another sell order (a resting stop holds them)."""


class WashTradeError(BrokerError):
    """A plain buy while a sell stop rests in the symbol (only OTOs are accepted, #114)."""


class ChainPendingError(BrokerError):
    """`order chain not fully replaced`: a replace is still pending (a closed market)."""


class AdvancedOrderChangeError(BrokerError):
    """A leg's quantity or time-in-force can't be changed (spike 0.3b)."""


class OrderNotFoundError(BrokerError):
    pass


class Broker(Protocol):
    def account(self) -> Account: ...

    def is_market_open(self) -> bool: ...

    def positions(self) -> list[BrokerPosition]: ...

    def open_orders(self) -> list[BrokerOrder]:
        """Every open order, legs included (each leg with its parent's id where known)."""
        ...

    def orders_since(self, after: datetime, symbols: Sequence[str]) -> list[BrokerOrder]:
        """Every order (open or closed) in `symbols` created after `after`, legs included."""
        ...

    def get_order(self, order_id: str) -> BrokerOrder:
        """Raises `OrderNotFoundError`."""
        ...

    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def submit(self, request: OrderRequest) -> BrokerOrder:
        """The new order; for an `OtoBuy`, the parent (`open_orders` lists its leg)."""
        ...

    def replace(
        self,
        order_id: str,
        *,
        qty: int | None = None,
        stop_price: float | None = None,
        limit_price: float | None = None,
    ) -> BrokerOrder:
        """The replacement order (a new id; the old one goes `replaced`)."""
        ...

    def cancel(self, order_id: str) -> None: ...

    def activities(self) -> list[Activity]:
        """The account's whole activity record, oldest first."""
        ...


class PriceSource(Protocol):
    def prices(self, symbols: Iterable[str]) -> dict[str, float | None]:
        """The current decision price per symbol (#120), None when there is no usable price."""
        ...
