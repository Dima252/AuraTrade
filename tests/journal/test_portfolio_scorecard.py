"""The scorecard (README §5.5; task 4.4) on hand-built books with known answers."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from auratrade.config import load_risk_config
from auratrade.journal.portfolio_scorecard import (
    Decision,
    Marks,
    Scorecard,
    block_bootstrap,
    judge,
    percentile,
    period_json,
    score,
)

RISK = load_risk_config()
CFG = RISK.portfolio_manager.scorecard
R, E1, MID, E2, LAST = (date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 16),
                        date(2026, 12, 1), date(2026, 12, 2))  # fmt: skip
DAYS = [R, E1, MID, E2, LAST]

BOOK = Marks(DAYS, [100, 100, 110, 121, 121], [0, 0, 2.5, 5, 5], [0, 0, 0, 4, 4])
TWIN = Marks(DAYS, [100, 100, 105, 110, 110], [0, 0, 1, 2, 2], [0, 0, 0, 0, 0])
SPY = Marks(DAYS, [100, 100, 104, 108, 109], [0, 0, 1, 2, 2.25], [0, 0, 0, 0, 0])
CLOSES = {
    "SPY": {E1: 100.0, E2: 110.0, LAST: 111.0},
    "GLD": {E1: 50.0, E2: 56.0, LAST: 56.0},
    "BIL": {E1: 10.0, E2: 10.1, LAST: 10.1},
}
TILT = Decision(E1, 0.1, {"GLD": 0.1, "SPY": 0.1}, {"GLD": 0.02, "SPY": 0.0})
HOLD = Decision(E2, 0.0, {"GLD": 0.1, "SPY": 0.1}, {})


def card(*decisions: Decision, closes: dict[str, dict[date, float]] = CLOSES) -> Scorecard:
    return score(
        BOOK,
        TWIN,
        SPY,
        decisions,
        closes,
        market="SPY",
        cash="BIL",
        satellite_share=0.2,
        tax_rate=0.25,
        cfg=CFG,
    )


class TestPeriods:
    def test_a_decision_is_scored_from_its_e_to_the_next(self) -> None:
        nov, dec = card(HOLD, TILT).periods  # sorted by E
        assert (nov.session, nov.end, nov.complete) == (E1, E2, True)
        assert nov.before_tax.book == pytest.approx(0.21)
        assert nov.before_tax.excess == pytest.approx(0.11)
        assert nov.before_tax.spy == pytest.approx(0.08)
        # After the tax a sale would owe: 116 / 100 against 108 / 100.
        assert nov.after_tax.book == pytest.approx(0.16)
        assert nov.after_tax.twin == pytest.approx(0.08)
        assert nov.after_tax.excess == pytest.approx(0.08)
        # Gold beat SPY by 2 points at a 10% weight; the shift earned SPY over T-bills.
        assert nov.tilts == {"GLD": pytest.approx(0.002)}
        assert nov.tilt_score == pytest.approx(0.002)
        assert nov.shift_score == pytest.approx(0.1 * (0.10 - 0.01))
        (gold,) = nov.expectations  # SPY's own isn't scored
        assert (gold.symbol, gold.expected, gold.direction, gold.size) == ("GLD", 0.02, True, True)
        assert gold.actual == pytest.approx(0.02)
        assert nov.realized_gain == 0.0
        # The last decision's period is open to the latest close; it states nothing to score.
        assert (dec.session, dec.end, dec.complete) == (E2, LAST, False)
        assert dec.before_tax.book == 0.0
        assert dec.expectations == ()
        # December's E sold 4 of gains: 1 of tax at 25%; the twin realized none.
        assert (dec.realized_gain, dec.tax_realized) == (4.0, 1.0)
        assert dec.twin_tax_realized == 0.0

    def test_an_open_periods_expectations_wait(self) -> None:
        (only,) = card(TILT).periods
        assert not only.complete
        assert only.end == LAST
        assert only.expectations == ()

    def test_a_decision_after_the_last_close_isnt_scored_yet(self) -> None:
        later = Decision(LAST + timedelta(days=1), 0.0, {}, {})
        assert [p.session for p in card(TILT, later).periods] == [E1]

    def test_a_fund_without_both_closes_is_left_out(self) -> None:
        closes = {**CLOSES, "GLD": {E1: 50.0}}
        nov, _ = card(TILT, HOLD, closes=closes).periods
        assert nov.tilts == {}
        assert nov.expectations == []
        assert nov.shift_score == pytest.approx(0.009)

    def test_without_the_market_funds_closes_nothing_is_scored_against_it(self) -> None:
        closes = {"GLD": CLOSES["GLD"], "BIL": CLOSES["BIL"]}
        nov, _ = card(TILT, HOLD, closes=closes).periods
        assert (nov.tilts, nov.shift_score, nov.expectations) == ({}, 0.0, [])

    def test_the_books_must_share_their_closes(self) -> None:
        short = Marks(DAYS[:-1], [100] * 4, [0] * 4, [0] * 4)
        with pytest.raises(ValueError, match="share"):
            score(short, TWIN, SPY, [], {}, market="SPY", cash="BIL", satellite_share=0.2,
                  tax_rate=0.25, cfg=CFG)  # fmt: skip
        empty = Marks([], [], [], [])
        with pytest.raises(ValueError, match="share"):
            score(empty, empty, empty, [], {}, market="SPY", cash="BIL", satellite_share=0.2,
                  tax_rate=0.25, cfg=CFG)  # fmt: skip


class TestRealized:
    def test_a_new_years_first_close_counts_from_zero(self) -> None:
        days = [date(2026, 12, 31), date(2027, 1, 4)]
        m = Marks(days, [1, 1], [0, 0], [50.0, 3.0])
        assert m.realized_on(0) == 50.0
        assert m.realized_on(1) == 3.0
        same = Marks([E1, E2], [1, 1], [0, 0], [5.0, 8.0])
        assert same.realized_on(1) == 3.0


class TestSummary:
    def test_the_facts_summary(self) -> None:
        summary = card(TILT, HOLD).summary()
        assert summary["months_complete"] == 1.0
        assert summary["book_return_after_tax"] == pytest.approx(0.16)
        assert summary["twin_return_after_tax"] == pytest.approx(0.08)
        assert summary["spy_return_after_tax"] == pytest.approx(0.0675)
        assert summary["excess_after_tax"] == pytest.approx(0.08)
        assert summary["excess_before_tax"] == pytest.approx(0.11)
        # November's gold tilt, then December's against SPY's 0.9% (gold flat).
        assert summary["tilt_score"] == pytest.approx(0.002 + 0.1 * (0 - 1 / 110))
        assert summary["shift_score"] == pytest.approx(0.009)
        assert summary["tax_realized"] == 1.0
        assert summary["twin_tax_realized"] == 0.0
        assert summary["expectations_scored"] == 1.0
        assert summary["expectation_direction_rate"] == 1.0
        assert summary["expectation_size_rate"] == 1.0
        assert "excess_after_tax_a_year" not in summary

    def test_no_rates_before_anything_is_scored(self) -> None:
        summary = card(TILT).summary()
        assert summary["expectations_scored"] == 0.0
        assert "expectation_direction_rate" not in summary

    def test_the_journal_row(self) -> None:
        nov = card(TILT, HOLD).periods[0]
        row = period_json(nov)
        assert row["end"] == str(E2)
        assert row["complete"] is True
        assert row["after_tax"] == {
            "book": pytest.approx(0.16),
            "twin": pytest.approx(0.08),
            "spy": pytest.approx(0.06),
            "excess": pytest.approx(0.08),
        }
        assert row["expectations"] == [
            {"symbol": "GLD", "expected": 0.02, "actual": pytest.approx(0.02), "direction": True,
             "size": True}
        ]  # fmt: skip
        assert row["tax_realized"] == 0.0


class TestJudge:
    @pytest.mark.parametrize(
        ("expected", "actual", "verdict"),
        [
            (0.005, 0.003, (True, True)),  # "level", and it was
            (0.0, -0.02, (False, False)),  # "level", and it wasn't
            (0.02, 0.011, (True, True)),  # within 2x
            (0.02, 0.05, (True, False)),  # the direction, but 2.5x the size
            (0.03, 0.01, (True, False)),  # a third of the size
            (-0.03, 0.01, (False, False)),
            (-0.03, -0.04, (True, True)),
            (0.02, 0.0, (False, False)),
        ],
    )
    def test_direction_and_size(
        self, expected: float, actual: float, verdict: tuple[bool, bool]
    ) -> None:
        assert judge(expected, actual, CFG) == verdict


def months(n: int, excess: list[float]) -> tuple[Marks, Marks, Marks, list[Decision]]:
    """``n`` monthly decisions on consecutive days; the book beats the twin by ``excess[k]``
    in month k (nothing owed in tax)."""
    days = [R + timedelta(days=i) for i in range(n + 2)]
    book, twin = [100.0, 100.0], [100.0, 100.0]
    for k in range(n):
        twin.append(twin[-1])
        book.append(book[-1] * (1 + excess[k]))
    zero = [0.0] * len(days)
    decisions = [Decision(days[1 + k], 0.0, {}, {}) for k in range(n + 1)]
    return (Marks(days, book, zero, zero), Marks(days, twin, zero, zero),
            Marks(days, twin, zero, zero), decisions)  # fmt: skip


class TestInterval:
    def test_the_interval_starts_at_24_complete_months(self) -> None:
        def run(n: int) -> Scorecard:
            book, twin, spy, decisions = months(n, [0.01] * n)
            return score(book, twin, spy, decisions, {}, market="SPY", cash="BIL",
                         satellite_share=0.2, tax_rate=0.25, cfg=CFG)  # fmt: skip

        assert run(CFG.bootstrap_months - 1).interval is None
        full = run(CFG.bootstrap_months)
        assert full.interval is not None
        assert full.interval.mean == pytest.approx(0.12)
        assert full.interval.low == pytest.approx(0.12)
        assert full.interval.high == pytest.approx(0.12)
        assert full.summary()["excess_after_tax_a_year"] == pytest.approx(0.12)

    def test_the_interval_brackets_the_mean(self) -> None:
        monthly = [0.01 * ((k * 7) % 5 - 2) for k in range(25)]
        interval = block_bootstrap(monthly, CFG)
        assert interval.low < interval.mean < interval.high
        assert block_bootstrap(monthly, CFG) == interval  # seeded


def test_percentile_interpolates() -> None:
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.5
    assert percentile([1.0, 2.0], 0.0) == 1.0
    assert percentile([1.0, 2.0], 1.0) == 2.0
    assert percentile([7.0], 0.95) == 7.0
