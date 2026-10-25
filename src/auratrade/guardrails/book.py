"""The momentum book's guardrails (README §8; #148; task 3.11), pure.

They run on every Portfolio Manager action before anything reaches `signals`:
- **Buys:** blocked by any entry block (kill switch, halts, the overlay, a half day, data
  integrity); only today's candidates; never a name held, pending or already bought today (no
  adding to a holding); at most ``book.holdings`` positions with pending buys; 1 or ½ slot; the
  cluster cap (a name without cluster data counts as linked, #140); sized in whole shares on the
  limit, within the cash left and the exposure cap (no margin), clipped to the room and rejected
  at 0 shares. Checks run in that order and the first rejection ends them; each approval counts
  against the next.
- **Exits:** only names held. Never blocked: no halt or kill switch may block an exit
  (CLAUDE.md §3.5).
- **Stop raises:** only names held, rounded down to the tick, strictly above the current stop
  and below the last price (Alpaca rejects a sell stop above the market, spike 0.3b).

The rule sells (out of the hold zone) are code's, not the Portfolio Manager's: no action can
keep a name past them, so there is nothing here to check.
"""

import math
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field

from auratrade.config import BookConfig, BookOverlay, GuardrailsConfig, HaltsConfig, Regime
from auratrade.execution.prices import round_down
from auratrade.guardrails.blocks import EntryBlock, entries_blocked, loss_halts
from auratrade.guardrails.clusters import ClusterMember, cluster_check
from auratrade.guardrails.model import (
    Finding,
    GuardrailRule,
    Outcome,
    clip,
    finite_positive,
    outcome_of,
    reject,
)
from auratrade.strategies.momentum_book import buy_limit, initial_stop, shares_for, slot_value

SLOT_FRACTIONS = (1.0, 0.5)


def book_halts(
    *, day_return: float, week_return: float, drawdown: float, halts: HaltsConfig, cfg: BookConfig
) -> frozenset[EntryBlock]:
    """The loss halts with the book's own drawdown threshold (30% below the peak, #148)."""
    config = halts.model_copy(update={"drawdown": cfg.drawdown_halt})
    return loss_halts(
        day_return=day_return, week_return=week_return, drawdown=drawdown, config=config
    )


def overlay_blocks(
    cfg: BookConfig,
    *,
    effective_regime: Regime | None,
    crisis: bool,
    spy_above_sma: bool | None = None,
) -> frozenset[EntryBlock]:
    """What the overlay blocks (README §6): `CORRECTION` or an unknown regime and the crisis
    override (the primary); SPY not above its SMA, or unknown, and the crisis override (C4);
    nothing (C3)."""
    if cfg.overlay is BookOverlay.OFF:
        return frozenset()
    out: set[EntryBlock] = {EntryBlock.CRISIS_OVERRIDE} if crisis else set()
    if cfg.overlay is BookOverlay.REGIME:
        if effective_regime is None or effective_regime is Regime.CORRECTION:
            out.add(EntryBlock.CORRECTION)
    elif spy_above_sma is not True:
        out.add(EntryBlock.INDEX_TREND)
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class BookAccount:
    equity: float
    cash: float
    exposure: float
    """Market value of the positions plus pending buys at their limit."""
    held: frozenset[str]
    pending: frozenset[str]
    """Symbols with a buy not yet filled."""
    stops: Mapping[str, float] = field(default_factory=dict)
    """The current stop per held symbol."""


@dataclass(frozen=True, slots=True)
class BookBuy:
    symbol: str
    slot_fraction: float = 1.0


@dataclass(frozen=True, slots=True)
class BuyDecision:
    symbol: str
    outcome: Outcome
    findings: tuple[Finding, ...]
    shares: int = 0
    limit: float | None = None
    stop: float | None = None


def _rejected(symbol: str, finding: Finding) -> BuyDecision:
    return BuyDecision(symbol, Outcome.REJECT, (finding,))


