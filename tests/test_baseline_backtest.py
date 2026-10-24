"""The 3.8 backtest's historical scan, tracker and 5-pillar regime search: pure pieces on
hand-built data, then the regime step end to end on a synthetic history."""

from __future__ import annotations

from array import array
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from auratrade.calendar.sessions import Session
from auratrade.config import Regime, Strategy
from auratrade.history.read import SecuritySeries
from auratrade.indicators.bases import BaseCountState
from auratrade.strategies.base import Setup
from backtest import baseline
from backtest import history_scan as hs
from backtest import regime as bt
from backtest import regime_search as rs
from tests.ingest.fakes import RISK
from tests.test_scan_real_data import build_synthetic
from tests.test_smoke_real_data import SESSIONS, weekdays


class Stub:
    def facts(self) -> Mapping[str, float]:
        return {}


def series(key: str, dates: list[date], closes: list[float], highs: list[float]) -> SecuritySeries:
    s = SecuritySeries(key, list(dates))
    s.open, s.close, s.raw_close = array("d", closes), array("d", closes), array("d", closes)
    s.high, s.low = array("d", highs), array("d", [c * 0.99 for c in closes])
    s.volume = array("d", [1e6] * len(dates))
    return s


def history(days: list[date], data: dict[str, SecuritySeries]) -> hs.History:
    return hs.History(
        sessions=days,
        minutes=dict.fromkeys(days, 390),
        securities={k: {"symbol": k} for k in data},
        members={d: set(data) for d in days},
        series=data,
        indexes={},
        macro={},
        earnings={},
        quarters={},
        groups={},
        rs={},
        momentum={},
        group_rs={},
        spy={},
    )


def found(
    key: str, index: int, pivot: float, strategy: Strategy = Strategy.VCP_BREAKOUT
) -> hs.Found:
    return hs.Found(key, index, Setup(key, strategy, index, pivot, pivot * 0.9, Stub()))


class TestGroups:
    def test_sic_codes_roll_up_to_groups_of_the_minimum_size(self) -> None:
        securities = {
            "a": {"sic": 3674},
            "b": {"sic": 3674},
            "c": {"sic": 3672},
            "d": {"sic": 2834},
            "e": {"sic": None},
        }
        groups = hs.industry_groups(securities, securities, min_size=2)
        assert groups == {"a": "3674", "b": "3674", "c": "36", "d": "28"}  # alone, 3672 rolls on

    def test_group_ranks_use_each_sessions_ratings(self) -> None:
        days = weekdays(date(2020, 1, 6), 2)
        data = {k: series(k, days, [10.0, 10.0], [10.0, 10.0]) for k in ("a", "b", "c")}
        ratings: dict[str, list[float | None]] = {
            "a": [90.0, 10.0],
            "b": [80.0, 20.0],
            "c": [10.0, None],
        }
        ranks = hs.group_ranks(ratings, data, {"a": "x", "b": "x", "c": "y"})
        assert ranks[days[0]]["x"] > ranks[days[0]]["y"]
        assert ranks[days[1]] == {"x": ranks[days[1]]["x"]}  # y has no rating that session


class TestCountState:
    def test_reliable_only_from_a_listing_inside_the_history(self) -> None:
        first = date(2016, 1, 4)
        assert hs.count_state({"listed_at": date(2018, 5, 1)}, first) == (
            BaseCountState.at_listing()
        )
        assert hs.count_state({"listed_at": first}, first) is None
        assert hs.count_state({"listed_at": None}, first) is None
        assert hs.count_state({"listed_at": date(2018, 5, 1), "relisted": True}, first) is None


