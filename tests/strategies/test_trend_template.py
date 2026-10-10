import pytest

from auratrade.config import RsWeight, Strategy, TrendTemplateConfig, load_risk_config
from auratrade.indicators.bars import Bar
from auratrade.strategies.trend_template import (
    TrendTemplate,
    passes_for,
    rs_ratings,
    rs_scores,
    trend_template,
)

ALL = frozenset(range(1, 9))

# Short windows so every case can be checked by hand: 50/150/200 SMA -> 2/3/4 sessions, "rising
# for 22 sessions" -> above its value 2 sessions ago, the 52-week range -> 6 sessions.
SMALL = TrendTemplateConfig(
    short_sma=2,
    mid_sma=3,
    long_sma=4,
    long_sma_rising_sessions=2,
    range_sessions=6,
    min_above_low=0.30,
    max_below_high=0.25,
    min_rs_rating=70,
    rs_weights=(RsWeight(lookback=1, weight=1.0),),
    required_rules={
        Strategy.VCP_BREAKOUT: ALL,
        Strategy.MOMENTUM_BREAKOUT: ALL,
        Strategy.LEADER_PULLBACK: ALL,
        Strategy.EMERGING_LEADER: frozenset({1, 5, 6, 7, 8}),
        Strategy.EPISODIC_PIVOT: frozenset(),
    },
)

# At the last session: close 17 > 50 SMA 16.5 > 150 SMA 16 > 200 SMA 15.5; the 200 SMA was 13.5
# two sessions earlier; 52-week low 12 (17 >= 15.6), high 17.
UPTREND = [10.0, 11, 12, 13, 14, 15, 16, 17]


def flat_bars(closes: list[float]) -> list[Bar]:
    return [Bar(open=c, high=c, low=c, close=c, volume=1000) for c in closes]


def last(
    bars: list[Bar], rating: float | None = 80, config: TrendTemplateConfig = SMALL
) -> TrendTemplate:
    return trend_template(bars, [rating] * len(bars), config)[-1]


def test_uptrend_passes_all_eight() -> None:
    template = last(flat_bars(UPTREND))
    assert template.passes()
    assert template.failed == ()


# One pass and one fail case per rule. Each fail case is built so that only that rule fails.


def test_rule_1_price_above_150_and_200_sma() -> None:
    assert last(flat_bars(UPTREND)).rule(1)
    # 200 SMA = (14 + 15 + 16 + 13) / 4 = 14.5 > close 13.
    assert not last(flat_bars([10, 11, 12, 13, 14, 15, 16, 13])).rule(1)


def test_rule_2_150_sma_above_200_sma() -> None:
    assert last(flat_bars(UPTREND)).rule(2)
    # 150 SMA = 16.67 < 200 SMA = 17.5, with the close (30) above both.
    template = last(flat_bars([10, 11, 12, 13, 20, 10, 10, 30]))
    assert template.failed == (2,)


def test_rule_3_200_sma_rising() -> None:
    assert last(flat_bars(UPTREND)).rule(3)
    # 200 SMA = 16.5, below its 16.75 two sessions earlier; everything else is in order.
    template = last(flat_bars([10, 10, 10, 19, 19, 14, 15, 18, 19]))
    assert template.failed == (3,)


def test_rule_4_50_sma_above_150_and_200_sma() -> None:
    assert last(flat_bars(UPTREND)).rule(4)
    # 50 SMA = 13 < 150 SMA = 15.33 (200 SMA = 15), close 16.
    template = last(flat_bars([10, 11, 12, 13, 14, 20, 10, 16]))
    assert template.failed == (4,)


def test_rule_5_price_above_50_sma() -> None:
    assert last(flat_bars(UPTREND)).rule(5)
    # 50 SMA = 18 > close 16, which is still above the 150 SMA (14.67) and 200 SMA (13.5).
    template = last(flat_bars([10, 10, 10, 10, 10, 8, 20, 16]))
    assert template.failed == (5,)


def test_rule_6_price_30_percent_above_52_week_low() -> None:
    assert last(flat_bars(UPTREND)).rule(6)
    # 52-week low 14: 14 * 1.3 = 18.2 > close 17.
    template = last(flat_bars([10, 11, 14, 14, 15, 15.5, 16, 17]))
    assert template.failed == (6,)


def test_rules_6_and_7_count_a_close_exactly_at_the_level() -> None:
    # In floating point 1.01 * 1.3 = 1.3130000000000002 and 1.1 * 0.75 = 0.8250000000000001,
    # yet closes of 1.313 and 0.825 are exactly 30% above the low / 25% below the high.
    assert last(flat_bars([1.01, 1.01, 1.01, 1.1, 1.2, 1.25, 1.28, 1.313])).rule(6)
    bars = flat_bars([0.7, 0.72, 0.74, 0.76, 0.78, 0.8, 0.81, 0.825])
    bars[5] = Bar(open=0.8, high=1.1, low=0.8, close=0.8, volume=1000)
    assert last(bars).rule(7)


