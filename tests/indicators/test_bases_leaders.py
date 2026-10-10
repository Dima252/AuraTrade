"""Spot-check of base detection and counting on 10 historical leaders (task 2.2).

Recorded split-adjusted SIP daily bars, 2016-2024 (`scripts/fetch_base_fixtures.py`). Only the
clear cases are asserted: declines far past the 50% limit, undercuts by a wide margin, advances
far from the 20% line, and where the count becomes reliable. The full detected list is in the
task 2.2 PR for checking against charts.
"""

import csv
import gzip
from datetime import date
from pathlib import Path

import pytest

from auratrade.config import DEFAULT_RISK_CONFIG, load_risk_config
from auratrade.indicators.bars import Bar
from auratrade.indicators.bases import BaseCountState, BaseRules, BaseStatus, detect_bases

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bases"
TICKERS = ("NVDA", "AMD", "ANET", "SHOP", "ENPH", "TSLA", "ZM", "CELH", "SMCI", "NFLX")
RULES = BaseRules(**load_risk_config(DEFAULT_RISK_CONFIG).bases.model_dump())

# IPOs that fall inside the recorded history, from the real listing dates. Every other ticker
# went public before 2016. CELH's first bar (2017-05-24) is its uplisting from OTC to Nasdaq,
# not an IPO: it had traded publicly for years, so its earlier bases are unknown.
IPO_DATES = {"ZM": date(2019, 4, 18)}

# SMCI was off Nasdaq from 2018-08-22 to 2020-01-14. A gap of more than 30 sessions makes the
# relisted shares a separate security (README §5.1), with a history that starts at the relisting.
SECURITY_STARTS = {"SMCI": date(2020, 1, 14)}


def load(symbol: str) -> tuple[list[date], list[Bar]]:
    with gzip.open(FIXTURES / f"{symbol}.csv.gz", "rt", encoding="utf-8") as f:
        rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
    start = SECURITY_STARTS.get(symbol, date.min)
    rows = [r for r in rows if date.fromisoformat(r["date"]) >= start]
    dates = [date.fromisoformat(r["date"]) for r in rows]
    bars = [
        Bar(
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
        )
        for r in rows
    ]
    return dates, bars


type Row = tuple[date, date | None, BaseStatus, int | None, bool, bool]
"""(start, end, status, stage, count reliable, reset)."""


def detected(symbol: str) -> list[Row]:
    dates, bars = load(symbol)
    at_ipo = IPO_DATES.get(symbol) == dates[0]
    scan = detect_bases(bars, RULES, BaseCountState.at_listing() if at_ipo else None)
    return [
        (
            dates[b.start],
            None if b.end is None else dates[b.end],
            b.status,
            b.stage,
            b.count_reliable,
            b.reset,
        )
        for b in scan.bases
    ]


B, F = BaseStatus.BROKEN_OUT, BaseStatus.FAILED
D = date

CLEAR_CASES: dict[str, list[Row]] = {
    "ZM": [
        # The first base after the IPO: a reliable stage 1.
        (D(2019, 6, 10), D(2019, 7, 26), B, 1, True, False),
        # The late-2019 slide to ~61 undercuts that base's low (83.10): the Feb 2020 breakout
        # is from a new stage 1.
        (D(2019, 7, 26), D(2020, 2, 14), B, 1, True, True),
    ],
    "CELH": [
        # The uplisting is not an IPO: the first base's count is unreliable.
        (D(2017, 6, 21), D(2017, 8, 15), B, 1, False, False),
        # 2.33 -> 1.02 (-56%): not a base, and the count resets ...
        (D(2017, 9, 8), D(2018, 8, 10), F, None, False, False),
        # ... so from here the count is reliable.
        (D(2018, 9, 11), D(2018, 11, 12), B, 1, True, True),
    ],
    "NVDA": [
        # A base-on-base (a 1% advance) keeps the prior (unreliable) stage.
        (D(2017, 2, 7), D(2017, 3, 30), B, 3, False, False),
        (D(2017, 3, 30), D(2017, 5, 10), B, 3, False, False),
        # Q4 2018: -57% from the high.
        (D(2018, 10, 1), D(2018, 11, 19), F, None, False, False),
        (D(2018, 12, 3), D(2019, 1, 18), B, 1, True, True),
    ],
    "AMD": [
        # Q4 2018: 34.14 -> about 16.
        (D(2018, 9, 12), D(2018, 10, 26), F, None, True, False),
        (D(2018, 12, 3), D(2019, 1, 18), B, 1, True, True),
    ],
    "TSLA": [
        # The Feb-Mar 2020 crash (-54%).
        (D(2020, 2, 4), D(2020, 3, 16), F, None, True, False),
        (D(2020, 7, 10), D(2020, 8, 17), B, 1, True, True),
        # +40% from the prior pivot: stage 2.
        (D(2020, 8, 31), D(2020, 10, 9), B, 2, True, False),
    ],
    "NFLX": [
        # The 2022 collapse (70.10 -> 16.2 split-adjusted at the low).
        (D(2021, 11, 17), D(2022, 3, 7), F, None, True, False),
        (D(2022, 6, 2), D(2022, 7, 18), B, 1, True, True),
        # +32% from the prior pivot: stage 2.
        (D(2022, 8, 10), D(2022, 9, 16), B, 2, True, False),
    ],
    "SHOP": [
        (D(2021, 11, 12), D(2022, 1, 21), F, None, True, False),
        (D(2022, 2, 9), D(2022, 3, 29), B, 1, True, True),
    ],
    "ENPH": [
        (D(2023, 7, 17), D(2023, 10, 23), F, None, True, False),
        (D(2023, 12, 26), D(2024, 2, 12), B, 1, True, True),
    ],
    "ANET": [
        (D(2019, 4, 15), D(2020, 3, 12), F, None, True, False),
        (D(2020, 8, 4), D(2020, 11, 6), B, 1, True, True),
    ],
    "SMCI": [
        # The relisted security's history doesn't start at an IPO: unreliable.
        (D(2020, 2, 5), D(2020, 5, 8), B, 1, False, False),
        # The 2024 collapse from 122.90.
        (D(2024, 3, 7), D(2024, 8, 2), F, None, True, False),
    ],
}


def test_every_ticker_has_clear_cases() -> None:
    assert set(CLEAR_CASES) == set(TICKERS)


@pytest.mark.parametrize("symbol", TICKERS)
def test_clear_cases(symbol: str) -> None:
    rows = detected(symbol)
    for case in CLEAR_CASES[symbol]:
        assert case in rows, f"{symbol}: expected {case}"


@pytest.mark.parametrize("symbol", TICKERS)
def test_the_count_is_reliable_only_from_an_ipo_or_a_reset(symbol: str) -> None:
    rows = detected(symbol)
    if symbol in IPO_DATES:
        assert all(reliable for *_, reliable, _ in rows)
        return
    first_reset = next(
        i for i, (_, _, status, _, _, reset) in enumerate(rows) if reset or status is F
    )
    assert not any(reliable for *_, reliable, _ in rows[:first_reset])
    assert all(reliable for *_, reliable, _ in rows[first_reset + 1 :])


def test_the_first_bar_alone_is_not_an_ipo() -> None:
    dates, _ = load("CELH")
    assert dates[0] == date(2017, 5, 24)
    assert "CELH" not in IPO_DATES
    assert detected("CELH")[0][4] is False
