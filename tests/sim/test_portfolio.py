"""Known-answer portfolios for `sim/portfolio.py` (pre-registration §3).

Costs are zeroed (no fees, spreads or slippage) so every number can be checked by hand; the
synthesis has its own tests with the costs on. Sessions are weekdays; R is the last session of a
month and E the first of the next.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from itertools import pairwise

import pytest

from auratrade.config import PortfolioConfig, load_risk_config
from auratrade.sim.portfolio import (
    BENCHMARK,
    CASH,
    LeverageMode,
    Market,
    PortfolioInvariantError,
    PortfolioRun,
    Ranking,
    Rules,
    TrendRule,
    _Simulation,
    simulate_portfolio,
    synthesize,
)
from auratrade.strategies.core_allocation import Trend

CFG = load_risk_config().portfolio
ZERO = CFG.model_copy(
    update={
        "backtest": CFG.backtest.model_copy(
            update={
                "market_fee": 0.0,
                "leveraged_fee": 0.0,
                "cash_fee": 0.0,
                "financing_spread": 0.0,
                "market_slippage": 0.0,
                "leveraged_slippage": 0.0,
                "cash_slippage": 0.0,
                "margin_spread": 0.0,
            }
        )
    }
)
FIRST = date(1999, 1, 4)
PRIMARY = Rules()


def weekdays(n: int, first: date = FIRST) -> list[date]:
    out: list[date] = []
    day = first
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


DAYS = weekdays(820)  # 1999-01-04 to 2002-02
AT = {d: i for i, d in enumerate(DAYS)}
R_DEC = AT[date(1999, 12, 31)]


def market(
    moves: dict[date, float] | None = None,
    *,
    rf: float = 0.0,
    dividend: float = 0.0,
    cfg: PortfolioConfig = ZERO,
) -> Market:
    """Flat, except the market's total return on the days in ``moves``."""
    returns = [0.0] * len(DAYS)
    for day, move in (moves or {}).items():
        returns[AT[day]] = move
    return synthesize(DAYS, returns, [rf] * len(DAYS), [dividend] * len(DAYS), cfg)


def run(
    m: Market,
    rules: Rules = PRIMARY,
    *,
    end: date = date(2000, 3, 31),
    start: int = R_DEC,
    tax: float = 0.25,
    cfg: PortfolioConfig = ZERO,
) -> PortfolioRun:
    return simulate_portfolio(
        m, rules, cfg, start=start, end=AT[end], equity=100_000.0, tax_rate=tax
    )


def trades(result: PortfolioRun, reason: str) -> dict[str, float]:
    return {t.symbol: t.units for t in result.trades if t.reason == reason}


class TestSynthesis:
    def test_the_funds_from_one_session(self) -> None:
        days = [date(2000, 1, 3), date(2000, 1, 4)]
        m = synthesize(days, [0.01, -0.02], [0.002, 0.002], [0.02, 0.02], CFG)
        bt = CFG.backtest
        assert m.per_year == [2, 2]
        total = 0.01 - bt.market_fee / 2
        assert m.payouts["SPY"][0] == pytest.approx(0.01)
        assert m.prices["SPY"][0] == pytest.approx(100 * (1 + total - 0.01))
        lev = 2 * 0.01 - (0.002 + bt.financing_spread / 2) - bt.leveraged_fee / 2
        assert m.prices["SSO"][0] == pytest.approx(100 * (1 + lev))
        assert m.payouts["SSO"] == [0.0, 0.0]
        assert m.prices["BIL"] == [100.0, 100.0]
        assert m.payouts["BIL"][0] == pytest.approx(0.002 - bt.cash_fee / 2)
        assert m.index[0] == pytest.approx(100 * (1 + total))
        assert m.returns[1] == pytest.approx(-0.02 - bt.market_fee / 2)

    def test_a_negative_net_interest_lowers_the_cash_fund_price(self) -> None:
        m = synthesize([date(2000, 1, 3)], [0.0], [0.0], [0.0], CFG)
        assert m.payouts["BIL"] == [0.0]
        assert m.prices["BIL"][0] == pytest.approx(100 * (1 - CFG.backtest.cash_fee))

    def test_bad_series(self) -> None:
        with pytest.raises(ValueError, match="as long as"):
            synthesize([date(2000, 1, 3)], [0.0, 0.0], [0.0], [0.0], CFG)
        with pytest.raises(ValueError, match="whole value"):
            synthesize([date(2000, 1, 3)], [-0.6], [0.0], [0.0], CFG)


