"""The regime backtest's measures on hand-built timelines with known answers, and one run end to
end on a synthetic history through the live classifier."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from auratrade.config import Regime
from auratrade.regime.classifier import Pillars, RegimeResult
from auratrade.regime.inputs import build_inputs
from auratrade.regime.pillars import Crisis, PillarScore
from auratrade.regime.state import RegimeState
from backtest import regime as bt
from tests.regime.regime_helpers import small_risk, weekdays

CU, UP, EU, CORR = (
    Regime.CONFIRMED_UPTREND,
    Regime.UNDER_PRESSURE,
    Regime.EARLY_UPTREND,
    Regime.CORRECTION,
)


def result(
    day: date, regime: Regime, score: int = 0, *, no_ftd: bool = False, crisis: bool = False
) -> RegimeResult:
    trend: PillarScore = 1 if score > 0 else -1 if score < 0 else 0
    return RegimeResult(
        session=day,
        previous=None,
        state=RegimeState(regime=regime, seq=0, entered_seq=0, no_ftd=no_ftd),
        pillars=Pillars(trend, 0, 0, 0, 0),
        crisis=Crisis(crisis, ("index_drop",) if crisis else ()),
        follow_through=None,
        distribution_counts={},
        features={},
    )


def timeline(days: list[date], regimes: str) -> list[RegimeResult]:
    """One result per day from a string of regime initials: C, E, U, K (confirmed)."""
    code = {"C": CORR, "E": EU, "U": UP, "K": CU}
    return [result(d, code[c]) for d, c in zip(days, regimes, strict=True)]


DAYS = weekdays(date(2018, 1, 1), 40)


class TestSpells:
    def test_runs_of_the_same_regime_with_what_came_next(self) -> None:
        got = bt.spells(timeline(DAYS[:6], "CCEEUK"))
        assert [(s.regime, s.first, s.last, s.sessions, s.next) for s in got] == [
            (CORR, DAYS[0], DAYS[1], 2, EU),
            (EU, DAYS[2], DAYS[3], 2, UP),
            (UP, DAYS[4], DAYS[4], 1, CU),
            (CU, DAYS[5], DAYS[5], 1, None),
        ]

    def test_an_empty_timeline_has_none(self) -> None:
        assert bt.spells([]) == []

    def test_the_no_ftd_flag_comes_from_the_spell_s_first_label(self) -> None:
        results = [result(DAYS[0], CORR), result(DAYS[1], EU, no_ftd=True)]
        assert [s.no_ftd for s in bt.spells(results)] == [False, True]


class TestCounts:
    def test_switches_per_year_count_changes_in_the_year_they_happen(self) -> None:
        days = [date(2018, 12, 27), date(2018, 12, 28), date(2019, 1, 2), date(2019, 1, 3)]
        assert bt.switches_per_year(timeline(days, "CEEU")) == {2018: 1, 2019: 1}

    def test_per_year_and_reported(self) -> None:
        days = [date(2016, 12, 30), date(2017, 1, 3), date(2017, 1, 4)]
        results = timeline(days, "CKK")
        assert [r.session for r in bt.reported(results)] == days[1:]
        assert bt.per_year(results)[2017][CU] == 2

    def test_pillar_and_score_counts(self) -> None:
        results = [result(DAYS[0], CU, 1), result(DAYS[1], CU, 1), result(DAYS[2], CORR, -1)]
        assert bt.pillar_counts(results)["index_trend"] == {1: 2, -1: 1}
        assert bt.pillar_counts(results)["leadership"] == {0: 3}
        assert bt.score_counts(results) == {1: 2, -1: 1}


def spy(closes: list[float], days: list[date] | None = None) -> bt.Spy:
    return bt.Spy(days or DAYS[: len(closes)], closes)


class TestSpy:
    def test_prior_high_includes_the_day_itself(self) -> None:
        s = spy([100, 110, 105])
        assert s.prior_high(DAYS[2]) == 110
        assert s.prior_high(DAYS[1]) == 110

    def test_prior_high_looks_back_252_sessions(self) -> None:
        days = weekdays(date(2016, 1, 4), 300)
        closes = [200.0] + [100.0] * 299
        s = bt.Spy(days, closes)
        assert s.prior_high(days[251]) == 200
        assert s.prior_high(days[252]) == 100

    def test_an_unknown_day_raises(self) -> None:
        with pytest.raises(KeyError):
            spy([100]).close(date(2030, 1, 1))


class TestForward:
    def test_returns_and_worst_close_by_regime(self) -> None:
        s = spy([100, 90, 120, 130])
        results = timeline(DAYS[:4], "KKCC")
        got = bt.forward_stats(results, s, 2)
        cu = got[CU]
        assert cu is not None
        # From 100: +20% two sessions later, worst 90 (-10%); from 90: +44.4%, worst 0.
        assert cu.n == 2
        assert cu.mean == pytest.approx((0.2 + 130 / 90 - 1) / 2)
        assert cu.positive == 1.0
        assert cu.mean_drawdown == pytest.approx(-0.05)
        # The CORRECTION labels have no complete 2-session horizon.
        assert got[CORR] is None
        assert got[EU] is None


class TestFalseCorrections:
    def test_a_long_correction_near_the_high_is_false(self) -> None:
        days = weekdays(date(2017, 3, 1), 20)
        closes = [100.0] + [95.0] * 19
        results = timeline(days, "K" + "C" * 19)
        [(spell, worst)] = bt.false_corrections(results, bt.Spy(days, closes))
        assert (spell.first, spell.sessions) == (days[1], 19)
        assert worst == pytest.approx(-0.05)

    def test_an_eight_percent_close_below_the_high_makes_it_real(self) -> None:
        days = weekdays(date(2017, 3, 1), 20)
        closes = [100.0] + [95.0] * 18 + [92.0]
        results = timeline(days, "K" + "C" * 19)
        assert bt.false_corrections(results, bt.Spy(days, closes)) == []

    def test_fifteen_sessions_is_not_long_enough(self) -> None:
        days = weekdays(date(2017, 3, 1), 16)
        results = timeline(days, "K" + "C" * 15)
        assert bt.false_corrections(results, bt.Spy(days, [100.0] * 16)) == []


class TestWindows:
    def test_shares_and_scores_inside_a_window(self) -> None:
        results = timeline(DAYS[:4], "CKKU")
        window = bt.Window("w", DAYS[1], DAYS[3])
        shares = bt.window_shares(results, window)
        assert shares[CU] == pytest.approx(2 / 3)
        assert shares[CORR] == 0
        assert bt.window_scores(results, window) == {0: 3}

    def test_an_empty_window_has_zero_shares(self) -> None:
        window = bt.Window("w", date(2030, 1, 1), date(2030, 2, 1))
        assert set(bt.window_shares(timeline(DAYS[:1], "K"), window).values()) == {0.0}


class TestDecline:
    def test_correction_reached_early_and_late(self) -> None:
        # Peak 100 on day 0, low 60 on day 5 (-40%). CORRECTION on day 2 at 90 (-10%): early.
        s = spy([100, 95, 90, 80, 70, 60])
        decline = bt.Decline("d", DAYS[0], DAYS[5])
        check = bt.decline_check(timeline(DAYS[:6], "KUCCCC"), s, decline)
        assert (check.defensive, check.correction) == (DAYS[1], DAYS[2])
        assert check.full_drawdown == pytest.approx(-0.4)
        assert check.correction_drawdown == pytest.approx(-0.1)
        assert check.early
        late = bt.decline_check(timeline(DAYS[:6], "KKKKCC"), s, decline)
        assert late.correction_drawdown == pytest.approx(-0.3)
        assert not late.early

    def test_never_defensive(self) -> None:
        s = spy([100, 90])
        check = bt.decline_check(timeline(DAYS[:2], "KK"), s, bt.Decline("d", DAYS[0], DAYS[1]))
        assert (check.defensive, check.correction, check.early) == (None, None, False)


class TestRecovery:
    def test_sessions_and_gains_from_the_low(self) -> None:
        s = spy([100, 110, 120, 130])
        got = bt.recovery(timeline(DAYS[:4], "CCEK"), s, DAYS[0])
        assert (got.early, got.confirmed) == (2, 3)
        assert got.early_gain == pytest.approx(0.2)
        assert got.confirmed_gain == pytest.approx(0.3)

    def test_the_pre_registered_limits(self) -> None:
        assert bt.Recovery(15, 0.1, 60, 0.2).ok
        assert not bt.Recovery(16, 0.1, 20, 0.2).ok
        assert not bt.Recovery(5, 0.1, 61, 0.2).ok
        assert not bt.Recovery(None, None, None, None).ok

    def test_no_recovery(self) -> None:
        got = bt.recovery(timeline(DAYS[:3], "CCC"), spy([100, 90, 80]), DAYS[0])
        assert got == bt.Recovery(None, None, None, None)


class TestEarlyUptrends:
    def test_spy_over_the_sessions_a_spell_governed(self) -> None:
        # EU labelled on days 1-2 governs days 2-3: from the close of day 1 to day 3.
        s = spy([100, 100, 104, 98, 97])
        [early] = bt.early_uptrends(timeline(DAYS[:5], "CEECC"), s)
        assert early.spell.next is CORR
        assert early.spy_return == pytest.approx(-0.02)

    def test_a_spell_at_the_end_stops_at_the_last_close(self) -> None:
        [early] = bt.early_uptrends(timeline(DAYS[:3], "CEE"), spy([100, 100, 110]))
        assert early.spy_return == pytest.approx(0.1)
        assert early.spell.next is None


class TestSummary:
    def test_the_checks_against_the_pre_registered_targets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        days = weekdays(date(2017, 1, 2), 40)
        monkeypatch.setattr(bt, "DECLINES", ())
        monkeypatch.setattr(bt, "UPTRENDS", (bt.Window("w", days[0], days[-1]),))
        monkeypatch.setattr(bt, "RECOVERY_LOW", days[0])
        results = timeline(days, "C" * 20 + "E" * 4 + "C" * 16)
        s = bt.summarize("x", "d", results, bt.Spy(days, [100.0] * 40))
        assert s.shares[CORR] == pytest.approx(36 / 40)
        assert s.max_switches == 2
        assert s.mean_switches == 2
        assert [e.spell.sessions for e in s.failed_early] == [4]
        checks = s.checks()
        assert checks["switches <= 12/yr"]
        # Two CORRECTION spells over 15 sessions with SPY flat: 2 false spells in 1 year.
        assert len(s.false) == 2
        assert not checks["false CORR <= 1/yr"]
        assert not checks["CU share >= target"]
        assert not checks["2020 recovery"]

    def test_no_switches_on_an_empty_timeline(self) -> None:
        s = bt.Summary("x", "d", {}, {}, [], {}, [], bt.Recovery(None, None, None, None), [], 0)
        assert (s.mean_switches, s.max_switches) == (0.0, 0)


def test_variants_change_only_what_they_name() -> None:
    cfg = small_risk().regime
    base = bt.VARIANTS["baseline"][1](cfg)
    assert base == cfg
    two = bt.VARIANTS["correction_2"][1](cfg)
    assert two.transitions.correction_sessions == 2
    assert two.model_copy(update={"transitions": cfg.transitions}) == cfg
    excluded = bt.VARIANTS["exclude_expirations"][1](cfg)
    assert excluded.distribution.exclude_expiration_sessions
    assert bt.VARIANTS["no_eu_minimum"][1](cfg).transitions.early_uptrend_min_sessions == 1
    assert bt.VARIANTS["no_ftd_20"][1](cfg).transitions.no_ftd_sessions == 20


def test_reading_is_kept_across_runs(tmp_path: Path) -> None:
    report = tmp_path / "regime.md"
    assert bt.kept_reading(report) == ""
    report.write_text("# Regime\n\nnumbers\n\n## Reading\n\nby hand\n", encoding="utf-8")
    assert bt.kept_reading(report) == "## Reading\n\nby hand\n"
    report.write_text("# Regime\n", encoding="utf-8")
    assert bt.kept_reading(report) == ""


def test_end_to_end_on_a_synthetic_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every variant through the live classifier, then the report and the chart."""
    from auratrade.regime.inputs import VIX, VIX3M, LoadedData, MacroSeries
    from tests.regime.regime_helpers import history

    risk = small_risk()
    days = weekdays(date(2016, 12, 1), 90)
    closes = [100.0 + k for k in range(40)] + [140.0 - 2 * k for k in range(20)]
    closes += [100.0 + 1.5 * k for k in range(30)]
    data = LoadedData(
        sessions=days,
        session_minutes=dict.fromkeys(days, 390),
        indexes={"SPY": history(days, closes, spread=0.01), "QQQ": history(days, closes)},
        universe=[history(days, closes, volume=1e6), history(days, [50.0] * 90, volume=1e6)],
        macro={VIX: MacroSeries(days, [15.0] * 90), VIX3M: MacroSeries(days, [18.0] * 90)},
    )
    s = bt.Spy(days, closes)
    monkeypatch.setattr(bt, "DECLINES", (bt.Decline("drop", days[39], days[59]),))
    monkeypatch.setattr(bt, "UPTRENDS", (bt.Window("rise", days[60], days[89]),))
    monkeypatch.setattr(bt, "RECOVERY_LOW", days[59])
    inputs = build_inputs(data, days[1:], risk.regime, risk.universe)
    summaries, baseline = [], []
    for name, (description, change) in bt.VARIANTS.items():
        results = bt.run(inputs, change(risk.regime), risk.data.macro_stale_sessions)
        assert len(results) == 89
        summaries.append(bt.summarize(name, description, results, s))
        baseline = baseline or results
    assert baseline[0].regime is CORR  # the machine starts in CORRECTION
    loaded = bt.Loaded(data, s, members=2, delisted_members=0)
    report = bt.render(summaries, baseline, loaded, risk, 1.0, "## Reading\n\nkept\n")
    assert report.startswith("# Regime backtest (task 2.4)")
    assert "| `correction_2` |" in report
    assert report.rstrip().endswith("kept")
    assert "(Not written yet.)" in bt.render(summaries, baseline, loaded, risk, 1.0)
    svg = bt.chart(baseline, s)
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert "2017" in svg


def test_detail_csv(tmp_path: Path) -> None:
    bt.write_detail("x", timeline(DAYS[:2], "CK"), tmp_path)
    rows = (tmp_path / "x.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("session,regime,score,index_trend")
    assert rows[2].startswith(f"{DAYS[1]},CONFIRMED_UPTREND,0")
