"""The live drawdown halt (README §7.3; task 5.2), on the account the order layer trades.

The state is the `halt_drawdown` flag in `system_flags`, with its running peak (and the session
it turned on) in the flag's details, moved only by `core_allocation`'s pure functions, the same
ones the twin and the backtest run:
- **On** at `post_market`'s check at each close: equity at most (1 - ``drawdown_halt``) of the
  peak (`halt_at_close`). `execute_close` then sells the leveraged fund into the market fund at
  15:40 on the next session (`execution.portfolio.halt_sale`).
- **Off** at `allocate` on R: on a DOWN -> UP flip (the peak reset to equity) or with equity back
  at its old peak (`halt_on_ranking`).

Without a stored peak the halt starts from the equity it is first given (`start_halt`). In
dry_run the account is the simulated book, whose halt is simulated (#166): the check is
reported and nothing is written.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert

from auratrade.config import PortfolioConfig
from auratrade.db.flags import HALT_DRAWDOWN
from auratrade.db.models import AccountSnapshot, SystemFlag
from auratrade.strategies.core_allocation import Halt, halt_at_close, halt_on_ranking, start_halt


def load(conn: Connection, equity: float) -> Halt:
    """The stored halt; one starting at `equity` when no peak is stored. A missing flag row
    reads as on (fail closed, as `read_flags`)."""
    row = conn.execute(
        select(SystemFlag.value, SystemFlag.details).where(SystemFlag.name == HALT_DRAWDOWN)
    ).first()
    if row is None:
        return Halt(True, equity)
    peak = (row.details or {}).get("peak")
    if peak is None:
        return Halt(True, equity) if row.value else start_halt(equity)
    return Halt(bool(row.value), float(peak))


def save(conn: Connection, halt: Halt, *, session: date, by: str, since: date | None) -> None:
    details: dict[str, Any] = {"peak": halt.peak, "session": session.isoformat()}
    if halt.on and since is not None:
        details["since"] = since.isoformat()
    statement = insert(SystemFlag).values(
        name=HALT_DRAWDOWN, value=halt.on, details=details, updated_by=by
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[SystemFlag.name],
            set_={
                "value": statement.excluded.value,
                "details": statement.excluded.details,
                "updated_by": statement.excluded.updated_by,
                "updated_at": func.now(),
            },
        )
    )


def _since(conn: Connection) -> date | None:
    details = conn.scalar(select(SystemFlag.details).where(SystemFlag.name == HALT_DRAWDOWN))
    since = (details or {}).get("since")
    return None if since is None else date.fromisoformat(since)


def check_at_close(
    conn: Connection,
    session: date,
    equity: float,
    cfg: PortfolioConfig,
    *,
    write: bool,
) -> dict[str, Any]:
    """`post_market`'s check: the halt after `session`'s close at `equity`."""
    before = load(conn, equity)
    after, turned_on = halt_at_close(before, equity, cfg)
    if write:
        since = session if turned_on else _since(conn)
        save(conn, after, session=session, by="post_market", since=since)
    return {
        "on": after.on,
        "turned_on": turned_on,
        "peak": round(after.peak, 2),
        "drawdown": round(max(0.0, 1 - equity / after.peak), 4) if after.peak > 0 else 0.0,
        "written": write,
    }


def check_on_ranking(
    conn: Connection,
    ranking: date,
    flipped_up: bool,
    cfg: PortfolioConfig,
    *,
    write: bool,
) -> dict[str, Any]:
    """`allocate`'s check on R, at R's equity in `account_snapshots` (`post_market` wrote it).
    Without it the halt stays as it is (fail closed)."""
    equity = conn.scalar(
        select(AccountSnapshot.equity).where(AccountSnapshot.session_date == ranking)
    )
    if equity is None:
        current = load(conn, 0.0)
        return {"on": current.on, "turned_off": False, "skipped": f"no snapshot on {ranking}"}
    before = load(conn, equity)
    after = halt_on_ranking(before, flipped_up, equity)
    turned_off = before.on and not after.on
    if write and turned_off:
        save(conn, after, session=ranking, by="allocate", since=None)
    return {
        "on": after.on,
        "turned_off": turned_off,
        "peak": round(after.peak, 2),
        "written": write and turned_off,
    }


def reset(conn: Connection, session: date, equity: float, *, by: str) -> Halt:
    """`auratrade halt reset`: off, with the peak at `equity` (after a paper account reset)."""
    halt = start_halt(equity)
    save(conn, halt, session=session, by=by, since=None)
    return halt
