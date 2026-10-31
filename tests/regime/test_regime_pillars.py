from datetime import date, timedelta

import pytest

from auratrade.config import DEFAULT_RISK_CONFIG, load_risk_config
from auratrade.regime.pillars import (
    IndexTrend,
    crisis_override,
    distribution_pillar,
    index_trend_pillar,
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


@pytest.mark.parametrize(
    ("days", "score"), [(0, 1), (3, 1), (4, 0), (6, 0), (7, 0), (8, -1), (9, -1)]
)
def test_distribution_pillar(days: int, score: int) -> None:
    assert distribution_pillar(days, CFG.distribution) == score


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
