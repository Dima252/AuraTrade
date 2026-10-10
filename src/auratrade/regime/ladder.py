"""The conservatism ladder (README §5.2, decision #69).

``CORRECTION`` < ``EARLY_UPTREND`` < ``UNDER_PRESSURE`` < ``CONFIRMED_UPTREND``, by the risk
each state allows (§6). "More conservative" always means lower on this ladder. Only the state
machine assigns ``EARLY_UPTREND``: it needs a follow-through day.
"""

from auratrade.config import Regime

LADDER: tuple[Regime, ...] = (
    Regime.CORRECTION,
    Regime.EARLY_UPTREND,
    Regime.UNDER_PRESSURE,
    Regime.CONFIRMED_UPTREND,
)
_RANK = {regime: i for i, regime in enumerate(LADDER)}


def rank(regime: Regime) -> int:
    return _RANK[regime]


def most_conservative(*regimes: Regime) -> Regime:
    if not regimes:
        raise ValueError("need at least one regime")
    return min(regimes, key=rank)


def downgrade(current: Regime, proposed: Regime | None) -> Regime:
    """The regime after an outside view that may only lower it: the LLM view in the Portfolio
    Manager, or the ``premarket_check`` downgrade.

    ``None`` (no view) keeps ``current``. A proposed ``EARLY_UPTREND`` is valid only when the
    state machine already computed it; anywhere else it is invalid, and an invalid view fails
    closed to ``CORRECTION``.
    """
    if proposed is None:
        return current
    if proposed is Regime.EARLY_UPTREND and current is not Regime.EARLY_UPTREND:
        return Regime.CORRECTION
    return most_conservative(current, proposed)
