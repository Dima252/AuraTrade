"""`system_flags`: the kill switch and the halts (README §5.13).

They block entries and adds only; nothing here may touch stops or plan exits.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, select

from auratrade.db.models import SystemFlag

TRADING_ENABLED = "trading_enabled"
HALT_DAILY_LOSS = "halt_daily_loss"
HALT_WEEKLY_LOSS = "halt_weekly_loss"
HALT_DRAWDOWN = "halt_drawdown"

# The initial migration seeds these. A fresh database starts with entries disabled, so a
# new environment trades only after someone turns the kill switch off by hand.
SEED: dict[str, bool] = {
    TRADING_ENABLED: False,
    HALT_DAILY_LOSS: False,
    HALT_WEEKLY_LOSS: False,
    HALT_DRAWDOWN: False,
}


@dataclass(frozen=True)
class SystemFlags:
    trading_enabled: bool
    halt_daily_loss: bool
    halt_weekly_loss: bool
    halt_drawdown: bool

    @property
    def entries_allowed(self) -> bool:
        return self.trading_enabled and not (
            self.halt_daily_loss or self.halt_weekly_loss or self.halt_drawdown
        )


def read_flags(conn: Connection) -> SystemFlags:
    """Missing rows read as the restrictive value (fail closed)."""
    rows: dict[str, bool] = dict(conn.execute(select(SystemFlag.name, SystemFlag.value)).all())
    return SystemFlags(
        trading_enabled=rows.get(TRADING_ENABLED, False),
        halt_daily_loss=rows.get(HALT_DAILY_LOSS, True),
        halt_weekly_loss=rows.get(HALT_WEEKLY_LOSS, True),
        halt_drawdown=rows.get(HALT_DRAWDOWN, True),
    )
