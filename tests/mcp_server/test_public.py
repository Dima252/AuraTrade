from datetime import date

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
