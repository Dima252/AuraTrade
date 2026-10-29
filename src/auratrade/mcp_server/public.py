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


# --- the dashboard (task 6.6, #176) ---------------------------------------------------------

DASHBOARD_POINTS = 600
DASHBOARD_DECISIONS = 3
NAMES = {"book": "the book", "twin": "the twin", "spy": "the S&P 500", "account": "the account"}


def thin(rows: Sequence[Mapping[str, Any]], points: int) -> list[Mapping[str, Any]]:
    """At most about ``points`` rows, evenly spread; each line's first row and the last two are
    always kept, so returns since the start and over the last session stay exact."""
    if len(rows) <= points:
        return list(rows)
    keep = set(range(0, len(rows), -(-len(rows) // points))) | {len(rows) - 2, len(rows) - 1}
    for name in BOOKS:
        first = next((i for i, r in enumerate(rows) if r.get(name) is not None), None)
        if first is not None:
            keep.add(first)
    return [rows[i] for i in sorted(keep)]


def dashboard(doc: Mapping[str, Any]) -> dict[str, Any]:
    """The page's data: the snapshot's shape, its series thinned, the latest decisions only."""
    journal = list(doc.get("journal", []))
    out = _header(doc)
    out["series"] = thin(list(doc.get("series", [])), DASHBOARD_POINTS)
    out["exposure"] = list(doc.get("exposure", []))
    out["journal"] = journal[-DASHBOARD_DECISIONS:]
    out["decisions_total"] = len(journal)
    out["scorecard"] = dict(doc.get("scorecard") or {})
    return out


def _pct(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


def _pts(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f} pts"


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.0f}"


def _level(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}".rstrip("0").rstrip(".") + "x"


def dashboard_text(doc: Mapping[str, Any]) -> str:
    """What the model reads, and every client without MCP Apps shows: the page in a few lines."""
    perf = performance(doc)
    returns: dict[str, dict[str, float | None]] = perf["returns"]
    where = "the Alpaca paper account" if doc.get("mode") == "paper" else "a dry run"
    lines = [f"AuraTrade through {doc.get('through')} ({where}). {doc.get('note') or ''}".strip()]
    if not returns:
        return lines[0] + "\nNo sessions in the snapshot yet."
    main = "account" if "account" in returns else "book"
    start = next(r["session"] for r in doc["series"] if r.get(main) is not None)
    held = returns[main]
    equity, after = held["equity"], held["equity_after_tax"]
    owed = None if equity is None or after is None else equity - after
    lines.append(
        f"{'Paper account' if main == 'account' else 'The book (simulated)'}: "
        f"{_usd(equity)}, {_pct(held['return'])} since {start}; {_usd(after)} after "
        f"the estimated tax ({_usd(owed)} owed on a sale of everything)."
    )
    lines.append(
        "Returns after tax (before): "
        + ", ".join(
            f"{NAMES[n]} {_pct(r['return_after_tax'])} ({_pct(r['return'])})"
            for n, r in returns.items()
            if n != "account"
        )
        + f". Claude's edge over the twin: {_pts(perf['book_vs_twin']['after_tax'])} after "
        f"tax; over the S&P 500: {_pts(perf['book_vs_spy']['after_tax'])}."
    )
    exposure = list(doc.get("exposure", []))
    if exposure:
        e = exposure[-1]
        level = e.get("level") if e.get("level") is not None else e.get("rule_level")
        states = (("volatility cap", e.get("capped")), ("halt", e.get("halted")))
        flags = [name for name, on in states if on]
        satellite = ", ".join(f"{s} {w * 100:.1f}%" for s, w in (e.get("satellite") or {}).items())
        lines.append(
            f"Exposure since {e.get('session')}: trend {e.get('trend')}"
            + (f" ({', '.join(flags)})" if flags else "")
            + f", rule level {_level(e.get('rule_level'))}, the book {_level(level)}; satellite: "
            f"{satellite or 'none'}."
        )
    journal = list(doc.get("journal", []))
    if journal:
        j = journal[-1]
        lines.append(f"Latest decision ({j.get('session')}): {j.get('summary') or 'no summary'}")
    card = doc.get("scorecard") or {}
    if card:
        lines.append(
            f"Scorecard: {card.get('months_complete', 0):.0f} complete month(s); edge after tax "
            f"{_pts(card.get('excess_after_tax'))}; tax realized {_usd(card.get('tax_realized'))}"
            f" (the twin: {_usd(card.get('twin_tax_realized'))})."
        )
    return "\n".join(lines)
