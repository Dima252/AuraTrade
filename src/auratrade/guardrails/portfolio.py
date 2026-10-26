"""The portfolio's guardrails (README §8; #155; task 3.19), pure.

They run on every Portfolio Manager decision before its targets reach `portfolio_targets`:
- **The core:** the rule level from code (`strategies/core_allocation.py`) plus the LLM's shift,
  clipped into the band: ± ``max_shift`` within [``min_level``, ``max_level``], and only downward
  while the trend is DOWN, the volatility cap or the drawdown halt is on. A shift that isn't a
  finite number is rejected (no shift).
- **The satellite:** only funds on the approved list (so never a leveraged or inverse one); each
  weight a finite fraction no larger than ``max_etf_weight`` (clipped); together no more than
  ``satellite_share`` (scaled down). What is left of the satellite goes to cash.
- **No increase** (the kill switch, an incomplete session, a calendar disagreement): no fund's
  target may rise above its current weight, except cash; the excess goes to cash. Sales toward
  a lower target are never blocked (CLAUDE.md §3.5).

The LLM never names a price; the targets are weights, and code prices every order.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

from auratrade.config import PortfolioConfig
from auratrade.guardrails.model import Finding, GuardrailRule, Outcome, clip, outcome_of, reject
from auratrade.strategies.core_allocation import (
    WEIGHT_TOLERANCE,
    Trend,
    book_targets,
    rule_level,
    shift_band,
)


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


@dataclass(frozen=True, slots=True)
class EnforcedTargets:
    rule_level: float
    core_level: float
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
    level = rule_level(ctx.state, ctx.capped, ctx.halted, cfg)
    core = _core_level(decision.core_shift, ctx, level, cfg, findings)
    satellite = _satellite(decision.satellite, cfg, findings)
    targets = book_targets(core, satellite, cfg)
    if not ctx.increases_allowed:
        targets = _no_increase(targets, ctx.current, cfg, findings)
    return EnforcedTargets(level, core, satellite, targets, tuple(findings))


def _core_level(
    shift: float, ctx: PortfolioContext, level: float, cfg: PortfolioConfig, out: list[Finding]
) -> float:
    if not math.isfinite(shift):
        out.append(reject(GuardrailRule.INVALID_INPUT, "core shift is not a number", str(shift)))
        return level
    band = shift_band(ctx.state, ctx.capped, ctx.halted, level, cfg)
    proposed = level + shift
    enforced = min(max(proposed, band.low), band.high)
    if abs(enforced - proposed) > WEIGHT_TOLERANCE:
        message = f"core level {proposed:.3f} outside [{band.low:.3f}, {band.high:.3f}]"
        out.append(clip(GuardrailRule.CORE_SHIFT, message, proposed, enforced))
    return enforced


def _satellite(
    proposed: Mapping[str, float], cfg: PortfolioConfig, out: list[Finding]
) -> dict[str, float]:
    allowed = set(cfg.satellite_list)
    weights: dict[str, float] = {}
    for symbol, weight in sorted(proposed.items()):
        if not math.isfinite(weight) or weight < 0:
            out.append(reject(GuardrailRule.INVALID_INPUT, f"{symbol} weight {weight}", weight))
        elif symbol not in allowed:
            out.append(reject(GuardrailRule.SATELLITE_LIST, f"{symbol} is not approved", symbol))
        elif weight > cfg.max_etf_weight + WEIGHT_TOLERANCE:
            message = f"{symbol} above {cfg.max_etf_weight:.0%} of equity"
            out.append(clip(GuardrailRule.SATELLITE_WEIGHT, message, weight, cfg.max_etf_weight))
            weights[symbol] = cfg.max_etf_weight
        elif weight > WEIGHT_TOLERANCE:
            weights[symbol] = weight
    total = math.fsum(weights.values())
    if total > cfg.satellite_share + WEIGHT_TOLERANCE:
        scale = cfg.satellite_share / total
        message = f"satellite {total:.3f} above {cfg.satellite_share:.3f}: scaled down"
        out.append(clip(GuardrailRule.SATELLITE_TOTAL, message, total, cfg.satellite_share))
        weights = {k: v * scale for k, v in weights.items()}
        total = cfg.satellite_share
    left = cfg.satellite_share - total
    if left > WEIGHT_TOLERANCE:
        weights[cfg.cash] = weights.get(cfg.cash, 0.0) + left
    return weights


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
