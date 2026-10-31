"""The portfolio's market from the real funds' daily bars (#31), pure.

`sim/portfolio.py` runs on a `Market`: each fund's price index, its distributions as a
fraction of its value per session, the market's total-return index (the trend's signal) and
its daily returns (the volatility cap's). The backtest synthesizes one from the long history;
the twin and the live book build one here from two sets of Alpaca bars per fund:
- **split-adjusted closes** are the price (what a share trades at);
- **total-return closes** (``adjustment=all``) also carry the distributions, so a session's
  distribution is its total return over its price return: (TR_t / TR_t-1) / (P_t / P_t-1) - 1.
  The ratio's float noise (below ``DISTRIBUTION_FLOOR``) is no distribution.

The sessions are the market fund's. A fund without a bar on one of them (a trading halt)
carries its last close forward at no distribution; a fund with no bar on the first session
is an error: the sim couldn't price it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from auratrade.config import PortfolioConfig
from auratrade.sim.portfolio import Market
from auratrade.strategies.core_allocation import TRADING_DAYS

DISTRIBUTION_FLOOR = 1e-6

type Closes = Mapping[str, Mapping[date, float]]


def funds_market(
    split: Closes, total_return: Closes, cfg: PortfolioConfig, start: date | None = None
) -> Market:
    """The market from ``start`` (all the market fund's sessions without one)."""
    funds = sorted(split)
    if cfg.market not in funds or set(funds) != set(total_return):
        raise ValueError("every fund needs split and total-return closes, the market fund too")
    days = sorted(d for d in split[cfg.market] if start is None or d >= start)
    if len(days) < 2:
        raise ValueError("too few sessions for a market")
    prices: dict[str, list[float]] = {}
    payouts: dict[str, list[float]] = {}
    for symbol in funds:
        price = _filled(split[symbol], days, symbol)
        tr = _filled(total_return[symbol], days, symbol)
        prices[symbol] = price
        payouts[symbol] = [0.0, *(_distribution(price, tr, i) for i in range(1, len(days)))]
    index = _filled(total_return[cfg.market], days, cfg.market)
    returns = [0.0, *(index[i] / index[i - 1] - 1 for i in range(1, len(days)))]
    cash = _filled(total_return[cfg.cash], days, cfg.cash) if cfg.cash in total_return else None
    rf = [0.0] * len(days)
    if cash is not None:
        rf = [0.0, *(cash[i] / cash[i - 1] - 1 for i in range(1, len(days)))]
    # Only a margin loan (comparison C3) reads it; the live book has none.
    per_year = [TRADING_DAYS] * len(days)
    return Market(days, prices, payouts, index, returns, rf, per_year)


def _filled(closes: Mapping[date, float], days: Sequence[date], symbol: str) -> list[float]:
    out: list[float] = []
    for day in days:
        close = closes.get(day)
        if close is None:
            if not out:
                raise ValueError(f"{symbol} has no close on {day}, the market's first session")
            close = out[-1]
        out.append(close)
    return out


def _distribution(price: Sequence[float], tr: Sequence[float], i: int) -> float:
    paid = (tr[i] / tr[i - 1]) / (price[i] / price[i - 1]) - 1
    return paid if paid > DISTRIBUTION_FLOOR else 0.0