def evaluate_buys(
    buys: Sequence[BookBuy],
    *,
    candidates: Sequence[str],
    account: BookAccount,
    prices: Mapping[str, float | None],
    blocks: Set[EntryBlock],
    holdings: Sequence[ClusterMember],
    members: Mapping[str, ClusterMember],
    cfg: BookConfig,
    guard: GuardrailsConfig,
    limit_slippage: float,
    clusters: bool = True,
) -> list[BuyDecision]:
    """Each buy's decision, in order; an approval counts against the buys after it."""
    out: list[BuyDecision] = []
    blocked = entries_blocked(blocks)
    taken = set(account.held | account.pending)
    group = list(holdings)
    cash_left = account.cash
    exposure = account.exposure
    tradable = set(candidates)
    for buy in buys:
        symbol = buy.symbol
        if blocked is not None:
            out.append(_rejected(symbol, blocked))
            continue
        if buy.slot_fraction not in SLOT_FRACTIONS:
            message = f"slot fraction {buy.slot_fraction} isn't 1 or 0.5"
            invalid = reject(GuardrailRule.INVALID_INPUT, message, buy.slot_fraction)
            out.append(_rejected(symbol, invalid))
            continue
        if symbol not in tradable:
            message = f"{symbol} isn't one of today's candidates"
            out.append(_rejected(symbol, reject(GuardrailRule.TRADABLE_SYMBOL, message)))
            continue
        if symbol in taken:
            message = f"{symbol} is already held, pending or bought: no adding"
            out.append(_rejected(symbol, reject(GuardrailRule.TIGHTEN_ONLY, message)))
            continue
        if len(taken) >= cfg.holdings:
            message = f"{cfg.holdings} positions already held or pending"
            out.append(
                _rejected(symbol, reject(GuardrailRule.MAX_POSITIONS, message, len(taken) + 1))
            )
            continue
        price = prices.get(symbol)
        if (
            price is None
            or not finite_positive(price, account.equity)
            or not math.isfinite(account.cash)
        ):
            message = f"{symbol}: no usable price or account values"
            out.append(_rejected(symbol, reject(GuardrailRule.INVALID_INPUT, message)))
            continue
        member = members.get(symbol)
        if clusters:
            finding = (
                reject(GuardrailRule.CLUSTER, f"{symbol} has no cluster data")
                if member is None
                else cluster_check(member, group, guard)
            )
            if finding is not None:
                out.append(_rejected(symbol, finding))
                continue
        limit = buy_limit(price, limit_slippage)
        target = shares_for(slot_value(account.equity, cfg, buy.slot_fraction), limit)
        room = min(cash_left, account.equity * cfg.max_exposure - exposure)
        shares = min(target, shares_for(room, limit))
        findings: list[Finding] = []
        if shares < 1:
            message = f"{symbol}: no whole share fits the slot, the cash or the exposure cap"
            out.append(_rejected(symbol, reject(GuardrailRule.NO_MARGIN, message, target)))
            continue
        if shares < target:
            message = f"{symbol}: clipped to the cash and exposure left"
            findings.append(clip(GuardrailRule.NO_MARGIN, message, target, shares))
        cost = shares * limit
        cash_left -= cost
        exposure += cost
        taken.add(symbol)
        if member is not None:
            group.append(member)
        out.append(
            BuyDecision(
                symbol,
                outcome_of(findings),
                tuple(findings),
                shares,
                limit,
                initial_stop(price, cfg),
            )
        )
    return out


def evaluate_exit(symbol: str, account: BookAccount) -> Finding | None:
    """An early exit tightens, so only a name not held is rejected; nothing blocks it."""
    if symbol not in account.held:
        return reject(GuardrailRule.TRADABLE_SYMBOL, f"{symbol} isn't held")
    return None


@dataclass(frozen=True, slots=True)
class StopRaiseDecision:
    symbol: str
    finding: Finding | None
    stop: float | None
    """The stop to place (rounded down to the tick), None when rejected."""


def evaluate_stop_raise(
    symbol: str, stop: float, *, account: BookAccount, last_price: float | None
) -> StopRaiseDecision:
    """A stop raise from the Portfolio Manager: held, finite, above the current stop after
    rounding down to the tick, and below the last price."""
    if symbol not in account.held:
        return StopRaiseDecision(
            symbol, reject(GuardrailRule.TRADABLE_SYMBOL, f"{symbol} isn't held"), None
        )
    current = account.stops.get(symbol)
    if current is None or last_price is None or not finite_positive(stop, current, last_price):
        message = f"{symbol}: the stop, the current stop or the last price is unusable"
        return StopRaiseDecision(symbol, reject(GuardrailRule.INVALID_INPUT, message, stop), None)
    rounded = round_down(stop)
    if rounded <= current:
        message = f"{symbol}: {rounded} doesn't raise the stop at {current}"
        return StopRaiseDecision(symbol, reject(GuardrailRule.TIGHTEN_ONLY, message, rounded), None)
    if rounded >= last_price:
        message = f"{symbol}: {rounded} isn't below the last price {last_price}"
        return StopRaiseDecision(symbol, reject(GuardrailRule.PRICE_LEVELS, message, rounded), None)
    return StopRaiseDecision(symbol, None, rounded)
