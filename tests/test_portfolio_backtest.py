"""The portfolio backtest's harness and report , on hand-built numbers and end to end
on a synthetic history: no real data is read."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from auratrade.config import RiskConfig, load_risk_config
from auratrade.history.benchmarks import Benchmark
from auratrade.history.long_history import LongHistory
from auratrade.sim.portfolio import PortfolioRun, Ranking, simulate_portfolio
from auratrade.strategies.core_allocation import Trend
from backtest import portfolio as pf
from backtest import portfolio_report as pr
from backtest.portfolio_render import render_blind, render_scored

RISK = load_risk_config()
BT = RISK.portfolio.backtest
SMALL: RiskConfig = RISK.model_copy(
    update={
        "portfolio": RISK.portfolio.model_copy(
            update={"backtest": BT.model_copy(update={"window_years": 2, "bootstrap_samples": 40})}
        )
    }
)
FORBIDDEN = ("cagr", "equity", "return", "drawdown", "excess", "volatility", "profit", "p&l")


def weekdays(first: date, last: date) -> list[date]:
    out, day = [], first
    while day <= last:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def synthetic() -> LongHistory:
    """1926-07 to 1933-12: slow waves, a crash in late 1929, and dividends that stop in
    1933-10 (carried forward, #9)."""
    days = weekdays(date(1926, 7, 1), date(1933, 12, 29))
    market = []
    for t, day in enumerate(days):
        r = 0.0004 + 0.012 * math.sin(2 * math.pi * t / 400)
        if date(1929, 10, 21) <= day <= date(1929, 11, 13):
            r = -0.03
        market.append(r)
    months = []
    day = date(1926, 6, 1)
    while day <= date(1933, 12, 1):
        months.append(day)
        day = date(day.year + day.month // 12, day.month % 12 + 1, 1)
    dividends: list[float | None] = [0.5] * len(months)
    dividends[-3:] = [None, None, None]
    return LongHistory(
        "built by the test",
        "2026-10-27T00:00:00+00:00",
        days,
        market,
        [0.0001] * len(days),
        months,
        [10.0] * len(months),
        dividends,
    )


def run_of(marks: list[float], days: list[date], final: float) -> PortfolioRun:
    run = PortfolioRun(marks[0], days=days, marks=marks)
    run.final_wealth = final
    return run


class TestNumbers:
    def test_cagr_and_drawdown(self) -> None:
        assert pr.cagr(100, 121, 2) == pytest.approx(0.1)
        assert pr.cagr(100, 0, 2) == -1.0
        with pytest.raises(ValueError, match="positive"):
            pr.cagr(0, 1, 1)
        assert pr.max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)
        assert pr.volatility([100.0], 252) == 0.0

    def test_calendar_years_and_decades(self) -> None:
        days = [date(1999, 12, 31), date(2000, 6, 30), date(2000, 12, 29), date(2001, 12, 31)]
        marks = [100.0, 150.0, 110.0, 121.0]
        assert pr.calendar_years(days, marks) == pytest.approx({2000: 0.1, 2001: 0.1})
        decades = pr.decades(run_of(marks, days, 0), run_of(marks, days, 0))
        # The 1990s hold no complete year here; the 2000s run from the end of 1999.
        assert decades == [("2000s", pytest.approx(0.1), pytest.approx(0.1))]

    def test_percentile(self) -> None:
        assert pr.percentile([3.0, 1.0, 2.0], 0.5) == 2.0
        assert pr.percentile([0.0, 10.0], 0.1) == pytest.approx(1.0)
        with pytest.raises(ValueError, match="no values"):
            pr.percentile([], 0.5)

    def test_falls_and_recoveries(self) -> None:
        days = [date(2000, 1, 3) + timedelta(days=30 * k) for k in range(5)]
        marks = [100.0, 80.0, 90.0, 101.0, 70.0]
        fall, months = pr.fall_and_recovery(days, marks, days[0], days[2])
        assert fall == pytest.approx(0.2)
        assert months == pytest.approx(60 / (365.25 / 12))
        fall, months = pr.fall_and_recovery(days, marks, days[4], days[4])
        assert (fall, months) == (pytest.approx(1 - 70 / 101), None)
        assert pr.fall_and_recovery(days, marks, date(1990, 1, 1), date(1990, 2, 1)) == (0, 0)

    def test_the_bootstrap_of_no_excess(self) -> None:
        marks = [100.0 * 1.001**k for k in range(400)]
        boot = pr.block_bootstrap(marks, marks, BT)
        assert (boot.mean, boot.low, boot.high) == (0.0, 0.0, 0.0)


def metrics(cagr: float, dd: float) -> pr.Metrics:
    return pr.Metrics(cagr, cagr, dd, 0.2, -0.3, 0.5, 0.01, 0.6, 0.4, 0.1, 0.0, 0, 0, 0)


def windows(ahead: int, behind: int) -> list[pr.Window]:
    w = [pr.Window(date(1930, 1, 1), date(1940, 1, 1), 2.0, 1.0)] * ahead
    return w + [pr.Window(date(1930, 1, 1), date(1940, 1, 1), 1.0, 2.0)] * behind


class TestGate:
    def test_a_pass(self) -> None:
        g = pr.gate(metrics(0.11, 0.30), metrics(0.10, 0.50), windows(7, 3), BT)
        assert (g.passed, g.verdict, g.window_share) == (True, "PASS", 0.7)
        assert windows(1, 0)[0].excess > 0

    def test_a_near_miss_needs_the_drawdown_and_a_small_gap(self) -> None:
        g = pr.gate(metrics(0.095, 0.30), metrics(0.10, 0.50), windows(4, 6), BT)
        assert (g.g1, g.g2, g.g3, g.verdict) == (False, True, False, "NEAR MISS")
        g = pr.gate(metrics(0.085, 0.30), metrics(0.10, 0.50), windows(9, 1), BT)
        assert g.verdict == "CLEAR FAIL"
        g = pr.gate(metrics(0.12, 0.60), metrics(0.10, 0.50), windows(9, 1), BT)
        assert (g.g2, g.verdict) == (False, "CLEAR FAIL")
        assert pr.gate(metrics(0.12, 0.3), metrics(0.1, 0.5), [], BT).window_share == 0.0

    def test_return_over_drawdown(self) -> None:
        assert metrics(0.1, 0.5).return_over_drawdown == pytest.approx(0.2)
        assert metrics(0.1, 0.0).return_over_drawdown == math.inf


class TestBlindPieces:
    def test_data_checks(self) -> None:
        proxy = [100.0, 101.0, 100.5, 102.0]
        good = pr.data_check("same", proxy, proxy, 1.0, BT)
        assert (good.passed, good.correlation, good.sessions) == (True, pytest.approx(1.0), 4)
        off = pr.data_check("off", proxy, [100.0, 99.0, 100.5, 98.0], 1.0, BT)
        assert not off.passed

    def test_gaps_and_stops(self) -> None:
        days = [date(1933, 3, 3), date(1933, 3, 15), date(1950, 1, 3), date(1950, 1, 20)]
        gaps = pr.session_gaps(days, BT)
        assert [(a, known) for a, _, known in gaps] == [
            (date(1933, 3, 3), True),
            (date(1933, 3, 15), False),
            (date(1950, 1, 3), False),
        ]
        counts = pr.BlindCounts(5.0, 0.4, 3.0, 1, 0.5, 0.5, 0.1, 0.4, 9)
        bad = pr.DataCheck("x", 0.5, 0.1, 10, False)
        stops = pr.blind_stops(counts, [bad], gaps[2:], BT)
        assert len(stops) == 4
        quiet = pr.BlindCounts(1.0, 0.0, 3.0, 0, 0.5, 0.5, 0.0, 0.0, 9)
        assert pr.blind_stops(quiet, [], [], BT) == []

    def test_blind_counts(self) -> None:
        days = [date(1930, 1, 31), date(1931, 1, 30)]
        run = run_of([1.0, 1.0], days, 1.0)
        run.rankings = [
            Ranking(days[0], Trend.UP, True, False, 1.0, False),
            Ranking(days[1], Trend.DOWN, False, True, 0.5, True),
        ]
        c = pr.blind_counts(run)
        assert (c.up, c.down, c.capped, c.halted, c.halt_share) == (0.5, 0.5, 0.5, 0.5, 0.5)
        assert c.flips_a_year == pytest.approx(1 / pr.years_between(*days))


class TestMarket:
    def test_dividend_yields_carry_forward(self) -> None:
        history = synthetic()
        yields = pf.dividend_yields(history)
        assert set(yields) == {0.05}
        late = replace(history, months=[date(1930, 1, 1)], prices=[10.0], dividends=[0.5])
        with pytest.raises(ValueError, match="no dividend yield"):
            pf.dividend_yields(late)

    def test_the_period_and_its_windows(self) -> None:
        market = pf.build_market(synthetic(), SMALL.portfolio)
        period = pf.scored_period(market.days, SMALL.portfolio)
        assert market.days[period.start] == date(1927, 12, 30)
        assert market.days[period.end] == date(1933, 12, 29)
        spans = pf.windows(market.days, period, SMALL.portfolio)
        # 73 month-ends from 1927-12 to 1933-12, less the 24 a 2-year window spans.
        assert len(spans) == 73 - 24
        assert market.days[spans[0].end] == date(1929, 12, 31)

    def test_configurations(self) -> None:
        primary, compared, diagnostics = pf.configurations(RISK.portfolio)
        assert primary.name == "primary"
        assert [c.name for c in compared] == ["C1", "C2", "C3", "C4", "C5"]
        assert [c.name for c in diagnostics] == ["D-a", "D-b", "D-c6", "D-c11", "D-c16"]
        assert compared[1].cfg.up_level == 1.5
        assert compared[1].cfg.down_level == 0.0


class TestEndToEnd:
    def test_the_scored_run(self) -> None:
        history = synthetic()
        s = pf.scored(history, SMALL)
        market = pf.build_market(history, SMALL.portfolio)
        direct = simulate_portfolio(
            market,
            pf.configurations(SMALL.portfolio)[0].rules,
            SMALL.portfolio,
            start=s.period.start,
            end=s.period.end,
            equity=100_000,
            tax_rate=0.25,
        )
        assert s.primary.after.marks == direct.marks
        assert len(s.windows) == 49
        assert len(s.compared) == 5
        assert s.published == []
        text = render_scored(s, SMALL, "2026-10-27 12:00 UTC")
        for heading in ("## The gate", "## Performance", "## The crashes", "## Reading"):
            assert heading in text
        assert f"`{pf.PREREGISTRATION_COMMIT}`" in text
        assert f"**Verdict: {s.gate.verdict}.**" in text

    def test_the_periods_after_publication(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pf, "PUBLISHED", (("a test", date(1931, 1, 1)),))
        s = pf.scored(synthetic(), SMALL)
        assert [(n, since) for n, since, _, _ in s.published] == [("a test", date(1931, 1, 1))]
        assert "1931-01-01 (a test)" in render_scored(s, SMALL, "now")

    def test_the_blind_check_prints_no_returns(self) -> None:
        history = synthetic()
        market = pf.build_market(history, SMALL.portfolio)
        funds = {
            "SPY": Benchmark("SPY", market.days, market.index),
            "SSO": Benchmark("SSO", market.days, market.prices["SSO"]),
        }
        b = pf.blind(history, funds, SMALL, checks_from=date(1930, 1, 1))
        assert all(c.passed for c in b.checks)
        assert b.window_count == 49
        text = render_blind(b, SMALL, "now")
        for word in FORBIDDEN:
            assert word not in text.lower(), word
        assert "## Stop conditions" in text
        assert ("None fired" in text) == (not b.stops)

    def test_a_fired_stop_is_reported(self) -> None:
        history = synthetic()
        market = pf.build_market(history, SMALL.portfolio)
        flat = Benchmark("SPY", market.days, [100.0 + (k % 2) for k in range(len(market.days))])
        b = pf.blind(history, {"SPY": flat, "SSO": flat}, SMALL, checks_from=date(1930, 1, 1))
        assert b.stops
        assert "**The work stops here" in render_blind(b, SMALL, "now")


class TestMain:
    def test_the_scored_run_is_one_shot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = tmp_path / "portfolio.md"
        existing.write_text("already run", encoding="utf-8")
        monkeypatch.setattr(pf, "REPORT", existing)
        assert pf.main([]) == 2
        assert existing.read_text(encoding="utf-8") == "already run"

    def test_the_blind_run_writes_its_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        history = synthetic()
        market = pf.build_market(history, SMALL.portfolio)
        funds = {
            "SPY": Benchmark("SPY", market.days, market.index),
            "SSO": Benchmark("SSO", market.days, market.prices["SSO"]),
        }
        monkeypatch.setattr(pf, "load_long_history", lambda store: history)
        monkeypatch.setattr(pf, "load_benchmarks", lambda store, name: funds)
        monkeypatch.setattr(pf, "load_risk_config", lambda path: SMALL)
        monkeypatch.setattr(pf, "CHECKS_FROM", date(1930, 1, 1))
        monkeypatch.setattr("auratrade.storage.make_store", lambda settings: None)
        report = tmp_path / "blind.md"
        assert pf.main(["--blind", "--report", str(report)]) == 0
        assert report.read_text(encoding="utf-8").startswith("# The portfolio manager: the blind")