class TestBenchmark:
    def test_bought_once_held_and_taxed_at_the_end(self) -> None:
        result = run(market({date(2000, 2, 15): 0.10}), BENCHMARK)
        assert [(t.symbol, t.reason) for t in result.trades] == [
            ("SPY", "REBALANCE"),
            ("SPY", "END"),
        ]
        assert result.final_wealth == pytest.approx(110_000 - 0.25 * 10_000)
        assert max(result.marks) == pytest.approx(110_000)
        assert result.rankings[0].level == 1.0

    def test_dividends_are_taxed_and_losses_dont_offset_them(self) -> None:
        # 2.4% a year, no total return: the price falls by what is paid out.
        result = run(market(dividend=0.024), BENCHMARK, end=date(2000, 12, 29))
        income = result.taxes[-1].income
        # A year's sessions after the first E, on about 100,000.
        assert income == pytest.approx(100_000 * 0.024, rel=0.01)
        # The final sale realizes a loss (the price fell); it can't offset the income.
        assert result.taxes[-1].realized < 0
        assert result.final_wealth == pytest.approx(100_000 - 0.25 * income, rel=1e-3)


class TestTrendAndLevels:
    def test_up_then_down_at_the_next_rebalance(self) -> None:
        result = run(market({date(2000, 1, 10): -0.10}))
        first, second = result.rankings[:2]
        assert (first.state, first.level, first.flipped) == (Trend.UP, 1.3, False)
        assert (second.state, second.level, second.flipped) == (Trend.DOWN, 0.5, True)
        assert result.flips == 1
        buys = [t for t in result.trades if t.day == date(2000, 1, 3)]
        assert {t.symbol: t.units for t in buys} == pytest.approx({"SPY": 700, "SSO": 300})
        rebalance = {t.symbol: t.units for t in result.trades if t.day == date(2000, 2, 1)}
        # Equity 87,000: SPY to 43,500 at 90, SSO sold at 80, BIL to 43,500.
        assert rebalance == pytest.approx({"SPY": -(700 - 43_500 / 90), "SSO": -300, "BIL": 435})
        # Losses only: no tax; nothing moves after.
        assert result.final_wealth == pytest.approx(87_000)
        assert result.tax_paid == 0

    def test_no_trade_inside_the_drift_band(self) -> None:
        result = run(market({date(2000, 1, 10): 0.03}))
        assert {t.day for t in result.trades if t.reason == "REBALANCE"} == {date(2000, 1, 3)}

    def test_a_drifted_holding_is_rebalanced(self) -> None:
        # SSO doubles: 30% grows to about 46% of equity, past the 5-point band.
        result = run(market({date(2000, 1, 10): 0.25}))
        days = sorted({t.day for t in result.trades if t.reason == "REBALANCE"})
        assert days[:2] == [date(2000, 1, 3), date(2000, 2, 1)]

    def test_the_volatility_cap(self) -> None:
        moves = {d: (0.025 if k % 2 else -0.02) for k, d in enumerate(DAYS[R_DEC - 20 : R_DEC + 1])}
        result = run(market(moves))
        first = result.rankings[0]
        assert (first.state, first.capped, first.level) == (Trend.UP, True, 1.0)
        first_e = {t.symbol: t.units for t in result.trades if t.day == date(2000, 1, 3)}
        assert first_e == pytest.approx({"SPY": 100_000 / result_price(moves)})

    def test_too_little_history(self) -> None:
        with pytest.raises(ValueError, match="too little history"):
            run(market(), start=AT[date(1999, 3, 31)])
        with pytest.raises(ValueError, match="too little history"):
            run(market(), Rules(trend=TrendRule.DAILY_SMA, sma_sessions=800))

    def test_the_daily_sma_of_comparison_c1(self) -> None:
        ramp = dict.fromkeys(DAYS[: R_DEC + 1], 0.001)
        result = run(market(ramp), Rules(trend=TrendRule.DAILY_SMA))
        assert (result.rankings[0].state, result.rankings[0].level) == (Trend.UP, 1.3)

    def test_a_constant_level(self) -> None:
        result = run(market(), Rules(trend=TrendRule.NONE, constant_level=1.3, vol_cap=False))
        assert {r.level for r in result.rankings} == {1.3}

    def test_a_constant_level_under_the_halt(self) -> None:
        rules = Rules(trend=TrendRule.NONE, constant_level=1.3, vol_cap=False, stop=False)
        result = run(market({date(2000, 1, 10): -0.20}), rules)
        assert [(r.halted, r.level) for r in result.rankings[:2]] == [(False, 1.3), (True, 1.0)]


