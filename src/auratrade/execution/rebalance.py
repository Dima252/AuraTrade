"""The rebalance's orders (README §5.6.3, §7.1), pure: targets → whole-share orders.

From the enforced targets (fractions of equity), the broker's holdings and the decision prices:
- **The drift band.** A holding is traded only if its target changed from last month's or its
  weight is more than ``portfolio.drift_band`` from its target; the first rebalance (no earlier
  targets) trades every holding. The same rule as the simulated book (`sim/portfolio.py`).
- **Sells** go to the target value ÷ the decision price, floored (a target of 0 sells all).
- **Buys** go to the target value ÷ the limit price (the worst fill), floored, and together fit
  the cash the sales left at their limits: the account never borrows (README §5.6.6).
- **What may be bought** (README §5.9): nothing in a fund with an open mismatch; never the
  leveraged fund while the drawdown halt is on; and while increases are blocked (the kill
  switch, a repaired invariant, incomplete data, a calendar disagreement) only the cash fund,
  and only if it is already held. Sales toward a lower target are never blocked.
- **The leveraged fund's stop** (README §7.2): ``1 - portfolio.stop_fraction`` of its decision
  price, rounded down to the tick, when it is bought from flat; raised to that on each later E
  while it is held, never lowered.

A fund without a decision price isn't traded: its move is skipped and the rebalance stays owed
(its retry, ``portfolio.retry_sessions``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from auratrade.config import PortfolioConfig
from auratrade.execution.prices import marketable_buy_limit, round_down
from auratrade.strategies.core_allocation import WEIGHT_TOLERANCE

# A share count computed as 9.9999999 is 10.
_SHARE_TOLERANCE = 1e-9


class Action(StrEnum):
    SELL = "SELL"
    BUY = "BUY"
    HOLD = "HOLD"


class Why(StrEnum):
    FIRST = "FIRST"
    """The first rebalance: every holding goes to its target."""
    TARGET_CHANGED = "TARGET_CHANGED"
    DRIFTED = "DRIFTED"
    """Its weight is more than the drift band from its target."""
    WITHIN_BAND = "WITHIN_BAND"
    AT_TARGET = "AT_TARGET"
    """Traded, but whole shares already put it at its target."""
    NO_PRICE = "NO_PRICE"
    BLOCKED = "BLOCKED"
    """A buy that isn't allowed (`buy_block`)."""
    NO_CASH = "NO_CASH"
    """A buy the cash left after the sales can't pay for (rounding and slippage: not owed; a
    sale that didn't fill is owed itself)."""


@dataclass(frozen=True, slots=True)
class Move:
    symbol: str
    target: float
    """The target weight, a fraction of equity."""
    held: int
    weight: float | None
    """The weight held now; None without a price."""
    action: Action
    qty: int
    why: Why
    price: float | None
    limit: float | None = None
    """A buy's limit price."""
    owed: bool = False
    """Traded but not placed (no price, a blocked buy): the rebalance stays unfinished, and its
    retry tries again with the same targets."""
    note: str = ""


@dataclass(frozen=True, slots=True)
class Blocks:
    """What may not be bought this run."""

    increases: tuple[str, ...] = ()
    """Why increases of exposure are blocked; empty when they aren't."""
    halted: bool = False
    mismatched: frozenset[str] = frozenset()


def buy_block(symbol: str, held: int, blocks: Blocks, cfg: PortfolioConfig) -> str | None:
    """Why `symbol` may not be bought, or None (README §5.9)."""
    if symbol in blocks.mismatched:
        return "an open mismatch in the fund"
    if blocks.halted and symbol == cfg.leveraged:
        return "the drawdown halt: no leverage"
    if blocks.increases and not (symbol == cfg.cash and held > 0):
        return "increases blocked: " + "; ".join(blocks.increases)
    return None


def halted_targets(targets: Mapping[str, float], cfg: PortfolioConfig) -> dict[str, float]:
    """The targets while the drawdown halt is on: the leveraged fund's weight in the market
    fund, as the halt's sale moves it (README §7.3)."""
    out = dict(targets)
    weight = out.pop(cfg.leveraged, 0.0)
    if weight > WEIGHT_TOLERANCE:
        out[cfg.market] = out.get(cfg.market, 0.0) + weight
    return out


