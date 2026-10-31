"""The public dashboard's snapshot (README §5.8, #35): one allow-listed JSON file.

Written by `simulate` each evening, once the day's twin exists, to ``public/snapshot.json`` in
the private storage bucket and, on AWS, to ``data/snapshot.json`` in the site's bucket, where
`web/dashboard.js` reads it. Every field is chosen here, never
copied whole from a row, so nothing reaches it by accident: no order or broker ids, no account
number, no stop levels, no units held. What it shows:

- **The series:** each session's equity of the book (its decisions replayed, the like-for-like
  return), the twin and SPY bought and held, each before and after the estimated tax; on
  `paper`, the account's own (paper fills) beside them.
- **The exposure history:** each decision's trend, cap, halt, rule level, the LLM's shift and
  the enforced satellite.
- **The decision journal:** each month's summary, the LLM's reasoning, the limits it hit, and
  its results once scored.
- **The scorecard's summary.**
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, orm, select

from auratrade.db.models import (
    AccountSnapshot,
    LlmDecision,
    PortfolioJournal,
    PortfolioTarget,
)
from auratrade.journal.portfolio import finite
from auratrade.journal.portfolio_scorecard import Scorecard, period_json
from auratrade.sim.portfolio import PortfolioRun
from auratrade.storage.base import ObjectStore

KEY = "public/snapshot.json"
# Under the site store's `data/` prefix, which the site's deploys never prune.
SITE_KEY = "snapshot.json"
SCHEMA = 1
NOTE = "Paper trading, not financial advice. Tax is estimated (25% Israeli capital gains)."
ANSWER_FIELDS = ("core_shift", "core_reasoning", "tax_note")
WEIGHT_FIELDS = ("symbol", "weight", "expectation", "expected_vs_market", "why_not_hold")


def _money(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _run_series(run: PortfolioRun) -> dict[date, tuple[float, float]]:
    return {
        day: (mark, mark - snap.tax_owed)
        for day, mark, snap in zip(run.days, run.marks, run.snapshots, strict=True)
    }


def series(
    book: PortfolioRun,
    twin: PortfolioRun,
    spy: PortfolioRun,
    account: Mapping[date, tuple[float, float | None]] | None,
) -> list[dict[str, Any]]:
    """One row per session of the books; ``account`` (equity, tax owed) on `paper`."""
    runs = {"book": _run_series(book), "twin": _run_series(twin), "spy": _run_series(spy)}
    out = []
    for day in book.days:
        row: dict[str, Any] = {"session": str(day)}
        for name, marks in runs.items():
            before, after = marks.get(day, (None, None))
            row[name] = _money(before)
            row[f"{name}_after_tax"] = _money(after)
        if account is not None:
            equity, tax = account.get(day, (None, None))
            row["account"] = _money(equity)
            row["account_after_tax"] = (
                None if equity is None or tax is None else _money(equity - tax)
            )
        out.append(row)
    return out


def exposure(rows: Sequence[PortfolioTarget]) -> list[dict[str, Any]]:
    return [
        {
            "session": str(r.execution_session),
            "kind": r.kind,
            "trend": r.trend,
            "capped": r.capped,
            "halted": r.halted,
            "rule_level": r.rule_level,
            "level": r.level,
            "shift": None if r.level is None else round(r.level - r.rule_level, 4),
            "satellite": {s: round(w, 4) for s, w in sorted((r.satellite or {}).items())},
            "held_satellite": r.held_satellite,
            "fell_back": r.fell_back,
        }
        for r in rows
    ]


def journal_entry(
    row: PortfolioJournal, answer: Mapping[str, Any] | None, results: Mapping[str, Any] | None
) -> dict[str, Any]:
    entry: dict[str, Any] = {"session": str(row.execution_session), "summary": row.summary}
    if answer is not None:
        entry["decision"] = {k: answer.get(k) for k in ANSWER_FIELDS}
        satellite = answer.get("satellite")
        entry["decision"]["satellite"] = (
            None if satellite is None else [{k: w.get(k) for k in WEIGHT_FIELDS} for w in satellite]
        )
    entry["limits"] = [
        {"rule": c.get("rule"), "outcome": c.get("outcome"), "message": c.get("message")}
        for c in row.clips
    ]
    entry["results"] = results
    return entry


def build(
    conn: Connection,
    book: PortfolioRun,
    twin: PortfolioRun,
    spy: PortfolioRun,
    card: Scorecard,
    *,
    mode: str,
    live: bool,
    generated: datetime,
) -> dict[str, Any]:
    session = orm.Session(conn)
    account = None
    if live:
        account = {
            s.session_date: (s.equity, s.tax_owed)
            for s in session.scalars(
                select(AccountSnapshot).where(AccountSnapshot.session_date.in_(book.days))
            )
        }
    decided = list(
        session.scalars(
            select(PortfolioTarget)
            .where(PortfolioTarget.status == "DECIDED")
            .order_by(PortfolioTarget.execution_session, PortfolioTarget.id)
        )
    )
    periods = {p.session: period_json(p) for p in card.periods}
    journal = [
        journal_entry(row, answer, periods.get(row.execution_session))
        for row, answer in session.execute(
            select(PortfolioJournal, LlmDecision.answer)
            .join(PortfolioTarget, PortfolioTarget.id == PortfolioJournal.targets_id)
            .outerjoin(LlmDecision, LlmDecision.id == PortfolioTarget.llm_decision_id)
            .order_by(PortfolioJournal.execution_session)
        ).all()
    ]
    return {
        "schema": SCHEMA,
        "generated_at": generated.isoformat(),
        "through": str(card.through),
        "mode": mode,
        "note": NOTE,
        "series": series(book, twin, spy, account),
        "exposure": exposure(decided),
        "journal": journal,
        "scorecard": card.summary(),
    }


def publish(storage: ObjectStore, doc: Mapping[str, Any], *, key: str = KEY) -> int:
    """Write the snapshot (non-finite numbers as null); its size in bytes."""
    data = json.dumps(finite(doc), indent=1).encode()
    storage.put(key, data, content_type="application/json")
    return len(data)
