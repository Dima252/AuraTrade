"""Prices the order layer acts on: the decision price (#23) and order-price rounding (pure)."""

from __future__ import annotations

import math
from datetime import datetime

from auratrade.config import DecisionPriceConfig
from auratrade.data.alpaca_data import Quote, Trade


def tick(price: float) -> float:
    """The valid price increment: $0.01 at $1 and above, $0.0001 below."""
    return 0.01 if price >= 1 else 0.0001


def round_down(price: float) -> float:
    step = tick(price)
    return round(math.floor(round(price / step, 6)) * step, 4)


def round_up(price: float) -> float:
    step = tick(price)
    return round(math.ceil(round(price / step, 6)) * step, 4)


def decision_price(
    trade: Trade | None, quote: Quote | None, now: datetime, cfg: DecisionPriceConfig
) -> float | None:
    """The last IEX trade if fresh, else the IEX quote midpoint if fresh with a sane spread,
    else None: no price (README glossary, #23). A fund without one isn't traded that run, and
    the leveraged fund's stop keeps resting at the broker."""
    if trade is not None and (now - trade.at).total_seconds() <= cfg.trade_max_age_seconds:
        return trade.price
    if quote is None or (now - quote.at).total_seconds() > cfg.quote_max_age_seconds:
        return None
    if quote.bid <= 0 or quote.ask < quote.bid:
        return None
    mid = (quote.bid + quote.ask) / 2
    if (quote.ask - quote.bid) / mid > cfg.max_spread:
        return None
    return mid


def marketable_sell_limit(price: float, slippage: float) -> float:
    """A sell limit far enough below the price to fill at once, rounded down to a tick."""
    return round_down(price * (1 - slippage))


def marketable_buy_limit(price: float, slippage: float) -> float:
    """A buy limit far enough above the price to fill at once, rounded up to a tick."""
    return round_up(price * (1 + slippage))