class TestTracker:
    def test_triggers_and_results(self) -> None:
        days = weekdays(date(2020, 1, 6), 12)
        rising = series("UP", days, [10.0 + i for i in range(12)], [10.5 + i for i in range(12)])
        flat = series("FL", days, [10.0] * 12, [10.2] * 12)
        gone = series("GO", days[:4], [10.0, 10.0, 11.0, 9.0], [10.0, 10.0, 11.5, 9.5])
        h = history(days, {"UP": rising, "FL": flat, "GO": gone})
        rows = hs.track(
            h,
            [
                found("UP", 0, 11.0),  # day 1's high 11.5 reaches it; day 6 closes 16 > 11
                found("FL", 0, 11.0),  # never reached
                found("GO", 1, 11.2),  # day 2's high 11.5; no bar on day 7: last close 9
                found("UP", 8, 30.0),  # not reached
                found("UP", 10, 20.0),  # day 11 reaches it; its result is past the history
            ],
            RISK,
        )
        assert [(r.security, r.session, r.above_trigger) for r in rows] == [
            ("UP", days[1], True),
            ("GO", days[2], False),
            ("UP", days[11], None),
        ]

    def test_the_pillar_reads_only_completed_rows_of_its_window(self) -> None:
        days = weekdays(date(2020, 1, 6), 40)
        rows = [hs.TrackerRow("a", Strategy.VCP_BREAKOUT, d, True) for d in days[:30]]
        rows.append(hs.TrackerRow("a", Strategy.VCP_BREAKOUT, days[38], None))
        per = hs.breakouts_by_session(rows, days, RISK)
        at = per[days[39]]  # the window is days 15-34 ago 0..24; results 5+ sessions ago count
        assert {b.sessions_ago for b in at} == set(range(10, 25)) | {1}
        assert all(b.above_trigger is not None for b in at if b.sessions_ago >= 5)


class TestVariants:
    def test_sixteen_distinct_variants(self) -> None:
        assert len({v.name for v in rs.VARIANTS}) == 16

    def test_a_variant_moves_its_thresholds_together(self) -> None:
        cfg = rs.Variant(3, -1, 2, 6).config(RISK.regime)
        t = cfg.transitions
        assert (t.confirmed_min_score, t.debounce_score, t.no_ftd_min_score) == (3, 2, 3)
        assert (t.correction_max_score, t.correction_sessions) == (-1, 2)
        assert cfg.distribution.down_min_days == 6
        assert t.upgrade_sessions == RISK.regime.transitions.upgrade_sessions

    def test_ibd_distance(self) -> None:
        assert rs.Variant(3, -1, 1, 6).ibd_distance == 0
        assert rs.Variant(3, -1, 2, 6).ibd_distance == 0
        assert rs.Variant(2, -2, 1, 8).ibd_distance == 3


@dataclass(frozen=True)
class Judged:
    passed: int
    mean_switches: float

    def checks(self) -> dict[str, bool]:
        return {f"c{i}": i < self.passed for i in range(5)}


def summary(checks: int, switches: float) -> Judged:
    return Judged(checks, switches)


def test_choose_by_targets_then_switches_then_ibd() -> None:
    a, b, c, d = (
        rs.Variant(2, -2, 1, 8),
        rs.Variant(3, -1, 2, 6),
        rs.Variant(3, -1, 1, 6),
        rs.Variant(2, -1, 1, 6),
    )
    assert rs.choose({a: summary(4, 10), b: summary(3, 5)}) == a
    assert rs.choose({a: summary(3, 10), b: summary(3, 5)}) == b
    assert rs.choose({a: summary(3, 5), d: summary(3, 5)}) == d
    assert rs.choose({b: summary(3, 5), c: summary(3, 5)}) == c


def test_regime_step_on_a_synthetic_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, risk = build_synthetic(tmp_path)
    monkeypatch.setattr(bt, "DECLINES", (bt.Decline("drop", SESSIONS[300], SESSIONS[340]),))
    monkeypatch.setattr(bt, "UPTRENDS", (bt.Window("rise", SESSIONS[350], SESSIONS[500]),))
    monkeypatch.setattr(bt, "RECOVERY_LOW", SESSIONS[340])
    days = [_session(d) for d in SESSIONS]
    h = hs.load_history(store, days, risk, last=SESSIONS[-1])
    assert h.sessions == SESSIONS
    assert "GAPR" in {h.symbol(k) for k in h.scanned}

    converged, summaries, chosen, rows = baseline.run_regime(h, risk, log=lambda _: None)
    assert set(converged) == set(rs.VARIANTS)
    assert chosen in rs.VARIANTS
    for c in converged.values():
        assert c.iterations >= 1
        assert {r.session for r in c.results} == set(SESSIONS[1:])
    assert all(r.session > SESSIONS[0] for r in rows)
    report = baseline.render_regime(h, risk, converged, summaries, chosen, 1.0)
    assert f"**Chosen: `{chosen.name}`**" in report
    assert "## The tracker" in report
    assert Regime.CORRECTION.value in report


def _session(day: date) -> Session:
    at = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)
    return Session(day, at, at + timedelta(minutes=390), False, 390)
