"""Backfill stage: the last 2 years of the history into Postgres, for the live jobs (README §10).

The bars themselves come from the evening ingest, which fetches each tracked ticker's whole
window the first time it sees it, so Postgres holds exactly what the nightly run would. This
stage adds what one evening can't: the EDGAR earnings events of the window (the usual timing of
an upcoming report reads the last 8, #119), each ticker's latest quarters of fundamentals (the
rolling refresh would take 20 sessions), VIX and VIX3M closes (the regime replay reads them),
and the listing dates the full history shows (#126). Postgres tickers are matched to the
history's securities by Alpaca asset id. Rows already there are left as they are.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, bindparam, select, update
from sqlalchemy.dialects.postgresql import insert

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.db.models import EarningsEvent, MacroObservation, Ticker
from auratrade.history.build import SECURITIES
from auratrade.history.edgar import EARNINGS, FUNDAMENTALS
from auratrade.history.macro import MACRO
from auratrade.history.store import get_table, table_key
from auratrade.ingest.earnings import EDGAR
from auratrade.ingest.fundamentals import Quarter, write_quarters
from auratrade.regime.store import MACRO_LOOKBACK_SESSIONS
from auratrade.storage import ObjectStore

WRITE_CHUNK = 2000


@dataclass
class LoadReport:
    window_start: date
    tickers_matched: int = 0
    listed_at_set: int = 0
    earnings_events: int = 0
    fundamentals: int = 0
    macro: int = 0

    def details(self) -> dict[str, object]:
        return {
            "window_start": self.window_start.isoformat(),
            "tickers_matched": self.tickers_matched,
            "listed_at_set": self.listed_at_set,
            "earnings_events": self.earnings_events,
            "fundamentals": self.fundamentals,
            "macro": self.macro,
        }


def _table(store: ObjectStore, name: str) -> list[dict[str, Any]]:
    table = get_table(store, table_key(name))
    if table is None:
        raise FileNotFoundError(f"no {name} table in the history: run the backfill stages first")
    return list(table.to_pylist())


def load_postgres(
    conn: Connection,
    store: ObjectStore,
    calendar: MarketCalendar,
    risk: RiskConfig,
    session: date,
) -> LoadReport:
    cfg = risk.ingest
    window_start = calendar.n_sessions_back(session, cfg.bar_history_sessions - 1).day
    report = LoadReport(window_start)
    rows = conn.execute(
        select(Ticker.id, Ticker.alpaca_asset_id, Ticker.listed_at).where(
            Ticker.alpaca_asset_id.is_not(None)
        )
    ).all()
    by_key = {str(r.alpaca_asset_id): r for r in rows}
    securities = {str(s["security"]): s for s in _table(store, SECURITIES)}
    matched = {key: r.id for key, r in by_key.items() if key in securities}
    report.tickers_matched = len(matched)

    listings = [
        {"p_id": by_key[key].id, "p_listed_at": securities[key]["listed_at"]}
        for key in matched
        if by_key[key].listed_at is None and securities[key]["listed_at"] is not None
    ]
    if listings:
        conn.execute(
            update(Ticker)
            .where(Ticker.id == bindparam("p_id"))
            .values(listed_at=bindparam("p_listed_at")),
            listings,
        )
    report.listed_at_set = len(listings)

    events = [
        {
            "ticker_id": matched[str(e["security"])],
            "report_date": e["report_date"],
            "timing": e["timing"],
            "accepted_at": e["accepted_at"],
            "headline_at": e["headline_at"],
            "source": EDGAR,
        }
        for e in _table(store, EARNINGS)
        if str(e["security"]) in matched and e["report_date"] >= window_start
    ]
    for i in range(0, len(events), WRITE_CHUNK):
        stmt = insert(EarningsEvent).values(events[i : i + WRITE_CHUNK])
        done = conn.execute(
            stmt.on_conflict_do_nothing(
                index_elements=["ticker_id", "report_date", "source"]
            ).returning(EarningsEvent.id)
        )
        report.earnings_events += len(done.all())

    by_ticker: dict[int, list[Quarter]] = defaultdict(list)
    for f in _table(store, FUNDAMENTALS):
        ticker_id = matched.get(str(f["security"]))
        if ticker_id is not None:
            by_ticker[ticker_id].append(_quarter(f))
    latest = {
        t: sorted(qs, key=lambda q: q.period_end)[-cfg.fundamentals_quarters :]
        for t, qs in by_ticker.items()
    }
    report.fundamentals = write_quarters(conn, latest)

    macro_start = calendar.n_sessions_back(window_start, MACRO_LOOKBACK_SESSIONS).day
    macro = [
        {
            "series": m["series"],
            "observation_date": m["observation_date"],
            "value": m["value"],
            "source": m["source"],
        }
        for m in _table(store, MACRO)
        if macro_start <= m["observation_date"] <= session
    ]
    if macro:
        done = conn.execute(
            insert(MacroObservation)
            .values(macro)
            .on_conflict_do_nothing(index_elements=["series", "observation_date"])
            .returning(MacroObservation.series)
        )
        report.macro = len(done.all())
    return report


def _quarter(row: dict[str, Any]) -> Quarter:
    return Quarter(
        period_end=row["period_end"],
        fiscal_year=row["fiscal_year"],
        fiscal_quarter=row["fiscal_quarter"],
        filed=row["filed_date"],
        form=row["form"],
        accession=row["accession"],
        eps=row["eps_diluted"],
        revenue=row["revenue"],
        eps_growth=row["eps_growth"],
        revenue_growth=row["revenue_growth"],
        eps_acceleration=row["eps_acceleration"],
        revenue_acceleration=row["revenue_acceleration"],
    )
