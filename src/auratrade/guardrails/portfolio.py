"""The portfolio's guardrails (README §8; #5, #4), pure.

They run on every Portfolio Manager decision before its targets reach `portfolio_targets`:
- **The level:** the rule level from code (`strategies/core_allocation.py`) plus the LLM's
  shift, clipped into the band: ± ``max_shift`` within [``min_level``, ``max_level``], and only
  downward while the trend is DOWN, the volatility cap or the drawdown halt is on. A shift that
  isn't a finite number is rejected (no shift). The core is sized from it so that the book, with
  the satellite at its neutral (the market fund), is at that level (#4).
- **The satellite:** only funds on the approved list (so never a leveraged or inverse one); each
  weight a finite fraction no larger than ``max_etf_weight`` (the market fund, the neutral, up
  to the whole ``satellite_share``; clipped); together no more than ``satellite_share`` (scaled
  down). What is left of the satellite goes to cash.
- **Hold by default** (#4): a weight within ``satellite_min_change`` of last month's target
  keeps that target, and a new holding smaller than that isn't opened. A full exit is honoured.
- **The tax budget** (#4): the tax the satellite's sales would realize (FIFO, at the decision's
  prices) and the year's earlier satellite tax may not exceed ``satellite_tax_budget`` of
  equity; a decision over it is moved back toward last month's targets until it fits. Not
  applied while the shift may only go down: reducing risk is never held back by tax.
- **No increase** (the kill switch, an incomplete session, a calendar disagreement): no fund's
  target may rise above its current weight, except cash; the excess goes to cash. Sales toward
  a lower target are never blocked (CLAUDE.md §3.5).

The LLM never names a price; the targets are weights, and code prices every order.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from auratrade.config import PortfolioConfig
from auratrade.guardrails.model import Finding, GuardrailRule, Outcome, clip, outcome_of, reject
from auratrade.strategies.core_allocation import (
    WEIGHT_TOLERANCE,
    Trend,
    book_targets,
    core_level,
    rule_level,
    shift_band,
)
from auratrade.tax.israel import Lot, fifo_gain

# Halvings of the step back toward last month's targets: far below a share.
TAX_SEARCH_STEPS = 40


@dataclass(frozen=True, slots=True)
class PortfolioDecision:
    """What the Portfolio Manager proposes (README §9)."""

    core_shift: float
    satellite: Mapping[str, float]
    """Weights as fractions of equity."""


@dataclass(frozen=True, slots=True)
class PortfolioContext:
    state: Trend
    capped: bool
    halted: bool
    increases_allowed: bool
    """False under the kill switch, an `INCOMPLETE` session or a calendar disagreement."""
    current: Mapping[str, float]
    """The account's weights now, as fractions of equity."""
    previous_satellite: Mapping[str, float]
    """Last month's enforced satellite targets (empty before the first decision)."""
    equity: float
    lots: Mapping[str, Sequence[Lot]]
    """The account's open tax lots per fund."""
    prices: Mapping[str, float]
    """The decision prices; a fund without one is taxed as if its whole sale were gain."""
    satellite_tax_this_year: float
    """The tax (USD) the satellite's sales have realized this calendar year."""


@dataclass(frozen=True, slots=True)
class EnforcedTargets:
    rule_level: float
    level: float
    """The book's level: the rule level plus the enforced shift."""
    core_level: float
    """The core's own level, sized from ``level`` (#4)."""
    satellite: dict[str, float]
    targets: dict[str, float]
    """Fractions of equity per fund, summing to 1."""
    findings: tuple[Finding, ...]

    @property
    def outcome(self) -> Outcome:
        return outcome_of(self.findings)


