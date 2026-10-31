from datetime import date
from typing import Any

import pytest

from auratrade.mcp_server import public
from tests.mcp_server.snapshots import snapshot


def test_performance_since_the_start_before_and_after_tax() -> None:
    out = public.performance(snapshot())

    assert (out["from"], out["to"], out["sessions"]) == ("2026-11-02", "2026-12-04", 3)
    book = out["returns"]["book"]
    assert book["equity"] == 104_000.0
    assert book["return"] == pytest.approx(0.04)
    assert book["return_after_tax"] == pytest.approx(0.03)
    assert out["book_vs_twin"]["before_tax"] == pytest.approx(0.02)
    assert out["book_vs_twin"]["after_tax"] == pytest.approx(0.015)
    assert out["book_vs_spy"]["after_tax"] == pytest.approx(0.0225)
    assert out["mode"] == "paper"


def test_the_account_line_starts_at_its_first_value() -> None:
    account = public.performance(snapshot())["returns"]["account"]

    assert account["return"] == pytest.approx(0.03)
    assert account["return_after_tax"] == pytest.approx(0.0225)


def test_performance_since_a_date() -> None:
    out = public.performance(snapshot(), since=date(2026, 11, 3))

    assert out["sessions"] == 2
    assert out["returns"]["book"]["return"] == pytest.approx(104 / 101 - 1, abs=1e-6)


def test_performance_with_no_sessions_in_the_window() -> None:
    out = public.performance(snapshot(), since=date(2027, 1, 1))

    assert out["sessions"] == 0
    assert out["returns"] == {}


def test_a_book_missing_from_the_series_is_left_out() -> None:
    doc = snapshot()
    for row in doc["series"]:
        row.pop("account")
        row.pop("account_after_tax")

    out = public.performance(doc)

    assert "account" not in out["returns"]


def test_a_zero_start_has_no_return() -> None:
    doc = snapshot()
    doc["series"][0]["spy"] = 0.0

    assert public.performance(doc)["returns"]["spy"]["return"] is None


def test_decisions_newest_first() -> None:
    out = public.decisions(snapshot(), last=1)

    assert out["total"] == 2
    assert [d["summary"] for d in out["decisions"]] == ["December"]
    assert public.decisions(snapshot(), last=-1)["decisions"] == []


def test_exposure_and_scorecard() -> None:
    assert public.exposure(snapshot())["exposure"][0]["rule_level"] == 1.3
    assert public.scorecard(snapshot())["scorecard"]["months_complete"] == 1.0
    assert public.scorecard({})["scorecard"] == {}


# --- the dashboard  ----------------------------------------------------------------


def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {
            "session": f"s{i:04d}",
            "book": 100.0 + i,
            "account": None if i < 5 else 100.0 + i,
        }
        for i in range(n)
    ]


def test_thin_keeps_a_short_series_whole() -> None:
    rows = _rows(10)

    assert public.thin(rows, 600) == rows


def test_thin_keeps_each_lines_start_and_the_last_two_sessions() -> None:
    rows = _rows(2_000)

    kept = public.thin(rows, 600)

    assert len(kept) <= 600 + 4
    sessions = [r["session"] for r in kept]
    assert sessions == sorted(sessions)
    assert {"s0000", "s0005", "s1998", "s1999"} <= set(sessions)  # the account starts at 5


def test_the_dashboard_carries_the_snapshot_trimmed() -> None:
    doc = snapshot()
    doc["journal"] = [{"session": f"2026-{m:02d}-01", "summary": str(m)} for m in range(1, 8)]

    out = public.dashboard(doc)

    assert [j["summary"] for j in out["journal"]] == ["5", "6", "7"]
    assert out["decisions_total"] == 7
    assert out["series"] == doc["series"]
    assert out["exposure"] == doc["exposure"]
    assert out["through"] == "2026-12-04"


def test_the_dashboard_text() -> None:
    text = public.dashboard_text(snapshot())

    assert text.splitlines() == [
        "AuraTrade through 2026-12-04 (the Alpaca paper account). "
        "Paper trading, not financial advice.",
        "Paper account: $103,000, +3.00% since 2026-11-03; $102,250 after the estimated tax "
        "($750 owed on a sale of everything).",
        "Returns after tax (before): the book +3.00% (+4.00%), the twin +1.50% (+2.00%), the "
        "S&P 500 +0.75% (+1.00%). Claude's edge over the twin: +1.50 pts after tax; over the "
        "S&P 500: +2.25 pts.",
        "Exposure since 2026-11-02: trend UP, rule level 1.3x, the book 1.3x; satellite: "
        "SPY 15.0%, GLD 5.0%.",
        "Latest decision (2026-12-01): December",
        "Scorecard: 1 complete month(s); edge after tax +1.50 pts; tax realized n/a "
        "(the twin: n/a).",
    ]


def test_the_dashboard_text_in_a_dry_run() -> None:
    doc = snapshot()
    doc["mode"] = "dry_run"
    for row in doc["series"]:
        row["account"] = row["account_after_tax"] = None
    doc["exposure"][0].update(level=1.1999999999999997, capped=True)

    text = public.dashboard_text(doc)

    assert "(a dry run)" in text
    assert "The book (simulated): $104,000, +4.00% since 2026-11-02" in text
    assert "trend UP (volatility cap), rule level 1.3x, the book 1.2x" in text


def test_the_dashboard_text_before_any_session() -> None:
    text = public.dashboard_text({"through": "2026-11-02", "mode": "paper", "series": []})

    assert text.endswith("No sessions in the snapshot yet.")


def test_the_dashboard_text_before_the_first_decision() -> None:
    doc = snapshot()
    doc["exposure"], doc["journal"], doc["scorecard"] = [], [], {}

    lines = public.dashboard_text(doc).splitlines()

    assert len(lines) == 3
    assert lines[2].startswith("Returns after tax (before): ")
