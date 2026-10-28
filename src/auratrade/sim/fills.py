"""The fill model's prices (README §13, #63), pure: what a buy or a sale gets after slippage.

Slippage is a fraction of the price, given by the caller (the portfolio's in
``portfolio.backtest``). The portfolio backtest trades its synthetic funds, index levels rather
than tradable prices, at these prices before the tick (pre-registration §3.5-§3.6).
"""


def buy_price(price: float, slip: float) -> float:
    """A buy's price with slippage, before the tick."""
    return price * (1 + slip)


def sell_price(price: float, slip: float) -> float:
    """A sale's price with slippage, before the tick."""
    return price * (1 - slip)