def leveraged_stop(price: float, current: float | None, cfg: PortfolioConfig) -> float:
    """The leveraged fund's stop level for an E at `price` (README §7.2): the fraction of the
    price rounded down to the tick, or the `current` level of a fund still held if higher."""
    level = round_down(price * (1 - cfg.stop_fraction))
    return level if current is None else max(current, level)


def plan(
    targets: Mapping[str, float],
    previous: Mapping[str, float] | None,
    held: Mapping[str, int],
    prices: Mapping[str, float | None],
    equity: float,
    cfg: PortfolioConfig,
    slip: float,
) -> list[Move]:
    """One move per fund held or targeted, sells first, then buys, then the rest; each in
    symbol order. A buy's quantity is before `fit_buys` and `allow_buys`."""
    if equity <= 0:
        raise ValueError(f"equity must be positive, got {equity}")
    symbols = sorted({s for s, q in held.items() if q > 0} | _positive(targets))
    moves = [
        _move(s, targets.get(s, 0.0), previous, held.get(s, 0), prices.get(s), equity, cfg, slip)
        for s in symbols
    ]
    order = {Action.SELL: 0, Action.BUY: 1, Action.HOLD: 2}
    return sorted(moves, key=lambda m: (order[m.action], m.symbol))


def _positive(targets: Mapping[str, float]) -> set[str]:
    return {s for s, w in targets.items() if w > WEIGHT_TOLERANCE}


def _move(
    symbol: str,
    target: float,
    previous: Mapping[str, float] | None,
    held: int,
    price: float | None,
    equity: float,
    cfg: PortfolioConfig,
    slippage: float,
) -> Move:
    weight = None if price is None else held * price / equity
    if previous is None:
        why = Why.FIRST
    elif abs(target - previous.get(symbol, 0.0)) > WEIGHT_TOLERANCE:
        why = Why.TARGET_CHANGED
    elif weight is not None and abs(weight - target) > cfg.drift_band:
        why = Why.DRIFTED
    elif weight is None:
        # Unchanged, and its drift can't be measured without a price: kept as it is.
        return Move(symbol, target, held, None, Action.HOLD, 0, Why.NO_PRICE, None)
    else:
        return Move(symbol, target, held, weight, Action.HOLD, 0, Why.WITHIN_BAND, price)
    if price is None:
        return Move(symbol, target, held, None, Action.HOLD, 0, Why.NO_PRICE, None, owed=True)
    value = max(target, 0.0) * equity
    if held * price > value:
        keep = math.floor(value / price + _SHARE_TOLERANCE) if target > WEIGHT_TOLERANCE else 0
        if held > keep:
            return Move(symbol, target, held, weight, Action.SELL, held - keep, why, price)
        return Move(symbol, target, held, weight, Action.HOLD, 0, Why.AT_TARGET, price)
    limit = marketable_buy_limit(price, slippage)
    want = math.floor(value / limit + _SHARE_TOLERANCE) - held
    if want > 0:
        return Move(symbol, target, held, weight, Action.BUY, want, why, price, limit)
    return Move(symbol, target, held, weight, Action.HOLD, 0, Why.AT_TARGET, price)


def allow_buys(moves: Iterable[Move], blocks: Blocks, cfg: PortfolioConfig) -> list[Move]:
    """Each buy that may not go in becomes a blocked hold (`buy_block`)."""
    out: list[Move] = []
    for move in moves:
        reason = None
        if move.action is Action.BUY:
            reason = buy_block(move.symbol, move.held, blocks, cfg)
        if reason is None:
            out.append(move)
        else:
            out.append(
                replace(move, action=Action.HOLD, qty=0, why=Why.BLOCKED, owed=True, note=reason)
            )
    return out


def fit_buys(moves: Sequence[Move], cash: float) -> list[Move]:
    """Scale the buys down, each floored, until they cost at most `cash` at their limits."""
    buys = [m for m in moves if m.action is Action.BUY]
    cost = math.fsum(m.qty * (m.limit or 0.0) for m in buys)
    if cost <= cash:
        return list(moves)
    scale = max(cash, 0.0) / cost
    out: list[Move] = []
    for move in moves:
        if move.action is not Action.BUY:
            out.append(move)
            continue
        qty = math.floor(move.qty * scale + _SHARE_TOLERANCE)
        note = f"{move.qty} wanted, {qty} fit the cash"
        if qty > 0:
            out.append(replace(move, qty=qty, note=note))
        else:
            out.append(replace(move, action=Action.HOLD, qty=0, why=Why.NO_CASH, note=note))
    return out
