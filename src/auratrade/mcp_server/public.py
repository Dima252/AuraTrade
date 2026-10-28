"""The public tools (README §5.10): views of the dashboard's snapshot (§5.8), nothing more.

The snapshot is allow-listed field by field (`journal/dashboard.py`), so these only select and
summarise it: a server serving them can't show more than the site does. Pure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

BOOKS = ("book", "twin", "spy", "account")


def _header(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "through": doc.get("through"),
        "mode": doc.get("mode"),
        "generated_at": doc.get("generated_at"),
        "note": doc.get("note"),
    }


def _first(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    return next((r[key] for r in rows if r.get(key) is not None), None)


def _last(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    return next((r[key] for r in reversed(rows) if r.get(key) is not None), None)


def _return(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start <= 0:
        return None
    return round(end / start - 1.0, 6)


def _diff(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round(a - b, 6)


def performance(doc: Mapping[str, Any], since: date | None = None) -> dict[str, Any]:
    """Each line's equity and return over the window, before and after the estimated tax."""
    rows = [r for r in doc.get("series", []) if since is None or r["session"] >= str(since)]
    out = _header(doc)
    out["sessions"] = len(rows)
    if not rows:
        out["returns"] = {}
        return out
    out["from"], out["to"] = rows[0]["session"], rows[-1]["session"]
    returns: dict[str, dict[str, float | None]] = {}
    for name in BOOKS:
        if _last(rows, name) is None:
            continue  # the account's line exists on `paper` only
        after = f"{name}_after_tax"
        returns[name] = {
            "equity": _last(rows, name),
            "equity_after_tax": _last(rows, after),
            "return": _return(_first(rows, name), _last(rows, name)),
            "return_after_tax": _return(_first(rows, after), _last(rows, after)),
        }
    out["returns"] = returns
    book = returns.get("book", {})
    for other in ("twin", "spy"):
        theirs = returns.get(other, {})
        out[f"book_vs_{other}"] = {
            "before_tax": _diff(book.get("return"), theirs.get("return")),
            "after_tax": _diff(book.get("return_after_tax"), theirs.get("return_after_tax")),
        }
    return out


def decisions(doc: Mapping[str, Any], last: int = 3) -> dict[str, Any]:
    """The monthly journal, newest first."""
    journal = list(doc.get("journal", []))
    out = _header(doc)
    out["total"] = len(journal)
    out["decisions"] = journal[::-1][: max(last, 0)]
    return out


def exposure(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Each decision's trend, cap, halt, rule level, the LLM's shift and the satellite."""
    out = _header(doc)
    out["exposure"] = list(doc.get("exposure", []))
    return out


def scorecard(doc: Mapping[str, Any]) -> dict[str, Any]:
    out = _header(doc)
    out["scorecard"] = dict(doc.get("scorecard") or {})
    return out
