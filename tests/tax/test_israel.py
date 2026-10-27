from __future__ import annotations

from datetime import date

import pytest

from auratrade.tax.israel import Lot, Sale, TaxBook, YearTax, fifo_gain

JAN = date(2020, 1, 2)
JUNE = date(2020, 6, 1)


def two_lots() -> TaxBook:
    book = TaxBook(0.25)
    book.buy("SPY", JAN, 10, 1000.0)
    book.buy("SPY", JUNE, 10, 1500.0)
    return book


class TestLots:
    def test_a_sale_closes_lots_first_in_first_out_splitting_the_last(self) -> None:
        book = two_lots()
        sale = book.sell("SPY", date(2020, 9, 1), 15, 2400.0)
        assert sale == Sale("SPY", date(2020, 9, 1), 15, 2400.0, 1000.0 + 750.0)
        assert sale.gain == 650.0
        assert book.lots("SPY") == [Lot(JUNE, 5, 750.0)]
        assert (book.units("SPY"), book.cost_basis("SPY")) == (5, 750.0)
        assert book.realized(2020) == 650.0

    def test_the_lots_returned_are_copies(self) -> None:
        book = two_lots()
        book.lots("SPY")[0].units = 0
        assert book.units("SPY") == 20

    def test_selling_everything_forgets_the_fund(self) -> None:
        book = two_lots()
        book.buy("BIL", JAN, 1, 91.0)
        # Float noise in the units sold doesn't leave a sliver or fail the sale.
        book.sell("SPY", JUNE, 20 + 1e-12, 2000.0)
        assert book.symbols() == ["BIL"]
        assert book.units("SPY") == 0
        assert book.realized(2020) == -500.0

    @pytest.mark.parametrize(
        ("units", "proceeds"),
        [(21, 100.0), (0, 100.0), (5, -1.0)],
    )
    def test_invalid_sales(self, units: float, proceeds: float) -> None:
        with pytest.raises(ValueError, match="sale of"):
            two_lots().sell("SPY", JUNE, units, proceeds)

    def test_float_noise_on_a_large_holding_is_tolerated(self) -> None:
        # The scored run's first attempt: 20.5 million units, sold 5e-9 "too many".
        book = TaxBook(0.25)
        for _ in range(7):
            book.buy("BIL", JAN, 20_543_372.664204985 / 7, 1000.0)
        held = book.units("BIL")
        book.sell("BIL", JUNE, held + held * 3e-16 + 5e-9, 7000.0)
        assert book.symbols() == []
        with pytest.raises(ValueError, match="sale of"):
            two_lots().sell("SPY", JUNE, 20 * (1 + 1e-6), 1.0)

    def test_a_withdrawal_removes_lots_without_realizing(self) -> None:
        book = two_lots()
        book.close_year(2020)
        assert book.withdraw("SPY", 12) == 1000.0 + 300.0
        assert book.lots("SPY") == [Lot(JUNE, 8, 1200.0)]
        assert book.realized(2020) == 0.0
        with pytest.raises(ValueError, match="sale of"):
            book.withdraw("SPY", 9)

    @pytest.mark.parametrize(
        ("units", "cost"), [(0, 10.0), (1, -1.0), (float("inf"), 1.0), (1, float("nan"))]
    )
    def test_invalid_buys(self, units: float, cost: float) -> None:
        with pytest.raises(ValueError, match="buy of"):
            TaxBook(0.25).buy("SPY", JAN, units, cost)


class TestYears:
    def test_gains_and_income_each_at_the_rate(self) -> None:
        book = two_lots()
        book.sell("SPY", JUNE, 10, 2000.0)
        book.add_income(JUNE, 100.0)
        tax = book.year_tax(2020)
        assert tax == YearTax(2020, 1000.0, 0.0, 0.0, 250.0, 100.0, 25.0)
        assert tax.total == 275.0
        # Asking doesn't close the year.
        assert book.year_tax(2020) == tax

    def test_a_loss_is_carried_forward_against_later_gains_only(self) -> None:
        book = two_lots()
        book.sell("SPY", JUNE, 10, 600.0)  # -400
        book.add_income(JUNE, 40.0)
        first = book.close_year(2020)
        assert (first.carry_out, first.gain_tax, first.income_tax) == (400.0, 0.0, 10.0)
        book.sell("SPY", date(2021, 3, 1), 4, 700.0)  # +100 of the 1,500 lot's 600
        second = book.close_year(2021)
        assert (second.carry_in, second.carry_out, second.gain_tax) == (400.0, 300.0, 0.0)
        book.sell("SPY", date(2022, 3, 1), 6, 1900.0)  # +1,000
        third = book.close_year(2022)
        assert (third.carry_in, third.carry_out, third.gain_tax) == (300.0, 0.0, 175.0)
        assert book.carry == 0.0

    def test_a_closed_year_takes_nothing_more(self) -> None:
        book = two_lots()
        book.close_year(2020)
        with pytest.raises(ValueError, match="already closed"):
            book.close_year(2020)
        with pytest.raises(ValueError, match="already closed"):
            book.buy("SPY", JUNE, 1, 1.0)
        with pytest.raises(ValueError, match="already closed"):
            book.add_income(JUNE, 1.0)
        book.add_income(date(2021, 1, 4), 1.0)

    def test_invalid_book_and_income(self) -> None:
        with pytest.raises(ValueError, match="rate"):
            TaxBook(1.5)
        with pytest.raises(ValueError, match="rate"):
            TaxBook(0.25, carry=-1.0)
        with pytest.raises(ValueError, match="negative"):
            TaxBook(0.25).add_income(JAN, -1.0)

    def test_a_carried_loss_can_start_the_book(self) -> None:
        book = TaxBook(0.25, carry=50.0)
        book.buy("SPY", JAN, 1, 100.0)
        book.sell("SPY", JUNE, 1, 300.0)
        assert book.close_year(2020).gain_tax == 37.5


class TestFifoGain:
    LOTS = (Lot(date(2024, 1, 2), 10.0, 100.0), Lot(date(2025, 1, 2), 10.0, 300.0))

    def test_lots_close_oldest_first_and_split_pro_rata(self) -> None:
        # 15 at $50: cost 100 + 300 x 5/10 = 250, proceeds 750.
        assert fifo_gain(self.LOTS, 15.0, 50.0) == pytest.approx(500.0)

    def test_units_beyond_the_lots_are_all_gain(self) -> None:
        # 25 at $50: cost 400 for the 20 held; the other 5 at no cost.
        assert fifo_gain(self.LOTS, 25.0, 50.0) == pytest.approx(850.0)

    def test_a_loss(self) -> None:
        assert fifo_gain(self.LOTS, 20.0, 10.0) == pytest.approx(200.0 - 400.0)

    def test_nothing_sold_and_no_lots(self) -> None:
        assert fifo_gain(self.LOTS, 0.0, 50.0) == 0.0
        assert fifo_gain((), 2.0, 50.0) == 100.0
