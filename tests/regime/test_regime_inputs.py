from dataclasses import replace
from datetime import date

import pytest

from auratrade.regime.inputs import (
    VIX,
    VIX3M,
    LoadedData,
    MacroSeries,
    MissingDataError,
    build_inputs,
)
from tests.regime.regime_helpers import history, small_risk, weekdays

RISK = small_risk()
CFG = RISK.regime
SESSIONS = weekdays(date(2026, 2, 9), 30)  # through Friday 2026-03-20, a quarterly expiration
LAST = SESSIONS[-1]


def loaded(**changes: object) -> LoadedData:
    closes = [100.0 + k for k in range(len(SESSIONS))]
    base = LoadedData(
        sessions=SESSIONS,
        session_minutes=dict.fromkeys(SESSIONS, 390),
        indexes={"SPY": history(SESSIONS, closes), "QQQ": history(SESSIONS, closes)},
        macro={
            VIX: MacroSeries([SESSIONS[-3], LAST], [21.0, 18.0]),
            VIX3M: MacroSeries([SESSIONS[-3], LAST], [20.0, 20.0]),
        },
    )
    return replace(base, **changes)  # type: ignore[arg-type]


class TestBuildInputs:
    def test_one_session(self) -> None:
        [x] = build_inputs(loaded(), [LAST], CFG)
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
        # Breadth and leadership have no inputs since task 5.2: they score 0.
        assert (x.fraction_above, x.net_new_highs, x.breakouts) == (None, None, ())

    def test_macro_staleness_counts_sessions(self) -> None:
        macro = {VIX: MacroSeries([SESSIONS[-4]], [18.0]), VIX3M: MacroSeries([LAST], [20.0])}
        [x] = build_inputs(loaded(macro=macro), [LAST], CFG)
        assert x.macro_stale_sessions == 3  # the older of the two series
        [none] = build_inputs(loaded(macro={}), [LAST], CFG)
        assert (none.vix, none.macro_stale_sessions) == (None, None)

    def test_half_day_volume_is_normalized(self) -> None:
        minutes = dict.fromkeys(SESSIONS, 390) | {LAST: 210}
        [x] = build_inputs(loaded(session_minutes=minutes), [LAST], CFG)
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
            build_inputs(loaded(indexes={"SPY": gappy, "QQQ": gappy}), [LAST], CFG)

    def test_a_missing_index_fails_closed(self) -> None:
        only_spy = {"SPY": history(SESSIONS, [100.0] * len(SESSIONS))}
        with pytest.raises(MissingDataError, match="no bars for index QQQ"):
            build_inputs(loaded(indexes=only_spy), [LAST], CFG)

    def test_an_index_that_ends_early_fails_closed(self) -> None:
        short = history(SESSIONS[:-1], [100.0] * (len(SESSIONS) - 1))
        with pytest.raises(MissingDataError):
            build_inputs(loaded(indexes={"SPY": short, "QQQ": short}), [LAST], CFG)

    def test_the_first_session_has_no_prior_bar(self) -> None:
        with pytest.raises(MissingDataError, match="no session before"):
            build_inputs(loaded(), [SESSIONS[0]], CFG)

    def test_an_index_listed_inside_the_range(self) -> None:
        late = history(SESSIONS[20:], [100.0] * 10)
        with pytest.raises(MissingDataError, match="has no bar"):
            build_inputs(loaded(indexes={"SPY": late, "QQQ": late}), [SESSIONS[20]], CFG)
        [x] = build_inputs(loaded(indexes={"SPY": late, "QQQ": late}), [SESSIONS[21]], CFG)
        assert x.index_trend["SPY"].long_sma is None  # still warming up
