"""Price levels, stop width, keep losses small vs gains, and the 50-day limit (archived README §7.1,
§8).

**Price levels come from code.** The LLM may nudge the pivot within ±3% of the code pivot and
may only tighten the stop: at or above the structural stop, and at least 0.5 x ATR below the
pivot. The 5% max chase and the limit are always measured from the **code** pivot
(``execution.triggers``), so a nudged pivot can't extend them.

**Stop width** is measured from the limit price (the worst fill) for the percentage caps and from
the trigger for the ATR and ADR caps (archived README §7.1). After ``keep_losses_small_trades``
closed trades of a strategy, its max width is also capped at ½ x its rolling average gain, measured
from the same reference; if that leaves less than the strategy's noise floor, the entry is rejected
rather than squeezing the stop into normal noise.
"""

from dataclasses import dataclass
from typing import assert_never

from auratrade.config import GuardrailsConfig, StopWidthRule, StopWidthUnit, Strategy
from auratrade.guardrails.model import Finding, GuardrailRule, finite_positive, reject
from auratrade.plans.model import reached

# Momentum and EPs may run far above the 50-day (archived README §5.3).
EXTENSION_EXEMPT = frozenset({Strategy.MOMENTUM_BREAKOUT, Strategy.EPISODIC_PIVOT})


def price_levels(
    *,
    pivot: float,
    stop: float | None,
    code_pivot: float,
    structural_stop: float,
    atr: float | None,
    config: GuardrailsConfig,
) -> Finding | None:
    """The LLM's pivot and stop against the code's (archived README §8 "Price levels", "Stop on
    every entry")."""
    if stop is None:
        return reject(GuardrailRule.STOP_REQUIRED, "every entry needs a stop")
    if atr is None or not finite_positive(pivot, stop, code_pivot, structural_stop, atr):
        return reject(GuardrailRule.INVALID_INPUT, "levels must be finite and positive", pivot)
    off = abs(pivot / code_pivot - 1)
    if not reached(config.pivot_tolerance, off):
        return reject(
            GuardrailRule.PRICE_LEVELS,
            f"pivot {pivot:.2f} is {off:.1%} from the code pivot {code_pivot:.2f}",
            pivot,
        )
    if not reached(stop, structural_stop):
        return reject(
            GuardrailRule.PRICE_LEVELS,
            f"stop {stop:.2f} is below the structural stop {structural_stop:.2f} (tighter only)",
            stop,
        )
    if not reached(pivot - stop, config.min_stop_below_pivot_atr * atr):
        return reject(
            GuardrailRule.PRICE_LEVELS,
            f"stop {stop:.2f} is less than {config.min_stop_below_pivot_atr} x ATR below the pivot",
            stop,
        )
    return None


@dataclass(frozen=True, slots=True)
class StrategyRecord:
    """A strategy's closed trades so far, for "keep losses small vs gains"
    (``journal.numbers``)."""

    closed_trades: int
    average_gain: float | None
    """The average winning trade's gain (a fraction) over its last 20 closed trades; None
    without a winner."""


def max_stop_width(rule: StopWidthRule, *, reference: float, atr: float, adr: float) -> float:
    """The default max distance from the reference price down to the stop."""
    match rule.unit:
        case StopWidthUnit.LIMIT_PCT:
            return rule.max * reference
        case StopWidthUnit.ATR:
            return rule.max * atr
        case StopWidthUnit.ADR:
            return rule.max * adr * reference
        case _:  # pragma: no cover - every unit is handled above
            assert_never(rule.unit)


def stop_width(
    strategy: Strategy,
    *,
    trigger: float,
    limit: float,
    stop: float,
    atr: float | None,
    adr: float | None,
    record: StrategyRecord | None,
    config: GuardrailsConfig,
) -> Finding | None:
    """Archived README §8 "Max stop width" and "Keep losses small vs gains". ``adr`` is a fraction
    (an EP's pre-gap ADR)."""
    if atr is None or adr is None or not finite_positive(trigger, limit, stop, atr, adr):
        return reject(GuardrailRule.INVALID_INPUT, "levels must be finite and positive", stop)
    if stop >= trigger:
        return reject(GuardrailRule.STOP_WIDTH, f"stop {stop:.2f} is not below the trigger", stop)
    rule = config.stop_widths[strategy]
    reference = limit if rule.unit is StopWidthUnit.LIMIT_PCT else trigger
    width = max_stop_width(rule, reference=reference, atr=atr, adr=adr)
    capped_by_gains = False
    if record is not None and record.closed_trades >= config.keep_losses_small_trades:
        # No winner in the window reads as an average gain of 0: the cap closes (fail closed).
        gain_cap = config.keep_losses_small_gain_share * max(record.average_gain or 0.0, 0.0)
        if gain_cap * reference < width:
            width, capped_by_gains = gain_cap * reference, True
        if not reached(width, rule.noise_floor_atr * atr):
            return reject(
                GuardrailRule.KEEP_LOSSES_SMALL,
                f"max stop width {width:.2f} is below the noise floor "
                f"{rule.noise_floor_atr} x ATR ({rule.noise_floor_atr * atr:.2f})",
                stop,
            )
    if not reached(width, reference - stop):
        return reject(
            GuardrailRule.KEEP_LOSSES_SMALL if capped_by_gains else GuardrailRule.STOP_WIDTH,
            f"stop {stop:.2f} is {reference - stop:.2f} below {reference:.2f}; "
            f"the max is {width:.2f}",
            stop,
        )
    return None


def extension(
    strategy: Strategy, *, price: float | None, sma_50: float | None, config: GuardrailsConfig
) -> Finding | None:
    """Never more than 25% above the 50-day (momentum and EPs exempt). An unknown 50-day
    fails closed."""
    if strategy in EXTENSION_EXEMPT:
        return None
    if price is None or sma_50 is None or not finite_positive(price, sma_50):
        return reject(GuardrailRule.EXTENSION, "no price or 50-day to check the extension")
    if not reached(sma_50 * (1 + config.max_above_50d), price):
        return reject(
            GuardrailRule.EXTENSION,
            f"price {price:.2f} is more than {config.max_above_50d:.0%} above the 50-day "
            f"{sma_50:.2f}",
            price,
        )
    return None
