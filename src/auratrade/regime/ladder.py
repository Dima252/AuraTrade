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
