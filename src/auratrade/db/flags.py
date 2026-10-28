"""`system_flags`: the kill switch and the drawdown halt (README §5.9).

They block increases of exposure only; nothing here may touch a protective stop or a sale toward
a lower target.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, select

from auratrade.db.models import SystemFlag

TRADING_ENABLED = "trading_enabled"
HALT_DRAWDOWN = "halt_drawdown"

# The migrations seed these. A fresh database starts with the kill switch on, so a new
# environment trades only after someone turns it off by hand.
SEED: dict[str, bool] = {
    TRADING_ENABLED: False,
    HALT_DRAWDOWN: False,
}


@dataclass(frozen=True)
class SystemFlags:
    trading_enabled: bool
    halt_drawdown: bool


def read_flags(conn: Connection) -> SystemFlags:
    """Missing rows read as the restrictive value (fail closed)."""
    rows: dict[str, bool] = dict(conn.execute(select(SystemFlag.name, SystemFlag.value)).all())
    return SystemFlags(
        trading_enabled=rows.get(TRADING_ENABLED, False),
        halt_drawdown=rows.get(HALT_DRAWDOWN, True),
    )