def evaluate_decision(
    decision: PortfolioDecision, ctx: PortfolioContext, cfg: PortfolioConfig
) -> EnforcedTargets:
    findings: list[Finding] = []
    rule = rule_level(ctx.state, ctx.capped, ctx.halted, cfg)
    level = _level(decision.core_shift, ctx, rule, cfg, findings)
    satellite = _satellite(decision.satellite, cfg, findings)
    satellite = _hold(satellite, ctx.previous_satellite, cfg, findings)
    if ctx.state is Trend.UP and not ctx.capped and not ctx.halted:
        satellite = _tax_budget(satellite, ctx, cfg, findings)
    targets = book_targets(level, satellite, cfg)
    if not ctx.increases_allowed:
        targets = _no_increase(targets, ctx.current, cfg, findings)
    return EnforcedTargets(rule, level, core_level(level, cfg), satellite, targets, tuple(findings))


def _level(
    shift: float, ctx: PortfolioContext, rule: float, cfg: PortfolioConfig, out: list[Finding]
) -> float:
    if not math.isfinite(shift):
        out.append(reject(GuardrailRule.INVALID_INPUT, "core shift is not a number", str(shift)))
        return rule
    band = shift_band(ctx.state, ctx.capped, ctx.halted, rule, cfg)
    proposed = rule + shift
    enforced = min(max(proposed, band.low), band.high)
    if abs(enforced - proposed) > WEIGHT_TOLERANCE:
        message = f"level {proposed:.3f} outside [{band.low:.3f}, {band.high:.3f}]"
        out.append(clip(GuardrailRule.CORE_SHIFT, message, proposed, enforced))
    return enforced


def _satellite(
    proposed: Mapping[str, float], cfg: PortfolioConfig, out: list[Finding]
) -> dict[str, float]:
    allowed = set(cfg.satellite_list)
    weights: dict[str, float] = {}
    for symbol, weight in sorted(proposed.items()):
        cap = cfg.satellite_share if symbol == cfg.market else cfg.max_etf_weight
        if not math.isfinite(weight) or weight < 0:
            out.append(reject(GuardrailRule.INVALID_INPUT, f"{symbol} weight {weight}", weight))
        elif symbol not in allowed:
            out.append(reject(GuardrailRule.SATELLITE_LIST, f"{symbol} is not approved", symbol))
        elif weight > cap + WEIGHT_TOLERANCE:
            message = f"{symbol} above {cap:.0%} of equity"
            out.append(clip(GuardrailRule.SATELLITE_WEIGHT, message, weight, cap))
            weights[symbol] = cap
        elif weight > WEIGHT_TOLERANCE:
            weights[symbol] = weight
    total = math.fsum(weights.values())
    if total > cfg.satellite_share + WEIGHT_TOLERANCE:
        scale = cfg.satellite_share / total
        message = f"satellite {total:.3f} above {cfg.satellite_share:.3f}: scaled down"
        out.append(clip(GuardrailRule.SATELLITE_TOTAL, message, total, cfg.satellite_share))
        weights = {k: v * scale for k, v in weights.items()}
    return _with_cash(weights, cfg)


def _hold(
    proposed: dict[str, float],
    previous: Mapping[str, float],
    cfg: PortfolioConfig,
    out: list[Finding],
) -> dict[str, float]:
    """Small changes keep last month's target, so they aren't traded. Cash is the remainder."""
    weights = {k: v for k, v in proposed.items() if k != cfg.cash}
    held: set[str] = set()
    for symbol in sorted(set(weights) | set(previous)):
        if symbol == cfg.cash:
            continue
        before, after = previous.get(symbol, 0.0), weights.get(symbol, 0.0)
        change = abs(after - before)
        if after <= WEIGHT_TOLERANCE or change <= WEIGHT_TOLERANCE:
            continue  # an exit, or no change
        if change >= cfg.satellite_min_change:
            continue  # a change worth its tax
        message = f"{symbol} {after:.3f} within {cfg.satellite_min_change:.0%} of {before:.3f}"
        out.append(clip(GuardrailRule.SATELLITE_HOLD, message, after, before))
        weights[symbol] = before
        held.add(symbol)
    excess = math.fsum(weights.values()) - cfg.satellite_share
    if excess > WEIGHT_TOLERANCE:
        # Holding raised the total: the changed funds give it back (they always can, since
        # last month's targets fit the share).
        changed = math.fsum(v for k, v in weights.items() if k not in held)
        scale = (changed - excess) / changed
        weights = {k: v if k in held else v * scale for k, v in weights.items()}
    return _with_cash(weights, cfg)


