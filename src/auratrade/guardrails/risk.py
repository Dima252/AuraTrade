"""Risk per trade, heat, positions and exposure (README §6, §8).

- **Effective risk** = the regime's base risk x the progressive-exposure multiplier x the
  late-stage factor, hard cap 1.5%. `EARLY_UPTREND` steps up after its starter sessions only on
  sessions with a score >= +1, never after a `no_ftd` entry (``regime.state.early_uptrend_stepped``
  decides; the caller passes the result).
- **Base count:** a 3rd-stage base, or an unreliable count, halves the risk; a 4th or later is
  not fundable.
- **Heat:** open risk (Σ max(0, entry - hard stop) x shares, plus pending entries' planned risk)
  stays within 6% of equity. A new entry is clipped to the room left and rejected only when the
  room is under half its planned risk.
- **Exposure** reserves a two-tranche entry's full planned size; total exposure stays within the
  regime's limit and, with pending entries at their limit price, within equity (no margin).
"""

from dataclasses import dataclass

from auratrade.config import FundedStrategy, GuardrailsConfig, Regime, RegimesConfig, Strategy
from auratrade.guardrails.model import Finding, GuardrailRule, clip, reject
from auratrade.plans.model import reached


@dataclass(frozen=True, slots=True)
class RegimeLimits:
    base_risk: float
    max_exposure: float
    """A fraction of equity."""
    max_positions: int
    strategies: tuple[FundedStrategy, ...]


def regime_limits(
    regime: Regime, *, stepped: bool, equity: float, config: RegimesConfig
) -> RegimeLimits:
    """The README §6 row for ``regime``; ``stepped`` applies the `EARLY_UPTREND` step-up."""
    row = getattr(config, regime.value)
    base_risk, max_exposure = row.base_risk, row.max_exposure
    if regime is Regime.EARLY_UPTREND and stepped:
        base_risk = config.early_uptrend_step.stepped_base_risk
        max_exposure = config.early_uptrend_step.stepped_max_exposure
    max_positions = row.max_positions
    if row.max_positions_large_account is not None and equity >= config.large_account_equity:
        max_positions = row.max_positions_large_account
    return RegimeLimits(base_risk, max_exposure, max_positions, row.strategies)


def fundable(
    strategy: Strategy, conviction: int | None, limits: RegimeLimits, regime: Regime
) -> Finding | None:
    """The strategy is funded in the regime and the conviction meets its bar (``None``
    conviction never does)."""
    for funded in limits.strategies:
        if funded.strategy is strategy:
            if funded.min_conviction is None or (
                conviction is not None and conviction >= funded.min_conviction
            ):
                return None
            return reject(
                GuardrailRule.FUNDABLE,
                f"{strategy} needs conviction >= {funded.min_conviction} in {regime}",
                conviction,
            )
    return reject(GuardrailRule.FUNDABLE, f"{strategy} is not fundable in {regime}")


def base_count_factor(
    stage: int | None, reliable: bool, config: GuardrailsConfig
) -> float | Finding:
    """The late-stage risk factor, or a rejection for a base past ``max_base_count``.
    No base (``None``) is not a late stage."""
    if stage is None:
        return 1.0
    if stage > config.max_base_count:
        return reject(GuardrailRule.BASE_COUNT, f"a stage-{stage} base is not fundable", stage)
    if stage >= config.late_stage_base_count or not reliable:
        return config.late_stage_risk_factor
    return 1.0


def effective_risk(
    base_risk: float, multiplier: float, late_stage_factor: float, config: GuardrailsConfig
) -> float:
    return min(base_risk * multiplier * late_stage_factor, config.max_risk_per_trade)


def risk_per_trade(requested: float | None, allowed: float) -> tuple[float, Finding | None]:
    """The requested risk clipped to the effective risk; no request takes the effective risk."""
    if requested is None or requested <= allowed:
        return (allowed if requested is None else requested), None
    return allowed, clip(
        GuardrailRule.MAX_RISK, f"risk clipped to {allowed:.4%}", requested, allowed
    )


@dataclass(frozen=True, slots=True)
class OpenRisk:
    """One held position's contribution to heat and exposure."""

    shares: int
    entry_price: float
    """Average entry price."""
    hard_stop: float
    price: float
    """The latest price (exposure at market)."""

    @property
    def risk(self) -> float:
        """Stops at breakeven or higher add nothing."""
        return max(0.0, self.entry_price - self.hard_stop) * self.shares

    @property
    def exposure(self) -> float:
        return self.price * self.shares


def heat_room(
    risk_amount: float, *, open_heat: float, equity: float, config: GuardrailsConfig
) -> tuple[float, Finding | None]:
    """The risk amount to approve: ``risk_amount``, clipped to the heat room left, or 0 and a
    rejection when the room is under ``heat_min_room_of_planned`` of it."""
    room = config.max_heat * equity - open_heat
    if reached(room, risk_amount):
        return risk_amount, None
    if room > 0 and reached(room, config.heat_min_room_of_planned * risk_amount):
        return room, clip(
            GuardrailRule.HEAT,
            f"risk clipped to the heat room left ({room:.2f})",
            risk_amount,
            room,
        )
    return 0.0, reject(
        GuardrailRule.HEAT,
        f"heat room {max(room, 0.0):.2f} is under {config.heat_min_room_of_planned:.0%} "
        f"of the planned risk {risk_amount:.2f}",
        risk_amount,
    )


def positions_check(open_positions: int, limits: RegimeLimits, regime: Regime) -> Finding | None:
    """Held positions plus pending and already approved entries, before this one."""
    if open_positions >= limits.max_positions:
        return reject(
            GuardrailRule.MAX_POSITIONS,
            f"{open_positions} positions already open or pending; {regime} allows "
            f"{limits.max_positions}",
            open_positions + 1,
        )
    return None


def exposure_check(
    new_exposure: float, *, open_exposure: float, equity: float, limits: RegimeLimits
) -> Finding | None:
    """No margin (the total within equity), then the regime's max exposure."""
    total = open_exposure + new_exposure
    if not reached(equity, total):
        return reject(GuardrailRule.NO_MARGIN, f"exposure {total:.2f} would use margin", total)
    if not reached(limits.max_exposure * equity, total):
        return reject(
            GuardrailRule.MAX_EXPOSURE,
            f"exposure {total:.2f} would pass {limits.max_exposure:.0%} of equity",
            total,
        )
    return None
