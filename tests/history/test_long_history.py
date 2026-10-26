from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from auratrade.data.french import FrenchDaily, MarketDay
from auratrade.data.shiller import ShillerMonth
from auratrade.history.long_history import load_long_history, long_history
from auratrade.storage import LocalStore

FETCHED = datetime(2026, 10, 27, 4, 0, tzinfo=UTC)


class Sources:
    def __init__(self, months: list[ShillerMonth]) -> None:
        self._months = months

    def daily_factors(self) -> FrenchDaily:
        # Out of order on purpose: the loader sorts.
        return FrenchDaily(
            "created from the 202608 CRSP database",
            [
                MarketDay(date(1926, 7, 1), 0.001, 0.0001),
                MarketDay(date(1926, 7, 2), 0.0046, 0.0001),
            ],
        )

    def monthly(self) -> list[ShillerMonth]:
        return self._months


MONTHS = [
    ShillerMonth(date(1926, 6, 1), 13.3, 0.69),
    ShillerMonth(date(1926, 7, 1), 13.6, 0.70),
    ShillerMonth(date(1926, 8, 1), 14.0, None),
]


def test_stored_and_loaded_with_the_release_named(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    stored = long_history(store, Sources(MONTHS), Sources(MONTHS), FETCHED)
    assert stored.details() == {
        "sessions": 2,
        "first_session": "1926-07-01",
        "last_session": "1926-07-02",
        "months": 3,
        "last_dividend_month": "1926-07-01",
    }
    loaded = load_long_history(store)
    assert loaded.note == "created from the 202608 CRSP database"
    assert loaded.fetched_at == "2026-10-27T04:00:00+00:00"
    assert loaded.days == [date(1926, 7, 1), date(1926, 7, 2)]
    assert loaded.market == [0.001, 0.0046]
    assert loaded.rf == [0.0001, 0.0001]
    assert loaded.months == [m.month for m in MONTHS]
    assert loaded.prices == [13.3, 13.6, 14.0]
    assert loaded.dividends == [0.69, 0.70, None]


def test_no_dividend_at_all_is_reported_as_none(tmp_path: Path) -> None:
    months = [ShillerMonth(date(1926, 7, 1), 13.6, None)]
    stored = long_history(LocalStore(tmp_path), Sources(months), Sources(months), FETCHED)
    assert stored.details()["last_dividend_month"] is None


def test_loading_without_the_stage_says_how_to_run_it(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"backfill.py longhistory"):
        load_long_history(LocalStore(tmp_path))