def _tax_budget(
    proposed: dict[str, float], ctx: PortfolioContext, cfg: PortfolioConfig, out: list[Finding]
) -> dict[str, float]:
    room = max(cfg.satellite_tax_budget * ctx.equity - ctx.satellite_tax_this_year, 0.0)
    previous = ctx.previous_satellite
    symbols = sorted(set(proposed) | set(previous))

    def toward(k: float) -> dict[str, float]:
        """``k`` of the way from last month's targets to the proposal."""
        return {
            s: previous.get(s, 0.0) + k * (proposed.get(s, 0.0) - previous.get(s, 0.0))
            for s in symbols
        }

    def tax(weights: Mapping[str, float]) -> float:
        return satellite_tax(weights, ctx, cfg)

    wanted = tax(proposed)
    if wanted <= room:
        return proposed
    low, high = 0.0, 1.0  # tax(toward(0)) is 0: nothing is sold
    for _ in range(TAX_SEARCH_STEPS):
        mid = (low + high) / 2
        low, high = (mid, high) if tax(toward(mid)) <= room else (low, mid)
    message = f"satellite sales would realize ${wanted:,.0f} tax, ${room:,.0f} left this year"
    out.append(clip(GuardrailRule.TAX_BUDGET, message, round(wanted, 2), round(room, 2)))
    return {k: v for k, v in toward(low).items() if v > WEIGHT_TOLERANCE}


def satellite_tax(
    satellite: Mapping[str, float], ctx: PortfolioContext, cfg: PortfolioConfig
) -> float:
    """The tax (USD) the satellite's sales from last month's targets to ``satellite`` would
    realize: 25% of their net FIFO gains at the decision prices; a fund without a price counts
    its whole sale as gain (#4)."""
    previous = ctx.previous_satellite
    gain = 0.0
    for symbol in sorted(set(satellite) | set(previous)):
        sold = (previous.get(symbol, 0.0) - satellite.get(symbol, 0.0)) * ctx.equity
        if sold <= 0:
            continue
        price = ctx.prices.get(symbol, 0.0)
        if price > 0:
            gain += fifo_gain(ctx.lots.get(symbol, ()), sold / price, price)
        else:
            gain += sold
    return cfg.tax_rate * max(gain, 0.0)


def _with_cash(weights: dict[str, float], cfg: PortfolioConfig) -> dict[str, float]:
    """The satellite's remainder in cash."""
    out = {k: v for k, v in weights.items() if k != cfg.cash and v > WEIGHT_TOLERANCE}
    left = cfg.satellite_share - math.fsum(out.values())
    if left > WEIGHT_TOLERANCE:
        out[cfg.cash] = left
    return out


def _no_increase(
    targets: dict[str, float],
    current: Mapping[str, float],
    cfg: PortfolioConfig,
    out: list[Finding],
) -> dict[str, float]:
    held = dict(targets)
    for symbol in sorted(targets):
        now = current.get(symbol, 0.0)
        if symbol != cfg.cash and targets[symbol] > now + WEIGHT_TOLERANCE:
            message = f"{symbol} held at {now:.3f}: increases are blocked"
            out.append(clip(GuardrailRule.NO_INCREASE, message, targets[symbol], now))
            held[cfg.cash] = held.get(cfg.cash, 0.0) + targets[symbol] - now
            held[symbol] = now
    return {k: v for k, v in held.items() if v > WEIGHT_TOLERANCE}
