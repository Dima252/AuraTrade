from datetime import date, timedelta

import pytest

from auratrade.config import DEFAULT_RISK_CONFIG, Regime, load_risk_config
from auratrade.regime.ladder import LADDER, downgrade, most_conservative, rank
from auratrade.regime.pillars import (
    IndexTrend,
    TrackedBreakout,
    breadth_pillar,
    crisis_override,
    distribution_pillar,
    index_trend_pillar,
    leadership_pillar,
    quarterly_expiration_sessions,
    volatility_pillar,
)

CFG = load_risk_config(DEFAULT_RISK_CONFIG).regime
UP_TREND = IndexTrend(
    close=110, short_sma=105, long_sma=100, short_rising=True, short_falling=False
)
FLAT = IndexTrend(close=110, short_sma=105, long_sma=100, short_rising=False, short_falling=False)
DOWN = IndexTrend(close=95, short_sma=98, long_sma=100, short_rising=False, short_falling=True)


class TestIndexTrend:
    def test_both_indexes_needed_for_plus_one(self) -> None:
        assert index_trend_pillar({"SPY": UP_TREND, "QQQ": UP_TREND}) == 1
        assert index_trend_pillar({"SPY": UP_TREND, "QQQ": FLAT}) == 0

    def test_either_index_gives_minus_one(self) -> None:
        assert index_trend_pillar({"SPY": UP_TREND, "QQQ": DOWN}) == -1

    def test_below_the_200_with_a_rising_50_is_not_minus_one(self) -> None:
        below = IndexTrend(95, 98, 100, short_rising=True, short_falling=False)
        assert index_trend_pillar({"SPY": below, "QQQ": below}) == 0

    def test_the_stack_order_matters(self) -> None:
        inverted = IndexTrend(110, 100, 105, short_rising=True, short_falling=False)
        assert index_trend_pillar({"SPY": inverted, "QQQ": UP_TREND}) == 0

    def test_missing_averages_score_zero(self) -> None:
        warming = IndexTrend(110, None, None, short_rising=False, short_falling=False)
        assert index_trend_pillar({"SPY": warming, "QQQ": UP_TREND}) == 0
        assert index_trend_pillar({}) == 0


class TestBreadth:
    @pytest.mark.parametrize(
        ("above", "net", "score"),
        [
            (0.61, 5.0, 1),
            (0.60, 5.0, 0),  # "> 60%"
            (0.61, 0.0, 0),  # net new highs must be > 0
            (0.50, 2.0, 0),
            (0.34, 2.0, -1),  # < 35% above
            (0.35, 2.0, 0),
            (0.70, -0.1, -1),  # new lows outnumber new highs
            (None, 2.0, 0),
            (0.70, None, 0),
        ],
    )
    def test_thresholds(self, above: float | None, net: float | None, score: int) -> None:
        assert breadth_pillar(above, net, CFG.breadth) == score


class TestVolatility:
    @pytest.mark.parametrize(
        ("vix", "vix3m", "score"),
        [
            (15, 18, 1),  # 15 < 20 and 0.83 < 0.9
            (19.9, 21, 0),  # ratio 0.95
            (20, 30, 0),  # VIX not < 20
            (25, 30, 0),
            (31, 35, -1),  # VIX > 30
            (22, 21, -1),  # ratio > 1.0
            (20, 20, 0),  # ratio exactly 1.0
        ],
    )
    def test_thresholds(self, vix: float, vix3m: float, score: int) -> None:
        assert volatility_pillar(vix, vix3m, stale=False, cfg=CFG.volatility) == score

    def test_stale_or_missing_scores_zero(self) -> None:
        assert volatility_pillar(40, 30, stale=True, cfg=CFG.volatility) == 0
        assert volatility_pillar(None, 30, stale=False, cfg=CFG.volatility) == 0


@pytest.mark.parametrize(("days", "score"), [(0, 1), (3, 1), (4, 0), (5, 0), (6, -1), (9, -1)])
def test_distribution_pillar(days: int, score: int) -> None:
    assert distribution_pillar(days, CFG.distribution) == score


def tracked(results: list[bool], sessions_ago: int = 5) -> list[TrackedBreakout]:
    return [TrackedBreakout(sessions_ago, r) for r in results]


class TestLeadership:
    def test_fewer_than_ten_breakouts_score_zero(self) -> None:
        result = leadership_pillar(tracked([True] * 9), CFG.leadership)
        assert (result.score, result.breakouts, result.share_above) == (0, 9, None)

    @pytest.mark.parametrize(
        ("held", "score"), [(11, 1), (10, 0), (8, 0), (7, -1), (0, -1), (20, 1)]
    )
    def test_shares(self, held: int, score: int) -> None:
        # 20 breakouts: 11 held = 55% -> +1; 7 held = 35% -> -1.
        breakouts = tracked([True] * held + [False] * (20 - held))
        assert leadership_pillar(breakouts, CFG.leadership).score == score

    def test_only_the_window_t_minus_24_to_t_minus_5_counts(self) -> None:
        # Recent breakouts (T-4..T) have no complete result and are ignored, as are older ones.
        breakouts = [
            *tracked([True] * 10, sessions_ago=24),
            *tracked([False] * 30, sessions_ago=25),
            *[TrackedBreakout(k, None) for k in range(5)],
            *[TrackedBreakout(k, False) for k in range(5)],
        ]
        result = leadership_pillar(breakouts, CFG.leadership)
        assert (result.score, result.breakouts) == (1, 10)

    def test_an_incomplete_result_inside_the_window_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="no complete result"):
            leadership_pillar([TrackedBreakout(7, None)], CFG.leadership)