def result_price(moves: dict[date, float]) -> float:
    price = 100.0
    for day in sorted(moves):
        price *= 1 + moves[day]
    return price


class TestProtection:
    def test_the_stop_sells_the_leveraged_fund_into_cash(self) -> None:
        result = run(market({date(2000, 1, 10): -0.16}))
        assert result.stopouts == [date(2000, 1, 10)]
        assert trades(result, "STOP") == pytest.approx({"SSO": -300, "BIL": 30_000 * 0.68 / 100})
        assert result.halt_starts == []

    def test_the_halt_sells_the_leveraged_fund_the_next_session(self) -> None:
        result = run(market({date(2000, 1, 10): -0.20}), Rules(stop=False))
        assert result.halt_starts == [date(2000, 1, 10)]
        halt = [t for t in result.trades if t.reason == "HALT"]
        assert {t.day for t in halt} == {date(2000, 1, 11)}
        assert {t.symbol: t.units for t in halt} == pytest.approx({"SSO": -300, "SPY": 225})
        later = result.rankings[1]
        assert (later.halted, later.capped, later.level) == (True, True, 0.5)

    def test_without_the_halt_nothing_is_sold_early(self) -> None:
        result = run(market({date(2000, 1, 10): -0.20}), Rules(stop=False, halt=False))
        assert result.halt_starts == []
        assert trades(result, "HALT") == {}

    def test_a_halt_with_nothing_leveraged_sells_nothing(self) -> None:
        # The stop has already sold the leveraged fund at the same close.
        result = run(market({date(2000, 1, 10): -0.20}))
        assert result.stopouts == [date(2000, 1, 10)]
        assert result.halt_starts == [date(2000, 1, 10)]
        assert trades(result, "HALT") == {}


def margin_market(drops: dict[date, float], rf: float = 0.0) -> Market:
    """The market fund only moves; the 2x fund is never held under margin."""
    prices, price = [], 100.0
    for day in DAYS:
        price *= 1 + drops.get(day, 0.0)
        prices.append(price)
    returns = [0.0] + [b / a - 1 for a, b in pairwise(prices)]
    flat = [100.0] * len(DAYS)
    zeros = [0.0] * len(DAYS)
    return Market(
        DAYS,
        {"SPY": prices, "SSO": flat, "BIL": flat},
        {"SPY": zeros, "SSO": zeros, "BIL": zeros},
        prices,
        returns,
        [rf] * len(DAYS),
        [261] * len(DAYS),
    )


MARGIN = Rules(leverage=LeverageMode.MARGIN)


class TestMargin:
    def test_leverage_by_borrowing(self) -> None:
        result = run(margin_market({}, rf=0.0001), MARGIN)
        assert trades(result, "REBALANCE") == pytest.approx({"SPY": 1300})
        # The loan of 30,000 grew with the interest; nothing else moved.
        days = AT[date(2000, 3, 31)] - AT[date(2000, 1, 3)]
        assert result.final_wealth == pytest.approx(130_000 - 30_000 * 1.0001**days)

    def test_a_margin_call_sells_to_one_times(self) -> None:
        result = run(margin_market({date(2000, 1, 10): -0.70}), MARGIN)
        assert result.margin_calls == [date(2000, 1, 10)]
        assert trades(result, "MARGIN_CALL") == pytest.approx({"SPY": -1000})
        assert result.marks[AT[date(2000, 1, 10)] - R_DEC] == pytest.approx(9_000)

    def test_the_halt_repays_the_loan(self) -> None:
        result = run(margin_market({date(2000, 1, 10): -0.22}), MARGIN)
        assert result.halt_starts == [date(2000, 1, 10)]
        assert trades(result, "HALT") == pytest.approx({"SPY": -30_000 / 78})

    def test_below_one_times_nothing_is_borrowed(self) -> None:
        down = dict.fromkeys(DAYS[: R_DEC + 1], -0.002)
        result = run(margin_market(down), MARGIN)
        assert result.rankings[0].level == 0.5
        assert set(trades(result, "REBALANCE")) == {"SPY", "BIL"}


