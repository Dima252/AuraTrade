"""The corporate-actions step (README §5.1, §5.7, #116).

Alpaca's feed filters on the **process date**, not the ex-date, and lists announced splits ahead
(spike 0.3b): the request window is padded by sessions on both sides and every action is keyed
on its ex-date (`effective_date`). Splits, reverse splits, mergers, spin-offs and symbol changes
of the ETF list (`symbols`, task 5.2) are stored; cash dividends need nothing (#116). A re-run,
or an action seen again on later evenings, updates its row only if the vendor changed it;
`handled` is left alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from sqlalchemy import Connection, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.data.alpaca_data import CorporateAction, CorporateActionKind
from auratrade.db.models import CorporateAction as CorporateActionRow
from auratrade.db.models import Ticker

KINDS = tuple(k for k in CorporateActionKind if k is not CorporateActionKind.CASH_DIVIDEND)
SOURCE = "ALPACA"


class CorporateActionSource(Protocol):
    def corporate_actions(
        self,
        start: date,
        end: date,
        *,
        symbols: Iterable[str] | None = ...,
        kinds: Iterable[CorporateActionKind] = ...,
    ) -> list[CorporateAction]: ...


@dataclass
class CorporateActionReport:
    window: tuple[date, date]
    fetched: int = 0
    stored: int = 0
    upcoming_splits: list[str] = field(default_factory=list)

    def details(self) -> dict[str, object]:
        return {
            "window": [d.isoformat() for d in self.window],
            "fetched": self.fetched,
            "stored": self.stored,
            "upcoming_splits": self.upcoming_splits,
        }


def ingest_corporate_actions(
    conn: Connection,
    source: CorporateActionSource,
    calendar: MarketCalendar,
    risk: RiskConfig,
    session: date,
    *,
    symbols: Iterable[str] | None = None,
) -> CorporateActionReport:
    cfg = risk.ingest
    start = calendar.n_sessions_back(session, cfg.corporate_actions_lookback_sessions).day
    end = session
    for _ in range(cfg.corporate_actions_lookahead_sessions):
        end = calendar.next_session(end).day
    report = CorporateActionReport((start, end))
    wanted = None if symbols is None else sorted(set(symbols))
    actions = source.corporate_actions(start, end, symbols=wanted, kinds=KINDS)
    report.fetched = len(actions)
    found = conn.execute(
        select(Ticker.symbol, Ticker.id).where(
            Ticker.active, Ticker.symbol.in_({a.symbol for a in actions})
        )
    ).all()
    known = {row.symbol: row.id for row in found}
    rows = [
        {
            "ticker_id": known[a.symbol],
            "symbol": a.symbol,
            "action_type": a.kind.value,
            "ex_date": a.effective_date,
            "ratio": a.ratio,
            "terms": a.terms,
            "source": SOURCE,
            "source_id": a.source_id,
        }
        for a in actions
        if a.symbol in known
    ]
    report.upcoming_splits = sorted(
        f"{a.symbol} {a.effective_date} x{a.ratio:g}"
        for a in actions
        if a.symbol in known
        and a.ratio is not None
        and a.effective_date > session
        and a.kind in (CorporateActionKind.SPLIT, CorporateActionKind.REVERSE_SPLIT)
    )
    if rows:
        t = CorporateActionRow
        stmt = insert(t).values(rows)
        new = stmt.excluded
        changed = ("ticker_id", "action_type", "ex_date", "ratio", "terms")
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "source_id"],
            set_={c: getattr(new, c) for c in changed},
            where=tuple_(*(getattr(t, c) for c in changed)).is_distinct_from(
                tuple_(*(getattr(new, c) for c in changed))
            ),
        )
        report.stored = len(conn.execute(stmt.returning(t.id)).all())
    return report
