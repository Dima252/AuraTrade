"""The close-entry backtest (#144): the gate's pieces on hand-built trades with known answers,
then the run and its report end to end on the synthetic history, blind and scored."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path

import pytest

from auratrade.config import Strategy
from auratrade.execution.sizing import Tranches
from auratrade.execution.triggers import OrderKind
from auratrade.guardrails.orders import OrderPlan
from auratrade.indicators.earnings import EarningsTiming
from auratrade.plans.entry import GateResult
from auratrade.plans.model import Rule
from auratrade.sim.minutes import MinuteCache
from auratrade.sim.trade import FillKind, TradeFill, TradeOutcome, TradeResult
from auratrade.storage import LocalStore
from backtest import close_entry as ce
from backtest import pool as bp
from backtest.baseline import Loaded, pool_inputs
from backtest.close_report import render_close
from backtest.history_scan import load_history
from backtest.regime_search import Variant
from tests.ingest.fakes import RISK
from tests.test_baseline_backtest import _session, found, history, series
from tests.test_baseline_pool import DailyMinutes
from tests.test_scan_real_data import build_synthetic
from tests.test_smoke_real_data import SESSIONS, weekdays

CLOSE = RISK.with_candidate(ce.CANDIDATE)


def trade(day: date, r: float, strategy: Strategy = Strategy.VCP_BREAKOUT) -> bp.PoolTrade:
    result = TradeResult(TradeOutcome.CLOSED, risk=100.0, pnl=100.0 * r)
    listing = bp.Listing(found("X", 0, 10.0, strategy), day, 1, 0.5, 4)
    return bp.PoolTrade(listing, day, bp.Regime.CONFIRMED_UPTREND, bp.Status.CLOSED, result=result)


def by_year(values: Mapping[int, list[float]]) -> list[bp.PoolTrade]:
    return [
        trade(date(y, 1, 2) + timedelta(days=i), r)
        for y, rs in values.items()
        for i, r in enumerate(rs)
    ]


def one_block(_: date) -> int:
    return 0


class TestVerdict:
    def test_positive_in_four_years_and_without_the_best(self) -> None:
        v = ce.verdict(
            by_year({2017: [1.0], 2018: [0.5], 2019: [0.5], 2020: [2.0], 2021: [-0.5]}),
            one_block,
        )
        assert v.years == {2017: 1.0, 2018: 0.5, 2019: 0.5, 2020: 2.0, 2021: -0.5, 2022: None}
        assert v.positive_years == 4
        assert v.without_best == pytest.approx((1.0 + 0.5 + 0.5 - 0.5) / 4)
        assert v.robust

    def test_three_positive_years_is_not_robust(self) -> None:
        v = ce.verdict(by_year({2017: [1.0], 2018: [1.0], 2019: [1.0], 2020: [-1.0]}), one_block)
        assert v.positive_years == 3
        assert not v.robust

    def test_one_big_year_carrying_the_rest_is_not_robust(self) -> None:
        values = {2017: [0.1], 2018: [0.1], 2019: [0.1], 2020: [5.0], 2021: [-1.0], 2022: [-1.0]}
        v = ce.verdict(by_year(values), one_block)
        assert v.positive_years == 4
        assert v.without_best == pytest.approx(-0.34)
        assert not v.robust

    def test_passing_needs_every_condition(self) -> None:
        # 300 trades, all positive, in every year: the interval is above zero.
        values = {y: [1.0] * 50 for y in ce.TUNING_YEARS}
        v = ce.verdict(by_year(values), one_block)
        assert v.enough
        assert v.expectancy.passes
        assert v.passes(decision_check_ok=True)
        assert not v.passes(decision_check_ok=False)
        few = ce.verdict(by_year({y: [1.0] * 10 for y in ce.TUNING_YEARS}), one_block)
        assert not few.enough
        assert not few.passes(decision_check_ok=True)

    def test_passes_on_one_year(self) -> None:
        values = {2017: [0.01] * 20, 2020: [3.0] * 300, 2021: [-0.01] * 20}
        v = ce.verdict(by_year(values), lambda d: d.toordinal())
        assert v.expectancy.passes
        assert v.one_year
        assert not v.passes(decision_check_ok=True)

    def test_no_closed_trades(self) -> None:
        v = ce.verdict([], one_block)
        assert (v.expectancy.trades, v.without_best, v.positive_years) == (0, None, 0)


def test_blocks_count_21_sessions_from_the_first_session_of_2017() -> None:
    days = weekdays(date(2016, 12, 26), 60)
    h = history(days, {"X": series("X", days, [10.0] * 60, [10.5] * 60)})
    p = bp.PoolInputs(h, {}, [_session(d) for d in days], RISK)
    block_of = ce.blocks(p)
    first = next(d for d in days if d.year == 2017)
    k = days.index(first)
    assert block_of(first) == 0
    assert block_of(days[k + 20]) == 0
    assert block_of(days[k + 21]) == 1
    assert block_of(days[k - 1]) == -1


def filled(fills: tuple[TradeFill, ...], *, stop: float = 9.0, limit: float = 10.5) -> bp.PoolTrade:
    order = OrderPlan(OrderKind.LIMIT, limit, limit, stop, Tranches(10, 0))
    result = TradeResult(
        TradeOutcome.CLOSED,
        order=order,
        fills=fills,
        risk=10.0,
        pnl=5.0,
        gate=GateResult(()),
    )
    listing = bp.Listing(found("X", 0, 10.0), date(2020, 3, 2), 1, 0.5, 4)
    return bp.PoolTrade(
        listing, date(2020, 3, 3), bp.Regime.CONFIRMED_UPTREND, bp.Status.CLOSED, result=result
    )


def test_invariants() -> None:
    good = filled(
        (TradeFill(1, FillKind.ENTRY, 10, 10.2), TradeFill(4, FillKind.EXIT, 10, 11.0, Rule.TRAIL))
    )
    assert ce.invariant_violations([good]) == []
    above = filled((TradeFill(1, FillKind.ENTRY, 10, 10.6),))
    after = filled(
        (
            TradeFill(1, FillKind.ENTRY, 10, 10.2),
            TradeFill(2, FillKind.EXIT, 10, 9.0, Rule.HARD_STOP),
            TradeFill(3, FillKind.SALE, 1, 9.5, Rule.TRAIL),
        )
    )
    none = filled(())
    problems = ce.invariant_violations([above, after, none])
    assert len(problems) == 3
    assert "between the stop and the limit" in problems[0]
    assert "after the exit" in problems[1]
    assert "no entry" in problems[2]


def test_the_comparisons_change_one_dimension() -> None:
    off = ce.volume_off(CLOSE)
    assert all(g.min_volume_ratio is None for g in off.plans.confirmation_gates.values())
    structural = ce.structural_stop(CLOSE)
    for s in ce.CANDIDATE_2:
        assert structural.plans.entry[s].stop.value == "STRUCTURAL"
    assert structural.plans.entry[Strategy.EPISODIC_PIVOT].stop.value == "TRIGGER_DAY_LOW"
    assert all(
        g.max_above_level is None for g in ce.no_max_chase(CLOSE).plans.confirmation_gates.values()
    )
    assert all(t.fraction == 0 for t in ce.no_slippage(CLOSE).sim.slippage)


@pytest.mark.parametrize("blind", [True, False])
def test_the_run_and_its_report_on_a_synthetic_history(tmp_path: Path, blind: bool) -> None:
    store, risk = build_synthetic(tmp_path)
    days = [_session(d) for d in SESSIONS]
    hist = load_history(store, days, risk, last=SESSIONS[-1])
    reports = [(d, EarningsTiming.AFTER_CLOSE) for d in SESSIONS[20::63]]
    hist.earnings = dict.fromkeys(hist.series, reports)
    loaded = Loaded(hist, days, store, None)  # type: ignore[arg-type]
    close = risk.with_candidate(ce.CANDIDATE)
    p, candidates = pool_inputs(loaded, close, Variant(2, -2, 2, 8), log=lambda _: None)
    lists = bp.capped_lists(p.h, candidates.found, candidates.regimes, close)
    raw = {
        (hist.symbol(k), d): (s.open[i], s.high[i], s.low[i], s.raw_close[i])
        for k, s in hist.series.items()
        for i, d in enumerate(s.dates)
    }
    cache = MinuteCache(LocalStore(tmp_path / "cache"), DailyMinutes(raw))

    def symbols_for(wanted: Mapping[date, set[str]]) -> Mapping[tuple[date, str], str]:
        return bp.traded_symbols(store, wanted)

    def minutes_for(wanted: Mapping[date, set[str]]) -> Mapping[date, Mapping[str, bp.Minutes]]:
        return bp.prefetch(p, cache, {d: s for d, s in wanted.items() if s})

    run = ce.run_close(p, lists, minutes_for, symbols_for, blind=blind)
    assert run.reference
    assert ce.invariant_violations(run.reference) == []
    assert run.decision.sampled == len(ce.gated(run.reference))
    assert run.decision.compared + run.decision.no_minutes == run.decision.sampled
    text = render_close(run, close, "cu+2_corr-2_c2_dist8", 1.0)
    assert "## The attempts" in text
    assert "## The decision-price checks" in text
    assert ce.PREREGISTRATION_COMMIT in text
    scored = ("## The gate (section 8.1)", "## Per year", "## Comparisons", "## Diagnostics")
    if blind:
        assert (run.compared, run.no_chase, run.no_cost) == ([], [], [])
        assert "Blind (step 3)" in text
        assert not any(section in text for section in scored)
    else:
        assert [v.name for v, _, _ in run.compared] == ["volume_off", "structural_stop"]
        assert all(section in text for section in scored)
