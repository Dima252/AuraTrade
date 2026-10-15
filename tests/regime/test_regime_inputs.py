from dataclasses import replace
from datetime import date

import pytest

from auratrade.regime.breadth import SessionBreadth, breadth_inputs, session_breadth
from auratrade.regime.inputs import (
    VIX,
    VIX3M,
    LoadedData,
    MacroSeries,
    MissingDataError,
    build_inputs,
)
from auratrade.regime.pillars import TrackedBreakout
from tests.regime.regime_helpers import history, small_risk, weekdays

RISK = small_risk()
CFG = RISK.regime
SESSIONS = weekdays(date(2026, 2, 9), 30)  # through Friday 2026-03-20, a quarterly expiration
LAST = SESSIONS[-1]


class TestBreadth:
    def test_universe_filters_and_counts(self) -> None:
        days = SESSIONS[:10]
        rising = [20.0 + k for k in range(10)]
        falling = [40.0 - k for k in range(10)]
        histories = [
            history(days, rising),  # member: above its SMA, a new high
            history(days, falling),  # member: below its SMA, a new low
            history(days, rising, raw_scale=0.3),  # raw close under $10 as traded: out
            history(days[6:], rising[6:]),  # 4 sessions of history: out (needs 5)
            history(days, rising, volume=10.0),  # dollar volume too small: out
        ]
        breadth = session_breadth(histories, days, CFG.breadth, RISK.universe)
        assert breadth[days[-1]] == SessionBreadth(members=2, above=1, new_highs=1, new_lows=1)
        assert breadth[days[-1]].fraction_above == 0.5
        exactly_five = session_breadth(
            [history(days[5:], rising[5:])], days, CFG.breadth, RISK.universe
        )
        assert exactly_five[days[-1]].members == 1
        assert breadth[days[3]].members == 0  # nobody has 5 sessions of history yet
        assert breadth[days[3]].fraction_above is None

    def test_point_in_time_membership(self) -> None:
        # A security with no bar at a session (delisted, or not yet listed) isn't counted.
        days = SESSIONS[:10]
        delisted = history(days[:8], [20.0 + k for k in range(8)])
        breadth = session_breadth([delisted], days, CFG.breadth, RISK.universe)
        assert breadth[days[7]].members == 1
        assert breadth[days[8]].members == 0

    def test_net_new_highs_average(self) -> None:
        days = SESSIONS[:4]
        points = {
            days[0]: SessionBreadth(10, 5, 4, 1),
            days[1]: SessionBreadth(10, 5, 1, 4),
            days[2]: SessionBreadth(10, 5, 6, 0),
            days[3]: SessionBreadth(0, 0, 0, 0),
        }
        out = breadth_inputs(points, days, CFG.breadth)
        assert out[days[1]] == (0.5, None)  # not a full 3-session window yet
        assert out[days[2]] == (0.5, pytest.approx((3 - 3 + 6) / 3))
        assert out[days[3]] == (None, pytest.approx((-3 + 6 + 0) / 3))


def loaded(**changes: object) -> LoadedData:
    closes = [100.0 + k for k in range(len(SESSIONS))]
    base = LoadedData(
        sessions=SESSIONS,
        session_minutes=dict.fromkeys(SESSIONS, 390),
        indexes={"SPY": history(SESSIONS, closes), "QQQ": history(SESSIONS, closes)},
        universe=[history(SESSIONS, [50.0] * len(SESSIONS))],
        macro={
            VIX: MacroSeries([SESSIONS[-3], LAST], [21.0, 18.0]),
            VIX3M: MacroSeries([SESSIONS[-3], LAST], [20.0, 20.0]),
        },
    )
    return replace(base, **changes)  # type: ignore[arg-type]


class TestBuildInputs:
    def test_one_session(self) -> None:
        [x] = build_inputs(
            loaded(), [LAST], CFG, RISK.universe, breakouts=lambda d: [TrackedBreakout(5, True)]
        )
        prev, bar = x.index_bars["SPY"]
        assert (prev.close, bar.close) == (128.0, 129.0)
        trend = x.index_trend["QQQ"]
        assert trend.short_sma == pytest.approx(128.0)  # 3-session SMA of 127, 128, 129
        assert trend.long_sma == pytest.approx(127.0)
        assert trend.short_rising
        assert not trend.short_falling
        assert (x.vix, x.vix3m, x.macro_stale_sessions) == (18.0, 20.0, 0)
        assert x.prior_vix_ratios == (pytest.approx(21 / 20),)  # latest on or before the 19th
        assert x.expiration_session
        assert x.fraction_above == 0.0  # flat at its SMA: not above
        assert x.net_new_highs == 0.0
        assert x.breakouts == [TrackedBreakout(5, True)]

    def test_macro_staleness_counts_sessions(self) -> None:
        macro = {VIX: MacroSeries([SESSIONS[-4]], [18.0]), VIX3M: MacroSeries([LAST], [20.0])}
        [x] = build_inputs(loaded(macro=macro), [LAST], CFG, RISK.universe)
        assert x.macro_stale_sessions == 3  # the older of the two series
        [none] = build_inputs(loaded(macro={}), [LAST], CFG, RISK.universe)
        assert (none.vix, none.macro_stale_sessions) == (None, None)

    def test_half_day_volume_is_normalized(self) -> None:
        minutes = dict.fromkeys(SESSIONS, 390) | {LAST: 210}
        [x] = build_inputs(loaded(session_minutes=minutes), [LAST], CFG, RISK.universe)
        assert x.index_bars["SPY"][1].volume == pytest.approx(1000 * 390 / 210)

    def test_a_gap_in_index_bars_fails_closed(self) -> None:
        closes = [100.0] * len(SESSIONS)
        gappy = history(SESSIONS, closes)
        gappy = type(gappy)(
            dates=[d for d in SESSIONS if d != SESSIONS[10]],
            highs=closes[1:],
            lows=closes[1:],
            closes=closes[1:],
            raw_closes=closes[1:],
            volumes=[1000.0] * (len(SESSIONS) - 1),
        )
        with pytest.raises(MissingDataError, match="SPY bars missing"):
            build_inputs(loaded(indexes={"SPY": gappy, "QQQ": gappy}), [LAST], CFG, RISK.universe)

    def test_a_missing_index_fails_closed(self) -> None:
        only_spy = {"SPY": history(SESSIONS, [100.0] * len(SESSIONS))}
        with pytest.raises(MissingDataError, match="no bars for index QQQ"):
            build_inputs(loaded(indexes=only_spy), [LAST], CFG, RISK.universe)

    def test_an_index_that_ends_early_fails_closed(self) -> None:
        short = history(SESSIONS[:-1], [100.0] * (len(SESSIONS) - 1))
        with pytest.raises(MissingDataError):
            build_inputs(loaded(indexes={"SPY": short, "QQQ": short}), [LAST], CFG, RISK.universe)

    def test_the_first_session_has_no_prior_bar(self) -> None:
        with pytest.raises(MissingDataError, match="no session before"):
            build_inputs(loaded(), [SESSIONS[0]], CFG, RISK.universe)

    def test_an_index_listed_inside_the_range(self) -> None:
        late = history(SESSIONS[20:], [100.0] * 10)
        with pytest.raises(MissingDataError, match="has no bar"):
            build_inputs(
                loaded(indexes={"SPY": late, "QQQ": late}), [SESSIONS[20]], CFG, RISK.universe
            )
        [x] = build_inputs(
            loaded(indexes={"SPY": late, "QQQ": late}), [SESSIONS[21]], CFG, RISK.universe
        )
        assert x.index_trend["SPY"].long_sma is None  # still warming up
