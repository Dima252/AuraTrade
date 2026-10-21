"""The account snapshot and the Telegram daily report written by `post_market` (README §5.10)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert

from auratrade.db.models import AccountSnapshot, TradeJournalRow
from auratrade.execution.broker import Account, BrokerPosition
from auratrade.execution.store import entities


@dataclass(frozen=True, slots=True)
class Snapshot:
    session: date
    equity: float
    cash: float
    long_market_value: float
    peak_equity: float
    drawdown: float
    open_positions: int
    previous_equity: float | None


def write_snapshot(
    conn: Connection, session: date, account: Account, positions: Sequence[BrokerPosition]
) -> Snapshot:
    """Upsert the session's snapshot; the peak is the highest equity on any earlier session or
    this one."""
    previous_peak = conn.execute(
        select(func.max(AccountSnapshot.equity)).where(AccountSnapshot.session_date < session)
    ).scalar_one_or_none()
    previous_equity = conn.execute(
        select(AccountSnapshot.equity)
        .where(AccountSnapshot.session_date < session)
        .order_by(AccountSnapshot.session_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    peak = max(account.equity, previous_peak or 0.0)
    drawdown = 0.0 if peak <= 0 else max(0.0, 1 - account.equity / peak)
    open_positions = len([p for p in positions if p.qty > 0])
    values: dict[str, object] = {
        "session_date": session,
        "equity": account.equity,
        "cash": account.cash,
        "long_market_value": account.long_market_value,
        "peak_equity": peak,
        "drawdown": drawdown,
        "open_positions": open_positions,
    }
    statement = insert(AccountSnapshot).values(values)
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[AccountSnapshot.session_date],
            set_={k: statement.excluded[k] for k in values if k != "session_date"},
        )
    )
    return Snapshot(
        session=session,
        equity=account.equity,
        cash=account.cash,
        long_market_value=account.long_market_value,
        peak_equity=peak,
        drawdown=drawdown,
        open_positions=open_positions,
        previous_equity=previous_equity,
    )


def closed_today(conn: Connection, session: date) -> list[TradeJournalRow]:
    return entities(conn, select(TradeJournalRow).where(TradeJournalRow.exit_session == session))


def _money(value: float) -> str:
    return f"${value:,.2f}"


def daily_report(
    snapshot: Snapshot,
    positions: Iterable[BrokerPosition],
    levels: Mapping[str, float],
    closed: Iterable[TradeJournalRow],
    *,
    entries: Iterable[Mapping[str, Any]] = (),
    invariant_ok: bool,
    alerts: Sequence[str] = (),
    mode: str,
) -> str:
    change = ""
    if snapshot.previous_equity:
        delta = snapshot.equity - snapshot.previous_equity
        change = f" ({'+' if delta >= 0 else ''}{_money(delta)}, "
        change += f"{delta / snapshot.previous_equity:+.2%})"
    lines = [
        f"AuraTrade daily report {snapshot.session} [{mode}]",
        f"Equity {_money(snapshot.equity)}{change}; cash {_money(snapshot.cash)}; "
        f"drawdown {snapshot.drawdown:.1%} from {_money(snapshot.peak_equity)}",
    ]
    held = [p for p in positions if p.qty > 0]
    lines.append(f"Positions: {len(held)}")
    for p in sorted(held, key=lambda x: x.symbol):
        price = f" @ {p.current_price:.2f}" if p.current_price else ""
        stop = levels.get(p.symbol)
        stop_text = f", stop {stop:.2f}" if stop is not None else ", NO PLAN"
        lines.append(f"  {p.symbol} {p.qty} sh, cost {p.avg_entry_price:.2f}{price}{stop_text}")
    entered = list(entries)
    if entered:
        lines.append("Entries: " + ", ".join(str(e.get("symbol")) for e in entered))
    trades = list(closed)
    if trades:
        lines.append("Closed:")
        for t in trades:
            r = "" if t.r_multiple is None else f" {t.r_multiple:+.2f}R"
            lines.append(f"  {t.symbol} {t.exit_reason}{r}")
    lines.append("Stops: every position covered" if invariant_ok else "Stops: CHECK THE ALERTS")
    if alerts:
        lines.append(f"Alerts today: {len(alerts)}")
    return "\n".join(lines)
