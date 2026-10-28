"""The portfolio's funds in the order layer (README §5.6, §7.2; #155; task 5.1).

- **Which funds carry a stop:** only the leveraged fund. The others (the market fund, cash and
  the satellite's list) have none; the trend rule and the halt are their exits, so the stop
  invariant and `stop_guard` leave them alone.
- **The leveraged fund's stop level** (`fund_stops`): set when it is bought from flat, raised on
  each later E, never lowered; the latest row is in force.
- **Splits** on a held fund (#116): on the ex-date Alpaca rejects resting stops and paper never
  adjusts the position. The leveraged fund's level is scaled by the ratio (new shares per old),
  so the invariant re-places its stop there rather than reading the halved price as a fall
  through the old level; each split opens a mismatch, resolved by hand, and until then the fund
  is neither sold at once nor bought.

The funds' holdings are the broker's positions (`execution.reconcile`); their tax lots are
`journal.tax_lots`' (task 5.3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, select, update
from sqlalchemy.dialects.postgresql import insert

from auratrade.config import PortfolioConfig
from auratrade.db.models import CorporateAction, FundStop
from auratrade.execution import store
from auratrade.execution.broker import BrokerPosition
from auratrade.execution.prices import round_up

SPLIT_TYPES = ("SPLIT", "REVERSE_SPLIT")


def funds(cfg: PortfolioConfig) -> frozenset[str]:
    return frozenset({cfg.market, cfg.leveraged, cfg.cash, *cfg.satellite_list})


def unstopped(cfg: PortfolioConfig) -> frozenset[str]:
    """The funds that carry no stop (#155)."""
    return funds(cfg) - {cfg.leveraged}


@dataclass(frozen=True, slots=True)
class StopLevel:
    symbol: str
    level: float
    session: date
    change: str


def stop_level(conn: Connection, symbol: str) -> StopLevel | None:
    row = conn.execute(
        select(FundStop.level, FundStop.session_date, FundStop.change)
        .where(FundStop.symbol == symbol)
        .order_by(FundStop.id.desc())
        .limit(1)
    ).first()
    return None if row is None else StopLevel(symbol, row.level, row.session_date, row.change)


def write_stop_level(
    conn: Connection,
    symbol: str,
    level: float,
    *,
    session: date,
    change: str,
    price: float | None = None,
    details: Mapping[str, Any] | None = None,
) -> float:
    """Record a new level; the one in force afterwards. A RAISE below the current level keeps
    the current one (tighten-only); ENTRY (bought from flat) and SPLIT set it."""
    current = stop_level(conn, symbol)
    if change == "RAISE" and current is not None and level <= current.level:
        return current.level
    conn.execute(
        insert(FundStop).values(
            symbol=symbol,
            session_date=session,
            level=level,
            change=change,
            decision_price=price,
            details=dict(details or {}),
        )
    )
    return level


def levels(conn: Connection, cfg: PortfolioConfig, held: Iterable[str]) -> dict[str, float]:
    """The leveraged fund's level, if it is held and has one."""
    if cfg.leveraged not in set(held):
        return {}
    found = stop_level(conn, cfg.leveraged)
    return {} if found is None else {cfg.leveraged: found.level}


def mismatched(conn: Connection, cfg: PortfolioConfig) -> frozenset[str]:
    """Funds with an open mismatch (a split, a merger): not bought, not sold at once."""
    return frozenset(s for _, s in store.open_mismatches(conn) if s in funds(cfg))


def apply_splits(
    conn: Connection,
    cfg: PortfolioConfig,
    held: Mapping[str, BrokerPosition],
    *,
    session: date,
    now: datetime,
) -> list[str]:
    """Handle the splits of held funds with an ex-date through `session`; the alerts."""
    symbols = sorted(funds(cfg) & set(held))
    if not symbols:
        return []
    actions = store.entities(
        conn,
        select(CorporateAction)
        .where(
            CorporateAction.symbol.in_(symbols),
            CorporateAction.action_type.in_(SPLIT_TYPES),
            CorporateAction.ex_date <= session,
            CorporateAction.handled.is_(False),
        )
        .order_by(CorporateAction.ex_date),
    )
    alerts: list[str] = []
    for action in actions:
        if not action.ratio:
            continue
        text = f"split {action.ratio:g}:1 in {action.symbol} (ex {action.ex_date})"
        if action.symbol == cfg.leveraged:
            current = stop_level(conn, action.symbol)
            if current is not None and current.session < action.ex_date:
                scaled = round_up(current.level / action.ratio)
                write_stop_level(
                    conn,
                    action.symbol,
                    scaled,
                    session=session,
                    change="SPLIT",
                    details={"ratio": action.ratio, "from": current.level},
                )
                text += f": its stop level scaled {current.level:g} -> {scaled:g}"
        conn.execute(
            update(CorporateAction)
            .where(CorporateAction.id == action.id)
            .values(handled=True, handled_at=now)
        )
        broker_qty = held[action.symbol].qty
        store.record_mismatch(
            conn,
            store.CORPORATE_ACTION,
            action.symbol,
            {
                "ratio": action.ratio,
                "ex_date": action.ex_date.isoformat(),
                "broker_qty": broker_qty,
                "expected_qty": int(broker_qty * action.ratio),
            },
        )
        alerts.append(
            f"{text}; the broker shows {broker_qty} shares, {int(broker_qty * action.ratio)} "
            "expected. Paper doesn't apply splits (#116): no buys or sales at once in it until "
            "resolved by hand (auratrade resolve)."
        )
    return alerts
