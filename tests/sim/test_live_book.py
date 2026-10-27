"""The live book on the simulator (task 4.3, #166): the Portfolio Manager's targets per E, a
run that stops at T, and the market built from the real funds' bars."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from auratrade.sim.funds import funds_market
from auratrade.sim.portfolio import Market, simulate_portfolio
from tests.sim.test_portfolio import AT, PRIMARY, R_DEC, ZERO, market

E_JAN = date(2000, 1, 3)
E_FEB = date(2000, 2, 1)
END = AT[date(2000, 2, 15)]


def with_fund(m: Market, symbol: str, move: dict[date, float] | None = None) -> Market:
    price, out = 100.0, []
    for day in m.days:
        price *= 1 + (move or {}).get(day, 0.0)
        out.append(price)
    return replace(
        m,
        prices={**m.prices, symbol: out},
        payouts={**m.payouts, symbol: [0.0] * len(m.days)},
    )


def run(m: Market, **kwargs: object) -> object:
    return simulate_portfolio(
        m, PRIMARY, ZERO, start=R_DEC, end=END, equity=100_000.0, tax_rate=0.25, **kwargs
    )


class TestBook:
    def test_the_book_trades_its_own_targets_with_satellite_funds(self) -> None:
        m = with_fund(market(), "QQQ", {date(2000, 1, 10): 0.10})
        book = {E_JAN: {"SPY": 0.5, "SSO": 0.3, "QQQ": 0.2}}
        result = simulate_portfolio(
            m,
            PRIMARY,
            ZERO,
            start=R_DEC,
            end=AT[date(2000, 1, 28)],
            equity=100_000.0,
            tax_rate=0.0,
            book=book,
            sell_at_end=False,
        )
        bought = {t.symbol: t.units * t.price for t in result.trades if t.day == E_JAN}
        assert bought == pytest.approx({"SPY": 50_000, "SSO": 30_000, "QQQ": 20_000})
        # QQQ's 10% rise is the book's only gain.
        assert result.marks[-1] == pytest.approx(102_000)
        assert result.end is not None
        assert result.end.units["QQQ"] == pytest.approx(200)
        assert result.end.targets == book[E_JAN]
        assert result.end.tax.units("QQQ") == pytest.approx(200)
        assert len(result.snapshots) == len(result.marks)
        last = result.snapshots[-1]
        assert last.units == result.end.units
        assert last.cash == pytest.approx(result.end.cash)
        assert not last.halted
        assert last.peak == pytest.approx(102_000)

    def test_a_holding_whose_target_is_unchanged_is_not_traded(self) -> None:
        m = with_fund(market(), "QQQ", {date(2000, 1, 10): 0.05})
        book = {
            E_JAN: {"SPY": 0.5, "SSO": 0.3, "QQQ": 0.2},
            E_FEB: {"SPY": 0.6, "SSO": 0.2, "QQQ": 0.2},
        }
        result = simulate_portfolio(
            m,
            PRIMARY,
            ZERO,
            start=R_DEC,
            end=END,
            equity=100_000.0,
            tax_rate=0.0,
            book=book,
            sell_at_end=False,
        )
        february = {t.symbol for t in result.trades if t.day == E_FEB}
        # The core's targets changed; QQQ's didn't and it is within the band (21% of 20%).
        assert february == {"SPY", "SSO"}
        assert result.end is not None
        assert result.end.units["QQQ"] == pytest.approx(200)

    def test_a_holding_past_the_band_is_traded_back(self) -> None:
        m = with_fund(market(), "QQQ", {date(2000, 1, 10): 0.50})
        book = {
            E_JAN: {"SPY": 0.5, "SSO": 0.3, "QQQ": 0.2},
            E_FEB: {"SPY": 0.5, "SSO": 0.3, "QQQ": 0.2},
        }
        result = simulate_portfolio(
            m,
            PRIMARY,
            ZERO,
            start=R_DEC,
            end=END,
            equity=100_000.0,
            tax_rate=0.0,
            book=book,
            sell_at_end=False,
        )
        # QQQ rose to 30k of 110k (27%): more than 5 points over its 20%, so it is sold back.
        # SPY (45%) and SSO (27%) are within 5 points of theirs: untouched, the sale in cash.
        sold = {t.symbol: t.units for t in result.trades if t.day == E_FEB}
        assert set(sold) == {"QQQ"}
        assert sold["QQQ"] * 150 == pytest.approx(-(30_000 - 0.2 * 110_000))
        assert result.end is not None
        assert result.end.cash == pytest.approx(8_000)

    def test_an_e_without_targets_takes_the_rules(self) -> None:
        m = with_fund(market(), "QQQ")
        twin = simulate_portfolio(
            m,
            PRIMARY,
            ZERO,
            start=R_DEC,
            end=END,
            equity=100_000.0,
            tax_rate=0.25,
            sell_at_end=False,
        )
        book = simulate_portfolio(
            m,
            PRIMARY,
            ZERO,
            start=R_DEC,
            end=END,
            equity=100_000.0,
            tax_rate=0.25,
            book={},
            sell_at_end=False,
        )
        assert book.trades == twin.trades
        assert book.marks == twin.marks
        assert run(m).snapshots == []  # the backtest's runs keep none

    def test_a_book_fund_without_prices_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="no prices for"):
            run(market(), book={E_JAN: {"QQQ": 0.2}})

    def test_a_run_can_stop_on_its_first_ranking_session(self) -> None:
        m = market()
        # The market ends on R: its E isn't in it, so only ``rank_at_end`` ranks R.
        cut = replace(
            m,
            days=m.days[: R_DEC + 1],
            prices={k: v[: R_DEC + 1] for k, v in m.prices.items()},
            payouts={k: v[: R_DEC + 1] for k, v in m.payouts.items()},
            index=m.index[: R_DEC + 1],
            returns=m.returns[: R_DEC + 1],
            rf=m.rf[: R_DEC + 1],
            per_year=m.per_year[: R_DEC + 1],
        )
        result = simulate_portfolio(
            cut,
            PRIMARY,
            ZERO,
            start=R_DEC,
            end=R_DEC,
            equity=1.0,
            tax_rate=0.0,
            sell_at_end=False,
            rank_at_end=True,
        )
        assert [r.day for r in result.rankings] == [date(1999, 12, 31)]
        assert result.rankings[0].level == ZERO.up_level
        assert result.end is not None
        assert result.end.cash == 1.0
        assert result.end.targets is None
        with pytest.raises(ValueError, match="start and end"):
            simulate_portfolio(
                market(), PRIMARY, ZERO, start=R_DEC, end=R_DEC, equity=1.0, tax_rate=0.0
            )
        with pytest.raises(ValueError, match="rank_at_end needs"):
            simulate_portfolio(
                m, PRIMARY, ZERO, start=0, end=R_DEC, equity=1.0, tax_rate=0.0, rank_at_end=True
            )

    def test_rank_at_end_on_a_ranking_session_already_ranked_adds_nothing(self) -> None:
        m = market()
        kwargs = {"start": R_DEC, "equity": 1.0, "tax_rate": 0.0, "sell_at_end": False}
        plain = simulate_portfolio(m, PRIMARY, ZERO, end=AT[date(2000, 1, 31)], **kwargs)
        ranked = simulate_portfolio(
            m, PRIMARY, ZERO, end=AT[date(2000, 1, 31)], rank_at_end=True, **kwargs
        )
        assert ranked.rankings == plain.rankings

    def test_the_market_needs_the_core_funds(self) -> None:
        m = market()
        broken = replace(m, prices={k: v for k, v in m.prices.items() if k != "SSO"})
        with pytest.raises(ValueError, match="the market needs"):
            run(broken)


DAYS_OCT = [date(2026, 10, d) for d in (26, 27, 28, 29, 30)]


class TestFundsMarket:
    def closes(self, values: list[float]) -> dict[date, float]:
        return dict(zip(DAYS_OCT, values, strict=True))

    def test_a_distribution_is_the_total_return_over_the_price_return(self) -> None:
        split = {s: self.closes([100, 101, 100, 100, 102]) for s in ("SPY", "SSO", "BIL")}
        tr = dict(split)
        tr["SPY"] = self.closes([100, 101, 101, 101, 103.02])  # 1% paid on the 28th
        m = funds_market(split, tr, ZERO)

        assert m.days == DAYS_OCT
        assert m.payouts["SPY"][2] == pytest.approx(0.01)
        assert m.payouts["SPY"][4] == pytest.approx(0.0, abs=1e-12)
        assert m.payouts["SSO"] == [0.0] * 5
        assert m.prices["SPY"] == [100, 101, 100, 100, 102]
        assert m.index == [100, 101, 101, 101, 103.02]
        assert m.returns[1] == pytest.approx(0.01)
        assert m.rf[1] == pytest.approx(0.01)

    def test_a_missing_bar_carries_the_last_close(self) -> None:
        split = {s: self.closes([10, 11, 12, 13, 14]) for s in ("SPY", "SSO", "BIL")}
        split["SSO"] = {d: c for d, c in split["SSO"].items() if d != DAYS_OCT[2]}
        m = funds_market(split, split, ZERO, start=DAYS_OCT[1])
        assert m.days == DAYS_OCT[1:]
        assert m.prices["SSO"] == [11, 11, 13, 14]
        assert m.payouts["SSO"] == [0.0] * 4

    def test_a_fund_must_have_the_first_session(self) -> None:
        split = {s: self.closes([10, 11, 12, 13, 14]) for s in ("SPY", "SSO", "BIL")}
        split["SSO"] = {d: c for d, c in split["SSO"].items() if d != DAYS_OCT[0]}
        with pytest.raises(ValueError, match="SSO has no close"):
            funds_market(split, split, ZERO)

    def test_every_fund_needs_both_sets_and_enough_sessions(self) -> None:
        split = {s: self.closes([10, 11, 12, 13, 14]) for s in ("SPY", "SSO", "BIL")}
        with pytest.raises(ValueError, match="split and total-return"):
            funds_market(split, {k: v for k, v in split.items() if k != "BIL"}, ZERO)
        with pytest.raises(ValueError, match="too few sessions"):
            funds_market(split, split, ZERO, start=DAYS_OCT[-1])


def test_each_close_carries_the_tax_a_sale_would_owe() -> None:
    m = with_fund(market(), "QQQ", {date(2000, 1, 10): 0.10})
    result = simulate_portfolio(
        m,
        PRIMARY,
        ZERO,
        start=R_DEC,
        end=AT[date(2000, 1, 28)],
        equity=100_000.0,
        tax_rate=0.25,
        book={E_JAN: {"SPY": 0.5, "SSO": 0.3, "QQQ": 0.2}},
        sell_at_end=False,
    )
    first, last = result.snapshots[0], result.snapshots[-1]
    assert (first.tax_owed, first.realized) == (0.0, 0.0)  # cash at R
    # QQQ's 2,000 gain is the only one: 25% of it if sold.
    assert last.tax_owed == pytest.approx(500.0)
    assert last.realized == 0.0