class TestCrisis:
    def test_term_inversion_for_two_sessions(self) -> None:
        one = crisis_override([0.95, 1.05], macro_stale=False, index_return=0.0, cfg=CFG.crisis)
        two = crisis_override([1.02, 1.05], macro_stale=False, index_return=0.0, cfg=CFG.crisis)
        assert not one.active
        assert two.active
        assert two.reasons == ("vix_term_inversion",)

    def test_too_little_history_or_missing_values(self) -> None:
        assert not crisis_override(
            [1.1], macro_stale=False, index_return=0.0, cfg=CFG.crisis
        ).active
        assert not crisis_override(
            [None, 1.1], macro_stale=False, index_return=0.0, cfg=CFG.crisis
        ).active

    def test_an_index_drop_of_four_percent(self) -> None:
        drop = crisis_override([0.9, 0.9], macro_stale=False, index_return=-0.04, cfg=CFG.crisis)
        assert drop.reasons == ("index_drop",)
        assert not crisis_override(
            [0.9, 0.9], macro_stale=False, index_return=-0.0399, cfg=CFG.crisis
        ).active

    def test_stale_macro_data_fails_closed(self) -> None:
        stale = crisis_override([], macro_stale=True, index_return=None, cfg=CFG.crisis)
        assert stale.reasons == ("macro_stale",)
        both = crisis_override([1.2, 1.2], macro_stale=True, index_return=-0.05, cfg=CFG.crisis)
        assert both.reasons == ("macro_stale", "index_drop")


class TestExpirations:
    def test_third_fridays_of_the_quarter_months(self) -> None:
        start = date(2026, 1, 1)
        weekdays = [
            start + timedelta(days=k)
            for k in range(365)
            if (start + timedelta(days=k)).weekday() < 5
        ]
        assert quarterly_expiration_sessions(weekdays) == {
            date(2026, 3, 20),
            date(2026, 6, 19),
            date(2026, 9, 18),
            date(2026, 12, 18),
        }

    def test_a_holiday_moves_it_to_the_prior_session(self) -> None:
        # 2008-03-21 was Good Friday: the March expiration was on Thursday the 20th.
        sessions = [date(2008, 3, d) for d in (17, 18, 19, 20, 24, 25)]
        assert quarterly_expiration_sessions(sessions) == {date(2008, 3, 20)}

    def test_no_sessions(self) -> None:
        assert quarterly_expiration_sessions([]) == set()


class TestLadder:
    def test_order(self) -> None:
        assert [rank(r) for r in LADDER] == [0, 1, 2, 3]
        assert LADDER[0] is Regime.CORRECTION
        assert LADDER[-1] is Regime.CONFIRMED_UPTREND

    def test_most_conservative(self) -> None:
        assert (
            most_conservative(Regime.CONFIRMED_UPTREND, Regime.EARLY_UPTREND)
            is Regime.EARLY_UPTREND
        )
        assert most_conservative(Regime.UNDER_PRESSURE) is Regime.UNDER_PRESSURE
        with pytest.raises(ValueError, match="at least one"):
            most_conservative()

    @pytest.mark.parametrize(
        ("current", "proposed", "effective"),
        [
            (Regime.CONFIRMED_UPTREND, None, Regime.CONFIRMED_UPTREND),
            (Regime.CONFIRMED_UPTREND, Regime.UNDER_PRESSURE, Regime.UNDER_PRESSURE),
            (Regime.UNDER_PRESSURE, Regime.CONFIRMED_UPTREND, Regime.UNDER_PRESSURE),  # never up
            (Regime.EARLY_UPTREND, Regime.EARLY_UPTREND, Regime.EARLY_UPTREND),
            (Regime.EARLY_UPTREND, Regime.CORRECTION, Regime.CORRECTION),
            (Regime.EARLY_UPTREND, Regime.CONFIRMED_UPTREND, Regime.EARLY_UPTREND),
            # Only the state machine assigns EARLY_UPTREND: anywhere else it fails closed.
            (Regime.CONFIRMED_UPTREND, Regime.EARLY_UPTREND, Regime.CORRECTION),
            (Regime.CORRECTION, Regime.EARLY_UPTREND, Regime.CORRECTION),
        ],
    )
    def test_downgrade(self, current: Regime, proposed: Regime | None, effective: Regime) -> None:
        assert downgrade(current, proposed) is effective
