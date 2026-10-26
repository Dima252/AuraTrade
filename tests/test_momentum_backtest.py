"""The momentum book's backtest harness and report (task 3.13): known answers on hand-built
series, and the whole run on a synthetic history."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from auratrade.calendar.sessions import Session
from auratrade.config import BookSignal, Regime, RiskConfig, load_risk_config
from auratrade.guardrails.blocks import EntryBlock
from auratrade.history.read import SecuritySeries
from auratrade.indicators.earnings import EarningsTiming
from auratrade.journal.scorecard import Interval
from auratrade.regime.classifier import RegimeResult
from auratrade.sim.book import (
    BookMarket,
    BookRun,
    BookSeries,
    BookTrade,
    ExitReason,
    RebalanceLog,
    SessionMark,
    simulate_book,
)
from auratrade.strategies.momentum_book import Skipped, SkipReason
from backtest import momentum as bm
from backtest import momentum_report as mr
from backtest.history_scan import History
from backtest.momentum_render import render_blind, render_holdout, render_scored

BASE = load_risk_config()
BT = BASE.book.backtest


def _days(first: date, last: date) -> list[date]:
    out, d = [], first
    while d <= last:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def curve(values: list[float], first: date = date(2021, 1, 4)) -> mr.Curve:
    days = _days(first, first + timedelta(days=len(values) * 2))[: len(values)]
    return mr.Curve(tuple(days), tuple(values))


# --- metrics --------------------------------------------------------------------------------


class TestMetrics:
    def test_cagr_counts_sessions(self) -> None:
        assert mr.cagr([100.0] * 252 + [110.0]) == pytest.approx(0.10)
        assert mr.cagr([100.0]) == 0.0

    def test_max_drawdown_on_closes(self) -> None:
        assert mr.max_drawdown([100.0, 120.0, 90.0, 130.0, 117.0]) == pytest.approx(0.25)
        assert mr.max_drawdown([1.0, 2.0, 3.0]) == 0.0

    def test_performance_and_calmar(self) -> None:
        p = mr.performance(curve([100.0, 120.0, 90.0] + [90.0] * 249 + [110.0]))
        assert p.max_drawdown == pytest.approx(0.25)
        assert p.calmar == pytest.approx(0.10 / 0.25)
        assert mr.performance(curve([100.0, 101.0])).calmar is None

    def test_years_run_from_the_last_close_of_the_year_before(self) -> None:
        c = mr.Curve(
            (date(2020, 12, 30), date(2020, 12, 31), date(2021, 6, 1), date(2021, 12, 31)),
            (100.0, 110.0, 99.0, 121.0),
        )
        flat = mr.Curve(c.dates, (1.0,) * 4)
        rows = mr.by_year(c, flat, flat)
        assert [(r.year, round(r.book, 4)) for r in rows] == [(2020, 0.1), (2021, 0.1)]
        assert rows[0].excess == pytest.approx(0.1)

    def test_aligned_carries_a_missing_close(self) -> None:
        days = [date(2021, 1, d) for d in (4, 5, 6)]
        assert mr.aligned({days[0]: 1.0, days[2]: 3.0}, days).values == (1.0, 1.0, 3.0)
        with pytest.raises(ValueError, match="no benchmark close"):
            mr.aligned({days[1]: 1.0}, days)

    def test_differences_need_the_same_sessions(self) -> None:
        a, b = curve([1.0, 1.1]), curve([1.0, 1.05])
        assert list(mr.differences(a, b).values()) == [pytest.approx(0.05)]
        with pytest.raises(ValueError, match="share"):
            mr.differences(a, curve([1.0, 1.0, 1.0]))

    def test_the_interval_and_the_detectable_effect(self) -> None:
        days = _days(date(2021, 1, 4), date(2021, 12, 31))
        constant = dict.fromkeys(days, 0.001)
        interval = mr.annual_interval(constant, BT)
        assert interval is not None
        assert interval.low == pytest.approx(0.252)
        assert interval.high == pytest.approx(0.252)
        assert mr.min_detectable(constant) == pytest.approx(0.0)
        assert mr.min_detectable({}) == math.inf


# --- the gate and the comparisons ------------------------------------------------------------

YEARS = [date(y, 12, 31) for y in range(2016, 2023)]


def yearly(returns: list[float]) -> mr.Curve:
    """A curve with one close a year (2016's is the start), compounding ``returns``."""
    values = [100.0]
    for r in returns:
        values.append(values[-1] * (1 + r))
    return mr.Curve(tuple(YEARS), tuple(values))


class TestGate:
    def test_a_pass(self) -> None:
        g = mr.gate(
            yearly([0.2, 0.15, 0.3, 0.15, 0.2, -0.05]), yearly([0.1] * 6), yearly([0.1] * 6), BT
        )
        assert (g.g1, g.g2, g.g3, g.g4, g.passed) == (True, True, True, True, True)
        assert g.positive_years == 5

    def test_g3_fails_without_the_best_year(self) -> None:
        # One huge year carries a book that trails SPY in every other.
        book = yearly([2.0, 0.05, 0.05, 0.05, 0.05, 0.05])
        g = mr.gate(book, yearly([0.1] * 6), yearly([0.0] * 6), BT)
        assert g.g1
        assert g.positive_years == 1
        assert not g.g3
        assert g.without_best < 0

    def test_g4_fails_on_a_deep_drawdown(self) -> None:
        book = yearly([0.6, -0.5, 0.6, 0.6, 0.3, 0.2])
        g = mr.gate(book, yearly([0.1] * 6), yearly([0.1] * 6), BT)
        assert g.max_drawdown == pytest.approx(0.5)
        assert not g.g4
        assert not g.passed

    def test_g1_g2_fail_behind_the_benchmarks(self) -> None:
        g = mr.gate(yearly([0.05] * 6), yearly([0.1] * 6), yearly([0.1] * 6), BT)
        assert (g.g1, g.g2) == (False, False)

    def test_g4_with_a_benchmark_that_never_fell(self) -> None:
        g = mr.gate(
            yearly([0.2, -0.1, 0.2, 0.2, 0.2, 0.2]), yearly([0.1] * 6), yearly([0.1] * 6), BT
        )
        assert g.spy_calmar is None
        assert g.g4


def daily(values: list[float]) -> mr.Curve:
    return curve(values)


class TestAdoption:
    def _pair(self, edge: float, dip: float = 0.0) -> tuple[mr.Curve, mr.Curve]:
        n = 130
        primary = [100.0 * (1 + 0.001 * math.sin(i)) for i in range(n)]
        primary[60] *= 0.9
        variant = [p * (1 + edge) ** i for i, p in enumerate(primary)]
        variant[60] *= 1 - dip
        return daily(variant), daily(primary)

    def test_a_better_variant_with_no_deeper_drawdown_is_adopted(self) -> None:
        variant, primary = self._pair(0.001)
        j = mr.judge("C1", mr.Kind.CHANGE, variant, primary, BT)
        assert j.mean > 0
        assert j.interval is not None
        assert j.interval.low > 0
        assert j.passed

    def test_a_deeper_drawdown_blocks_adoption(self) -> None:
        variant, primary = self._pair(0.001, dip=0.2)
        assert not mr.judge("C2", mr.Kind.CHANGE, variant, primary, BT).passed

    def test_a_safety_rule_stays_unless_removing_it_is_clearly_better(self) -> None:
        variant, primary = self._pair(0.0)
        assert not mr.judge("C3", mr.Kind.REMOVE_SAFETY, variant, primary, BT).passed

    def test_c5_needs_a_shallower_drawdown_and_no_clear_loss(self) -> None:
        primary = daily([100.0, 90.0, 95.0, 100.0] * 20)
        # Shallower dips and a small drift: the mean daily difference (arithmetic, so a calmer
        # curve with the same end scores lower) isn't wholly below zero.
        calm = [100.0, 95.0, 97.5, 100.0] * 20
        shallower = daily([v * 1.003**i for i, v in enumerate(calm)])
        assert mr.judge("C5", mr.Kind.ADD_SAFETY, shallower, primary, BT).passed
        losing = daily([100.0 * 0.995**i for i in range(80)])
        assert not mr.judge("C5", mr.Kind.ADD_SAFETY, losing, primary, BT).passed

    def test_no_interval_adopts_nothing(self) -> None:
        j = mr.Judgement("C1", mr.Kind.CHANGE, 0.1, None, 0.1, 0.2)
        assert not j.passed

    def test_c3_and_c4_cannot_both_be_adopted(self) -> None:
        ok = Interval(0.01, 0.05)
        judged = [
            mr.Judgement("C2", mr.Kind.CHANGE, 0.02, ok, 0.1, 0.2),
            mr.Judgement("C3", mr.Kind.REMOVE_SAFETY, 0.03, ok, 0.1, 0.2),
            mr.Judgement("C4", mr.Kind.CHANGE, 0.04, ok, 0.1, 0.2),
            mr.Judgement("C6", mr.Kind.CHANGE, -0.01, Interval(-0.02, 0.01), 0.1, 0.2),
        ]
        assert mr.adopted(judged) == ["C2", "C4"]


# --- references ------------------------------------------------------------------------------


class TestReferences:
    def test_equal_weight_compounds_period_means(self) -> None:
        h = mr.Holding
        periods = [
            mr.Period(date(2021, 2, 1), date(2021, 3, 1), {"A": h(0.5, 1.1), "B": h(0.1, 0.9)}),
            mr.Period(date(2021, 3, 1), date(2021, 3, 31), {}),
            mr.Period(date(2021, 3, 31), date(2021, 4, 30), {"A": h(0.5, 1.2)}),
        ]
        ew = mr.equal_weight(periods, date(2021, 1, 29), 100.0)
        assert ew.values == (100.0, 100.0, 100.0, 120.0)

    def test_the_signal_check_is_a_monthly_rank_correlation(self) -> None:
        h = mr.Holding
        agree = {k: h(s, r) for k, s, r in (("A", 3, 1.3), ("B", 2, 1.2), ("C", 1, 1.1))}
        disagree = {k: h(s, r) for k, s, r in (("A", 3, 1.1), ("B", 2, 1.2), ("C", 1, 1.3))}
        check = mr.signal_check(
            [
                mr.Period(date(2021, 2, 1), date(2021, 3, 1), agree),
                mr.Period(date(2021, 3, 1), date(2021, 4, 1), agree),
                mr.Period(date(2021, 4, 1), date(2021, 5, 3), disagree),
                mr.Period(date(2021, 5, 3), date(2021, 6, 1), {"A": h(1, 1)}),  # too few
            ]
        )
        assert check.months == 3
        assert check.mean == pytest.approx(1 / 3)
        assert check.t == pytest.approx((1 / 3) / (math.sqrt(4 / 3) / math.sqrt(3)))
        assert mr.signal_check([]) == mr.SignalCheck(0, None, None)
        same = mr.Period(date(2021, 2, 1), date(2021, 3, 1), agree)
        assert mr.signal_check([same, same]).t is None
        flat = {k: h(1.0, r) for k, r in (("A", 1.1), ("B", 1.2), ("C", 1.3))}
        assert mr.spearman([(x.signal, x.relative) for x in flat.values()]) is None

    def test_returns_by_the_regime_of_the_evening_before(self) -> None:
        book = mr.Curve(tuple(_days(date(2021, 1, 4), date(2021, 1, 6))), (100.0, 110.0, 99.0))
        spy = mr.Curve(book.dates, (100.0, 100.0, 100.0))
        rows = mr.by_regime(book, spy, {book.dates[0]: Regime.CORRECTION})
        assert [(r.regime, r.sessions) for r in rows] == [(None, 1), (Regime.CORRECTION, 1)]
        assert rows[1].book == pytest.approx(0.1 * 252)
        assert rows[1].excess == pytest.approx(0.1 * 252)


# --- activity and the blind counts -----------------------------------------------------------


def _trade(entered: date, exited: date | None, reason: ExitReason | None) -> BookTrade:
    return BookTrade(
        "K", "K", 10, entered, 10.0, 100.0, 10.05, 8.0, exited, None, None, reason, slippage=1.0
    )


def _run() -> tuple[BookRun, dict[date, int]]:
    days = _days(date(2021, 1, 29), date(2022, 2, 4))
    position = {d: i for i, d in enumerate(days)}
    marks = tuple(SessionMark(d, 1000.0, 100.0, 1, 0.3) for d in days[:253])
    logs = (
        RebalanceLog(
            days[0],
            days[1],
            days[1],
            retry=False,
            blocks=frozenset({EntryBlock.DAILY_LOSS_HALT}),
            ranked=900,
            candidates=4,
            picks=2,
            skipped=(Skipped("X", SkipReason.PRICE_FIT), Skipped("Y", SkipReason.CLUSTER)),
            holdings=0,
            retried=True,
        ),
        RebalanceLog(
            days[0],
            days[1],
            days[2],
            retry=True,
            blocks=frozenset(),
            candidates=4,
            picks=2,
            bought=2,
            holdings=2,
        ),
        RebalanceLog(
            days[22],
            days[25],
            days[25],
            retry=False,
            blocks=frozenset(),
            ranked=700,
            candidates=2,
            picks=1,
            bought=1,
            holdings=3,
            unfilled=1,
        ),
    )
    trades = (
        _trade(days[2], days[12], ExitReason.STOP),
        _trade(days[2], None, None),
        _trade(days[25], days[260], ExitReason.ENDED_SUCCESSOR),
    )
    return BookRun(marks, trades, logs), position


class TestActivityAndBlind:
    def test_activity(self) -> None:
        run, position = _run()
        a = mr.activity(run, position)
        assert a.buys_per_year == pytest.approx(3.0)
        assert a.slippage_per_year == pytest.approx(3 / 1000)
        assert a.stop_outs_per_year == pytest.approx(1.0)
        assert a.mean_cash_share == pytest.approx(0.1)
        assert a.largest_weight == pytest.approx(0.3)
        assert a.mean_holding_sessions == pytest.approx((10 + 250 + 235) / 3)
        days = sorted(position)
        moved = mr.moved(run, {days[0]: days[1], days[22]: days[23]})
        assert (moved.half_day, moved.retry) == (1, 2)

    def test_blind_counts_per_year_and_overall(self) -> None:
        run, position = _run()
        counts = mr.blind_counts(run, position)
        overall = counts[None]
        assert (overall.executions, overall.retries, overall.buys) == (2, 1, 3)
        assert overall.ranked == [900, 700]
        assert sorted(overall.holdings) == [2, 3]  # E1's after its retry
        assert overall.reached == 2 + 2 + 2 + 1
        assert overall.price_skip_share == pytest.approx(1 / 7)
        assert overall.cluster_skip_share == pytest.approx(1 / (1 + 4 + 2))
        assert overall.blocked[EntryBlock.DAILY_LOSS_HALT] == 1
        assert overall.blocked_picks[EntryBlock.DAILY_LOSS_HALT] == 2
        assert overall.exits[ExitReason.ENDED_SUCCESSOR] == 1
        assert overall.unfilled == 1
        assert counts[2022].exits[ExitReason.ENDED_SUCCESSOR] == 1
        assert mr.BlindCounts().price_skip_share == 0.0
        assert mr.BlindCounts().cluster_skip_share == 0.0

    def test_stop_conditions(self) -> None:
        run, position = _run()
        fired = mr.stop_conditions(mr.blind_counts(run, position)[None], BT)
        assert len(fired) == 2
        assert "mean holdings" in fired[0]
        assert "700" in fired[1]
        quiet = mr.BlindCounts(holdings=[8, 8], ranked=[1000])
        assert mr.stop_conditions(quiet, BT) == []
        busy = mr.BlindCounts(reached=10, candidates=[1])
        busy.skips.update({SkipReason.PRICE_FIT: 3, SkipReason.CLUSTER: 2})
        assert [f.split()[1] for f in mr.stop_conditions(busy, BT)] == ["of", "cluster"]


# --- the harness's builders ------------------------------------------------------------------


class TestBuilders:
    def test_successors_are_the_next_piece_of_the_same_ticker(self) -> None:
        symbols = {"a1": "A", "a2": "A", "a3": "A", "b1": "B"}
        firsts = {
            "a2": date(2019, 1, 2),
            "a1": date(2016, 1, 4),
            "a3": date(2021, 5, 3),
            "b1": date(2016, 1, 4),
        }
        assert bm.successors(symbols, firsts) == {"a1": "a2", "a2": "a3"}

    def test_above_the_sma(self) -> None:
        days = _days(date(2021, 1, 4), date(2021, 1, 8))
        out = bm.above_sma(days, [10.0, 11.0, 12.0, 10.0, 13.0], 3)
        assert out == {days[2]: True, days[3]: False, days[4]: True}

    def test_reaction_sessions(self) -> None:
        days = _days(date(2021, 1, 4), date(2021, 1, 8))
        reports = [
            (date(2021, 1, 5), EarningsTiming.BEFORE_OPEN),
            (date(2021, 1, 5), EarningsTiming.AFTER_CLOSE),
            (date(2021, 1, 9), EarningsTiming.BEFORE_OPEN),  # a Saturday: the next session
            (date(2021, 1, 8), EarningsTiming.AFTER_CLOSE),  # reacts after the calendar
        ]
        assert bm.reactions(reports, days) == [date(2021, 1, 5), date(2021, 1, 6)]

    def test_configurations_change_one_thing_each(self) -> None:
        configs = bm.comparisons(BASE)
        assert [c.name for c in configs] == [f"C{i}" for i in range(1, 8)]
        changed = {
            c.name: {
                k for k, v in c.risk.book.model_dump().items() if v != BASE.book.model_dump()[k]
            }
            for c in configs
        }
        assert changed == {
            "C1": {"signal"},
            "C2": {"stop_fraction"},
            "C3": {"overlay"},
            "C4": {"overlay"},
            "C5": {"earnings_rules"},
            "C6": {"universe_size"},
            "C7": {"cluster_cap"},
        }
        both = bm.adopted_config(BASE, ["C1", "C2"]).risk.book
        assert (both.signal, both.stop_fraction) == (BookSignal.HIGH_52W, 0.10)
        extra = bm.diagnostics(BASE)
        assert [c.offset for c in extra] == [6, 11, 16, 1, 1]
        assert extra[-1].risk.sim.slippage[0].fraction == 0.0
        assert extra[-2].equity == BT.notional_equity

    def test_the_market_from_the_history(self) -> None:
        days = _days(date(2021, 1, 4), date(2021, 1, 8))
        series = {}
        for key, first in (("spy", 0), ("a1", 0), ("a2", 3), ("empty", 5)):
            s = SecuritySeries(key)
            for d in days[first:]:
                s.dates.append(d)
                for column in (s.open, s.high, s.low, s.close, s.raw_close, s.volume):
                    column.append(10.0)
            series[key] = s
        symbols = {"spy": "SPY", "a1": "A", "a2": "A", "empty": "E"}
        h = SimpleNamespace(
            series=series,
            indexes={"SPY": "spy"},
            symbol=symbols.__getitem__,
            members={days[0]: {"a1"}},
            groups={"a1": "X"},
            earnings={"a1": [(days[1], EarningsTiming.AFTER_CLOSE)]},
        )
        results = [
            SimpleNamespace(
                session=days[0], regime=Regime.CORRECTION, crisis=SimpleNamespace(active=True)
            ),
            SimpleNamespace(
                session=days[1],
                regime=Regime.CONFIRMED_UPTREND,
                crisis=SimpleNamespace(active=False),
            ),
        ]
        sessions = [Day(d) for d in days]
        m = bm.book_market(
            cast(History, h),
            cast(list[Session], sessions),
            cast(list[RegimeResult], results),
            SMALL,
        )
        assert sorted(m.series) == ["a1", "a2", "spy"]
        assert m.series["a2"].symbol == "A"
        assert m.successors == {"a1": "a2"}
        assert m.regimes == {days[0]: Regime.CORRECTION, days[1]: Regime.CONFIRMED_UPTREND}
        assert m.crisis == frozenset({days[0]})
        assert m.reactions == {"a1": [days[2]]}
        assert m.spy_above_sma == dict.fromkeys(days[4:], False)

    def test_the_holdout_refuses_an_unfrozen_configuration(self) -> None:
        with pytest.raises(SystemExit, match="frozen"):
            bm.check_frozen(BASE, None)
        with pytest.raises(SystemExit, match="frozen"):
            bm.check_frozen(BASE, "0" * 64)
        bm.check_frozen(BASE, BASE.config_hash)


# --- the whole run on a synthetic history ----------------------------------------------------


def _small_risk() -> RiskConfig:
    book = BASE.book.model_copy(
        update={
            "holdings": 3,
            "candidates": 4,
            "review_candidates": 4,
            "universe_size": 8,
            "formation_sessions": 5,
            "skip_sessions": 1,
            "hold_percentile": 50.0,
            "overlay_sma_sessions": 5,
            "backtest": BT.model_copy(
                update={
                    "bootstrap_samples": 200,
                    "blind_min_ranked": 5,
                    "blind_min_mean_holdings": 1,
                }
            ),
        }
    )
    return BASE.model_copy(
        update={
            "book": book,
            "universe": BASE.universe.model_copy(update={"dollar_volume_sessions": 2}),
            "sim": BASE.sim.model_copy(update={"volume_sessions": 2}),
            "guardrails": BASE.guardrails.model_copy(
                update={"cluster_correlation_sessions": 4, "cluster_correlation": 0.99}
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class Day:
    day: date
    is_early_close: bool = False


SMALL = _small_risk()
CALENDAR = _days(date(2020, 10, 1), date(2021, 6, 30))
WINDOW = bm.Window(date(2020, 10, 30), date(2021, 6, 30))


def synthetic() -> bm.Inputs:
    rng = random.Random(7)  # noqa: S311 - synthetic prices
    series = {}
    for k in range(10):
        key = f"S{k}"
        price, drift = 20.0 + 5 * k, rng.uniform(-0.002, 0.004)
        closes = []
        for _ in CALENDAR:
            price *= 1 + drift + rng.gauss(0, 0.02)
            closes.append(round(price, 2))
        opens = [round(c * (1 + rng.gauss(0, 0.005)), 2) for c in closes]
        lows = [min(o, c) * 0.99 for o, c in zip(opens, closes, strict=True)]
        highs = [max(o, c) * 1.01 for o, c in zip(opens, closes, strict=True)]
        series[key] = BookSeries(
            key, key, CALENDAR, opens, highs, lows, closes, list(closes), [5e7] * len(closes)
        )
    regimes = {d: Regime.CORRECTION if d.month == 2 else Regime.CONFIRMED_UPTREND for d in CALENDAR}
    spy = {d: 300 * 1.0004**i for i, d in enumerate(CALENDAR)}
    market = BookMarket(
        sessions=[Day(d) for d in CALENDAR],
        series=series,
        members=dict.fromkeys(CALENDAR, frozenset(series)),
        regimes=regimes,
        industries={"S0": "X", "S1": "X", "S2": "X"},
        spy_above_sma=bm.above_sma(CALENDAR, list(spy.values()), 5),
        reactions={"S3": [date(2021, 3, 3)]},
    )
    mtum = {d: 150 * 1.0006**i for i, d in enumerate(CALENDAR)}
    return bm.Inputs(market, spy, mtum)


FORBIDDEN = ("cagr", "equity", "return", "drawdown", "excess", "volatility", "profit", "p&l")


class TestEndToEnd:
    def test_the_scored_run(self) -> None:
        inputs = synthetic()
        s = bm.scored(inputs, SMALL, WINDOW, log=lambda _: None)
        direct = simulate_book(
            inputs.market, SMALL, start=WINDOW.start, end=WINDOW.end, equity=5000
        )
        assert s.primary.run.marks == direct.marks
        assert [j.name for _, j in s.compared] == [f"C{i}" for i in range(1, 8)]
        assert len(s.diagnostics) == 5
        assert s.spy.dates == s.primary.curve.dates
        assert s.signal.months == len([x for x in s.primary.run.rebalances if not x.retry])
        assert any(EntryBlock.CORRECTION in x.blocks for x in s.primary.run.rebalances)
        gate = s.gate_of(s.adopted or s.primary)
        expected = s.adopted if s.adopted is not None and gate.passed else None
        if expected is None:
            expected = s.primary if s.gate_of(s.primary).passed else None
        assert s.frozen is expected
        text = render_scored(s, "2026-10-26 12:00 UTC")
        for heading in ("## The gate", "## Performance", "## The comparisons", "## Reading"):
            assert heading in text
        assert f"`{bm.PREREGISTRATION_COMMIT}`" in text
        assert "Tuning gate: " in text

    def test_a_forced_adoption_and_a_forced_pass_render(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr, "adopted", lambda judged: ["C7"])
        s = bm.scored(synthetic(), SMALL, WINDOW, log=lambda _: None)
        assert s.adopted is not None
        assert s.adopted.config.change == "C7"
        assert s.adopted_names == ["C7"]
        monkeypatch.setattr(mr.Gate, "passed", property(lambda g: True))
        assert s.frozen is s.adopted
        text = render_scored(s, "now")
        assert "Tuning gate: PASS" in text
        assert "the adopted configuration (C7)" in text
        monkeypatch.setattr(mr.Gate, "passed", property(lambda g: g.calmar is None))
        assert s.frozen in (s.primary, None)

    def test_the_blind_check_prints_no_returns(self) -> None:
        b = bm.blind(synthetic(), SMALL, WINDOW, log=lambda _: None)
        assert b.checked == ["primary", *(f"C{i}" for i in range(1, 8))]
        assert b.counts[None].buys > 0
        text = render_blind(b, "now")
        lowered = text.lower()
        for word in FORBIDDEN:
            assert word not in lowered, word
        assert "## Stop conditions" in text

    def test_a_fired_stop_condition_is_reported(self) -> None:
        risk = SMALL.model_copy(
            update={
                "book": SMALL.book.model_copy(
                    update={
                        "backtest": SMALL.book.backtest.model_copy(update={"blind_min_ranked": 50})
                    }
                )
            }
        )
        text = render_blind(bm.blind(synthetic(), risk, WINDOW, log=lambda _: None), "now")
        assert "**Fired:** a ranked set of 8 names < 50" in text

    def test_the_holdout_runs_only_frozen(self) -> None:
        inputs = synthetic()
        with pytest.raises(SystemExit):
            bm.holdout(inputs, SMALL, WINDOW, None)
        h = bm.holdout(inputs, SMALL, WINDOW, SMALL.config_hash)
        text = render_holdout(h, "now")
        assert "opened once" in text
        assert ("PASS" in text) == (h.h1 and h.h2)
