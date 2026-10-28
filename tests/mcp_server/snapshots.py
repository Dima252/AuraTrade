"""A small dashboard snapshot (the shape `journal/dashboard.build` writes)."""

from __future__ import annotations

from typing import Any


def snapshot() -> dict[str, Any]:
    return {
        "schema": 1,
        "generated_at": "2026-12-04T22:00:00+00:00",
        "through": "2026-12-04",
        "mode": "paper",
        "note": "Paper trading, not financial advice.",
        "series": [
            {
                "session": "2026-11-02",
                "book": 100_000.0,
                "book_after_tax": 100_000.0,
                "twin": 100_000.0,
                "twin_after_tax": 100_000.0,
                "spy": 100_000.0,
                "spy_after_tax": 100_000.0,
                "account": None,
                "account_after_tax": None,
            },
            {
                "session": "2026-11-03",
                "book": 101_000.0,
                "book_after_tax": 100_750.0,
                "twin": 100_500.0,
                "twin_after_tax": 100_375.0,
                "spy": 100_400.0,
                "spy_after_tax": 100_300.0,
                "account": 100_000.0,
                "account_after_tax": 100_000.0,
            },
            {
                "session": "2026-12-04",
                "book": 104_000.0,
                "book_after_tax": 103_000.0,
                "twin": 102_000.0,
                "twin_after_tax": 101_500.0,
                "spy": 101_000.0,
                "spy_after_tax": 100_750.0,
                "account": 103_000.0,
                "account_after_tax": 102_250.0,
            },
        ],
        "exposure": [
            {
                "session": "2026-11-02",
                "kind": "REBALANCE",
                "trend": "UP",
                "capped": False,
                "halted": False,
                "rule_level": 1.3,
                "level": 1.3,
                "shift": 0.0,
                "satellite": {"SPY": 0.15, "GLD": 0.05},
                "held_satellite": False,
                "fell_back": False,
            }
        ],
        "journal": [
            {"session": "2026-11-02", "summary": "November", "limits": [], "results": None},
            {"session": "2026-12-01", "summary": "December", "limits": [], "results": None},
        ],
        "scorecard": {"months_complete": 1.0, "excess_after_tax": 0.015},
    }
