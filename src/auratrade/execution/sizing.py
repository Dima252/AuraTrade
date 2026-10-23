"""Position sizing in whole shares (README §7.1, §8, #122), pure.

- **Planned size** is sized on the worst fill (the limit price): the approved risk amount over
  (limit - stop), clipped by the max position size and the exposure reserved for the entry,
  floored to whole shares (a GTC stop can't be fractional). 0 shares -> skipped.
- **Tranches:** a two-tranche entry buys ``tranche1_fraction`` (floored) at the trigger and the
  rest on confirmation; a planned size under ``min_two_tranche_shares`` goes in as one tranche.
- **Tranche 2** is re-sized on its own worst fill and tranche 1's actual (possibly partial) fill,
  so the total risk stays within the plan and the total size within the reservation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from auratrade.config import EntriesConfig

# Float noise in risk / width must not floor 10.0 shares to 9.
_SHARE_TOLERANCE = 1e-9


def _floor(value: float) -> int:
    return max(math.floor(value + _SHARE_TOLERANCE), 0)


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def planned_shares(
    *,
    risk_amount: float,
    limit: float,
    stop: float,
    max_position_value: float,
    exposure_room: float,
) -> int:
    """Whole shares risking at most ``risk_amount`` on a fill at the limit, within the max
    position value and the exposure left; 0 when nothing fits or the inputs are invalid."""
    if not _finite(risk_amount, limit, stop, max_position_value, exposure_room):
        return 0
    per_share = limit - stop
    if per_share <= 0 or stop <= 0 or risk_amount <= 0:
        return 0
    value_cap = min(max_position_value, exposure_room)
    return min(_floor(risk_amount / per_share), _floor(value_cap / limit))


@dataclass(frozen=True, slots=True)
class Tranches:
    first: int
    second: int
    """0 for a single-tranche entry."""

    @property
    def total(self) -> int:
        return self.first + self.second


def split_tranches(shares: int, tranches: int, config: EntriesConfig) -> Tranches:
    """Tranche 1 at the trigger and tranche 2 on confirmation; a small size goes in as one."""
    if shares < 0:
        raise ValueError(f"shares must be >= 0, got {shares}")
    if tranches == 1 or shares < config.min_two_tranche_shares:
        return Tranches(first=shares, second=0)
    first = max(_floor(shares * config.tranche1_fraction), 1)
    return Tranches(first=first, second=shares - first)


def tranche2_shares(
    *,
    planned_risk: float,
    planned_shares: int,
    filled_shares: int,
    fill_price: float,
    limit: float,
    stop: float,
) -> int:
    """Tranche 2 on tranche 1's actual fill: the shares that keep the total risk (to the hard
    stop) within ``planned_risk`` on a fill at ``limit``, and the total within the planned size.
    """
    if not _finite(planned_risk, fill_price, limit, stop) or filled_shares < 0:
        return 0
    per_share = limit - stop
    if per_share <= 0 or stop <= 0:
        return 0
    risk_left = planned_risk - filled_shares * max(fill_price - stop, 0.0)
    if risk_left <= 0:
        return 0
    return min(_floor(risk_left / per_share), max(planned_shares - filled_shares, 0))