class TestTax:
    def test_tax_is_paid_from_cash_first(self) -> None:
        down = dict.fromkeys(DAYS[: R_DEC + 1], -0.002)
        result = run(market(down, rf=0.0002), end=date(2001, 3, 30))
        assert result.rankings[0].level == 0.5
        year = next(t for t in result.taxes if t.year == 2000)
        assert year.income_tax == pytest.approx(0.25 * year.income)
        assert year.income > 0
        spy = [t.units for t in result.trades if t.symbol == "SPY"]
        # SPY untouched by the tax: what was bought is what was sold.
        assert sum(spy) == pytest.approx(0.0, abs=1e-6)
        assert result.tax_paid == pytest.approx(math.fsum(t.total for t in result.taxes))

    def test_tax_is_paid_pro_rata_without_cash(self) -> None:
        result = run(market(dividend=0.03), end=date(2001, 3, 30))
        sso = [t.units for t in result.trades if t.symbol == "SSO"]
        # SSO paid part of the tax, so less of it was left to sell.
        assert sso[0] == pytest.approx(300)
        assert -sso[-1] < 300

    def test_before_tax(self) -> None:
        result = run(market({date(2000, 2, 15): 0.04}), tax=0.0)
        assert result.tax_paid == 0
        assert result.final_wealth == pytest.approx(70_000 * 1.04 + 30_000 * 1.08)


class TestArguments:
    def test_start_and_end(self) -> None:
        m = market()
        with pytest.raises(ValueError, match="start and end"):
            simulate_portfolio(m, Rules(), ZERO, start=10, end=5, equity=1.0, tax_rate=0.0)
        with pytest.raises(ValueError, match="no ranking session"):
            simulate_portfolio(
                m,
                Rules(),
                ZERO,
                start=AT[date(2000, 1, 3)],
                end=AT[date(2000, 1, 20)],
                equity=1.0,
                tax_rate=0.0,
            )

    def test_a_window_starts_in_cash_at_its_first_ranking(self) -> None:
        result = run(market(), start=AT[date(1999, 12, 15)])
        assert result.days[0] == date(1999, 12, 31)
        assert result.marks[0] == 100_000


class TestInvariants:
    """Each invariant on a corrupted account: they guard against bugs, so no rule reaches them."""

    def sim(self) -> _Simulation:
        return _Simulation(market(), Rules(), ZERO, 100_000.0, 0.25, [])

    RANKING = Ranking(date(2000, 1, 31), Trend.UP, False, False, 1.3, False)

    def test_the_exposure_ceiling(self) -> None:
        high = ZERO.model_copy(update={"up_level": 1.6, "max_level": 1.6})
        with pytest.raises(PortfolioInvariantError, match="exposure"):
            run(market(), cfg=high.model_copy(update={"max_level": 1.5}))

    def test_a_negative_holding(self) -> None:
        sim = self.sim()
        sim.acct.units["SPY"] = -1.0
        with pytest.raises(PortfolioInvariantError, match="negative SPY"):
            sim._check_after_rebalance({"SPY": 1.0}, self.RANKING)

    def test_lots_that_dont_add_up(self) -> None:
        sim = self.sim()
        sim.acct.units["SPY"] = 5.0
        with pytest.raises(PortfolioInvariantError, match="lots"):
            sim._check_after_rebalance({"SPY": 1.0}, self.RANKING)

    def test_the_leveraged_fund_below_one_times(self) -> None:
        sim = self.sim()
        sim.acct.units["SSO"] = 1.0
        sim.acct.tax.buy("SSO", date(2000, 1, 3), 1.0, 100.0)
        with pytest.raises(PortfolioInvariantError, match="below 1x"):
            sim._check_after_rebalance({"SPY": 1.0}, self.RANKING)

    def test_a_loan_without_margin(self) -> None:
        sim = self.sim()
        sim.acct.cash = -5.0
        with pytest.raises(PortfolioInvariantError, match="without margin"):
            sim._check_after_rebalance({"SPY": 1.0}, self.RANKING)

    def test_nothing_is_bought_for_nothing(self) -> None:
        sim = self.sim()
        sim._buy(date(2000, 1, 3), "SPY", 0.0, {"SPY": 100.0}, "REBALANCE")
        assert (sim.run.trades, sim.acct.cash, CASH) == ([], 100_000.0, "CASH")
