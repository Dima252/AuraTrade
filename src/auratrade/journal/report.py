"""The account snapshot and the Telegram daily report written by `post_market` (README §5.7).

The snapshot carries the funds' holdings and, from the live tax lots , the tax a sale
of everything at the close would owe: equity less it is the account "after the estimated tax".
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert

from auratrade.db.models import AccountSnapshot
from auratrade.execution.broker import Account, BrokerPosition


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
    holdings: dict[str, dict[str, float]] = field(default_factory=dict)
    tax_owed: float | None = None

    @property
    def after_tax(self) -> float | None:
        return None if self.tax_owed is None else self.equity - self.tax_owed


def holdings(
    positions: Iterable[BrokerPosition], funds: Collection[str]
) -> dict[str, dict[str, float]]:
    """The funds held at the close: units and value (at the cost when the broker gives no
    price)."""
    out = {}
    for p in positions:
        if p.qty > 0 and p.symbol in funds:
            price = p.current_price if p.current_price is not None else p.avg_entry_price
            out[p.symbol] = {"units": float(p.qty), "value": p.qty * price}
    return dict(sorted(out.items()))


def write_snapshot(
    conn: Connection,
    session: date,
    account: Account,
    positions: Sequence[BrokerPosition],
    *,
    funds: Collection[str] = (),
    tax_owed: float | None = None,
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
    held = holdings(positions, funds)
    values: dict[str, Any] = {
        "session_date": session,
        "equity": account.equity,
        "cash": account.cash,
        "long_market_value": account.long_market_value,
        "peak_equity": peak,
        "drawdown": drawdown,
        "open_positions": open_positions,
        "holdings": held,
        "tax_owed": tax_owed,
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
        holdings=held,
        tax_owed=tax_owed,
    )


def _money(value: float) -> str:
    return f"${value:,.2f}"


def daily_report(
    snapshot: Snapshot,
    positions: Iterable[BrokerPosition],
    levels: Mapping[str, float],
    *,
    invariant_ok: bool,
    halted: bool = False,
    alerts: Sequence[str] = (),
    tax_notes: Sequence[str] = (),
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
    if snapshot.tax_owed is None:
        lines.append("After the estimated tax: unknown (the tax lots weren't rebuilt)")
    else:
        lines.append(
            f"After the estimated tax: {_money(snapshot.equity - snapshot.tax_owed)} "
            f"({_money(snapshot.tax_owed)} if everything were sold now)"
        )
    if halted:
        lines.append("Drawdown halt ON: no leverage (README §7.3)")
    held = [p for p in positions if p.qty > 0]
    lines.append(f"Positions: {len(held)}")
    for p in sorted(held, key=lambda x: x.symbol):
        price = f" @ {p.current_price:.2f}" if p.current_price else ""
        stop = levels.get(p.symbol)
        stop_text = f", stop {stop:.2f}" if stop is not None else ""
        lines.append(f"  {p.symbol} {p.qty} sh, cost {p.avg_entry_price:.2f}{price}{stop_text}")
    lines.append("Stops: the leveraged fund covered" if invariant_ok else "Stops: CHECK THE ALERTS")
    if tax_notes:
        lines.append(f"Tax lots: CHECK ({len(tax_notes)})")
        lines.extend(f"  {note}" for note in tax_notes)
    if alerts:
        lines.append(f"Alerts today: {len(alerts)}")
    return "\n".join(lines)
