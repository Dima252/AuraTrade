"""The live tax book from the broker's activities (task 5.3): known answers by hand."""

from __future__ import annotations

import itertools
from datetime import date

import pytest

from auratrade.execution.broker import FILL, Activity, Side
from auratrade.tax.live import (
    CASH,
    LotRow,
    carried_loss,
    disagreements,
    replay,
    values_for_tax,
)

FUNDS = ("SPY", "SSO", "BIL")
_ids = itertools.count()


def fill(
    day: date, symbol: str | None, side: Side, qty: float, price: float, order: str
) -> Activity:
    return Activity(
        f"{day:%Y%m%d}{next(_ids):06d}", FILL, day, symbol, side, qty, price, order_id=order
    )


def cash(day: date, kind: str, amount: float, symbol: str | None = None) -> Activity:
    return Activity(f"{day:%Y%m%d}{next(_ids):06d}", kind, day, symbol, amount=amount)


D1, D2, D3 = date(2026, 11, 2), date(2026, 12, 1), date(2027, 1, 4)


def test_fills_open_lots_and_a_sale_closes_them_fifo() -> None:
    record = [
        fill(D1, "SPY", Side.BUY, 6, 100.0, "o1"),
        fill(D1, "SPY", Side.BUY, 4, 100.0, "o1"),  # the same order's second partial fill
        fill(D2, "SPY", Side.BUY, 10, 110.0, "o2"),
        fill(D2, "SPY", Side.SELL, 8, 120.0, "o3"),
    ]
    live = replay(record, FUNDS, 0.25, D2)
    assert live.warnings == []
    assert live.rows == [
        LotRow("LOT", "SPY", D1, 2, 200.0, order_id="o1"),
        LotRow("LOT", "SPY", D2, 10, 1100.0, order_id="o2"),
        LotRow("LOT", "SPY", D1, 6, 600.0, D2, 720.0, 120.0, "o1"),
        LotRow("LOT", "SPY", D1, 2, 200.0, D2, 240.0, 40.0, "o1"),
    ]
    assert live.book.realized(2026) == pytest.approx(160.0)
    assert live.book.tax_if_sold(2026, {"SPY": 12 * 120.0}) == pytest.approx(
        0.25 * (160.0 + 12 * 120.0 - 1300.0)
    )


def test_the_record_is_sorted_by_session_then_id() -> None:
    sell = fill(D2, "SPY", Side.SELL, 1, 120.0, "o2")
    buy = fill(D1, "SPY", Side.BUY, 1, 100.0, "o1")
    assert replay([sell, buy], FUNDS, 0.25, D2).book.realized(2026) == 20.0


def test_income_is_the_positive_distributions_and_interest() -> None:
    record = [
        fill(D1, "BIL", Side.BUY, 10, 91.0, "o1"),
        cash(D2, "DIV", 3.5, "BIL"),
        cash(D2, "DIVNRA", -0.9, "BIL"),  # withholding: skipped
        cash(D2, "INT", 0.2),  # interest on cash names no fund
        cash(D2, "DIV", 9.0, "SLN"),  # not a fund
        cash(D2, "FEE", -0.01),
        cash(D2, "JNLC", 100000.0),
    ]
    live = replay(record, FUNDS, 0.25, D2)
    assert [r for r in live.rows if r.kind == "INCOME"] == [
        LotRow("INCOME", "BIL", D2, 0.0, 3.5),
        LotRow("INCOME", CASH, D2, 0.0, 0.2),
    ]
    assert live.book.year_tax(2026).income == pytest.approx(3.7)
    assert live.warnings == []


def test_fills_outside_the_funds_or_without_a_symbol_are_skipped() -> None:
    record = [
        fill(D1, "SLN", Side.BUY, 7, 19.69, "x"),
        fill(D1, None, Side.BUY, 1, 1.0, "y"),
    ]
    live = replay(record, FUNDS, 0.25, D1)
    assert live.rows == []
    assert live.book.symbols() == []


def test_other_entries_in_a_fund_are_reported_not_applied() -> None:
    record = [
        fill(D1, "SSO", Side.BUY, 10, 50.0, "o1"),
        cash(D2, "SPLIT", 0.0, "SSO"),
        cash(D2, "SPLIT", 0.0, "NVDA"),  # not a fund: silent
    ]
    live = replay(record, FUNDS, 0.25, D2)
    assert live.warnings == ["2026-12-01 SPLIT in SSO: not applied to the lots"]
    assert live.book.units("SSO") == 10


def test_a_sale_beyond_the_lots_counts_the_unknown_units_at_no_cost() -> None:
    record = [
        fill(D1, "SPY", Side.BUY, 2, 100.0, "o1"),
        fill(D2, "SPY", Side.SELL, 5, 110.0, "o2"),
    ]
    live = replay(record, FUNDS, 0.25, D2)
    assert live.warnings == ["2026-12-01 sale of 5 SPY with 2 in the lots: 3 counted at no cost"]
    assert live.book.realized(2026) == pytest.approx(5 * 110.0 - 200.0)
    assert live.book.symbols() == []


def test_years_close_as_the_record_crosses_them_and_carry_the_loss() -> None:
    record = [
        fill(D1, "SPY", Side.BUY, 10, 100.0, "o1"),
        fill(D2, "SPY", Side.SELL, 5, 80.0, "o2"),  # a 100 loss in 2026
        fill(D3, "SPY", Side.SELL, 5, 130.0, "o3"),  # a 150 gain in 2027
    ]
    live = replay(record, FUNDS, 0.25, D3)
    assert live.book.carry == pytest.approx(100.0)
    assert live.book.year_tax(2027).gain_tax == pytest.approx(0.25 * 50.0)
    # The record ends in 2026 and the evening is in 2028: both years close.
    later = replay(record[:2], FUNDS, 0.25, date(2028, 1, 3))
    assert later.book.carry == pytest.approx(100.0)
    assert replay([], FUNDS, 0.25, D3).rows == []


def test_disagreements_with_the_broker() -> None:
    live = replay([fill(D1, "SPY", Side.BUY, 10, 100.0, "o1")], FUNDS, 0.25, D1)
    assert disagreements(live.book, {"SPY": 10.0}, FUNDS) == []
    assert disagreements(live.book, {"SPY": 9.0, "BIL": 3.0}, FUNDS) == [
        "BIL: 0 units in the lots, 3 at the broker",
        "SPY: 10 units in the lots, 9 at the broker",
    ]


def test_values_for_tax_fall_back_to_the_cost() -> None:
    live = replay(
        [fill(D1, "SPY", Side.BUY, 10, 100.0, "o1"), fill(D1, "BIL", Side.BUY, 1, 91.0, "o2")],
        FUNDS,
        0.25,
        D1,
    )
    assert values_for_tax(live.book, {"SPY": 1200.0, "SSO": 5.0}) == {
        "BIL": 91.0,
        "SPY": 1200.0,
    }


def test_carried_loss() -> None:
    assert carried_loss({}, 2027) == 0.0
    # 2025: -300, 2026: +100 uses 100 of it, 2027 isn't before 2027.
    assert carried_loss({2026: 100.0, 2025: -300.0, 2027: 500.0}, 2027) == pytest.approx(200.0)
    assert carried_loss({2025: -300.0, 2026: 400.0}, 2027) == 0.0