def test_rule_7_price_within_25_percent_of_52_week_high() -> None:
    bars = flat_bars(UPTREND)
    bars[5] = Bar(open=15, high=22, low=15, close=15, volume=1000)  # 22 * 0.75 = 16.5 <= 17
    assert last(bars).rule(7)
    bars[5] = Bar(open=15, high=23, low=15, close=15, volume=1000)  # 23 * 0.75 = 17.25 > 17
    assert last(bars).failed == (7,)


def test_rule_8_rs_rating_at_least_70() -> None:
    bars = flat_bars(UPTREND)
    assert last(bars, rating=70).rule(8)
    assert last(bars, rating=69.9).failed == (8,)
    assert last(bars, rating=None).failed == (8,)  # not ranked


def test_every_session_is_computed_and_warm_up_fails_closed() -> None:
    bars = flat_bars(UPTREND)
    templates = trend_template(bars, [80] * len(bars), SMALL)
    assert len(templates) == len(bars)
    # Only the RS rating is known on the first session; the 50 SMA (rule 5) follows a session
    # later and the 200 SMA (rules 1-4) at session 3.
    assert templates[0].failed == (1, 2, 3, 4, 5, 6, 7)
    assert templates[1].failed == templates[2].failed == (1, 2, 3, 4, 6, 7)
    # Rule 3 compares the 200 SMA with 2 sessions earlier, and the 6-session range starts at 5.
    assert templates[4].failed == (3, 6, 7)
    assert templates[5].passes()


def test_empty_history() -> None:
    assert trend_template([], [], SMALL) == []


def test_ratings_must_align_with_bars() -> None:
    with pytest.raises(ValueError, match="lengths differ"):
        trend_template(flat_bars(UPTREND), [80], SMALL)


def test_rule_numbers_are_checked() -> None:
    template = last(flat_bars(UPTREND))
    for bad in (0, 9):
        with pytest.raises(ValueError, match="1-8"):
            template.rule(bad)


def test_emerging_leaders_skip_the_ma_stack_and_episodic_pivots_are_exempt() -> None:
    # Fails only rule 4 (the MA stack).
    template = last(flat_bars([10, 11, 12, 13, 14, 20, 10, 16]))
    assert passes_for(template, Strategy.EMERGING_LEADER, SMALL)
    assert not passes_for(template, Strategy.VCP_BREAKOUT, SMALL)
    assert not passes_for(template, Strategy.MOMENTUM_BREAKOUT, SMALL)
    assert not passes_for(template, Strategy.LEADER_PULLBACK, SMALL)
    # A downtrend fails everything, but an EP's catalyst replaces the trend.
    downtrend = last(flat_bars(UPTREND[::-1]), rating=10)
    assert downtrend.failed == (1, 2, 3, 4, 5, 6, 7, 8)
    assert passes_for(downtrend, Strategy.EPISODIC_PIVOT, SMALL)
    assert not passes_for(downtrend, Strategy.EMERGING_LEADER, SMALL)


def test_rs_ratings_rank_the_universe_on_the_weighted_score() -> None:
    weights = (RsWeight(lookback=1, weight=1.0), RsWeight(lookback=2, weight=0.5))
    config = SMALL.model_copy(update={"rs_weights": weights})
    # Scores: A = 1.0 * 10% + 0.5 * 10% = 0.15; B = 0; C = 1.0 * -10% + 0.5 * -14.5% = -0.1725.
    ratings = rs_ratings(
        {"A": [100, 100, 110], "B": [100, 100, 100], "C": [100, 95, 85.5], "NEW": [100]},
        config,
    )
    assert ratings == {"A": 100.0, "B": 50.0, "C": 0.0}  # NEW lacks history: unranked
    assert rs_scores([100, 100, 110], config) == [None, None, pytest.approx(0.15)]
    assert rs_ratings({"X": []}, config) == {}


def test_repo_config_on_a_long_uptrend() -> None:
    config = load_risk_config().trend_template
    closes = [100 * 1.002**t for t in range(300)]
    templates = trend_template(flat_bars(closes), [90] * len(closes), config)
    # The 200 SMA starts at session 199; rule 3 compares it with 22 sessions earlier.
    assert not templates[220].rule(3)
    assert templates[221].rule(3)
    assert templates[251].passes()
    assert not templates[250].rule(6)  # the 52-week range needs 252 sessions
    assert templates[-1].passes()
