import pytest

from auratrade.config import Strategy
from auratrade.indicators.bars import Bar
from auratrade.strategies.momentum_breakout import MomentumBreakout, MomentumDetail
from tests.strategies.scan_data import (
    FAILING,
    FLAG_FIRST,
    FLAG_SECOND,
    FLAT,
    MOVE,
    RISK,
    bar,
    data,
    momentum_bars,
    with_strategy,
)

T = 61
PEAK = 49


def strategy_with(**changes: object) -> MomentumBreakout:
    return MomentumBreakout(with_strategy(RISK, "MOMENTUM_BREAKOUT", **changes))


def test_flag_after_a_big_move_is_found() -> None:
    setup = MomentumBreakout(RISK).scan(data(momentum_bars()))
    assert setup is not None
    assert setup.strategy is Strategy.MOMENTUM_BREAKOUT
    assert setup.session == T
    assert setup.pivot == 80  # the consolidation high
    assert setup.structural_stop == 75  # the second half's low
    detail = setup.detail
    assert isinstance(detail, MomentumDetail)
    assert (detail.peak_session, detail.consolidation_sessions) == (PEAK, 12)
    assert detail.move == pytest.approx(80 / 49.5 - 1)
    assert detail.move_low == 49.5
    assert detail.depth == pytest.approx(1 - 72 / 80)
    assert detail.second_half_range < detail.first_half_range
    assert detail.momentum_rank == 99


def test_rank_below_the_top_two_percent_rejects() -> None:
    assert MomentumBreakout(RISK).scan(data(momentum_bars(), momentum=97.9)) is None
    assert MomentumBreakout(RISK).scan(data(momentum_bars(), momentum=None)) is None


def test_trend_template_failure_rejects() -> None:
    assert MomentumBreakout(RISK).scan(data(momentum_bars(), template=FAILING)) is None


def test_consolidation_too_short_rejects() -> None:
    bars = momentum_bars()
    assert MomentumBreakout(RISK).scan(data(bars), at=PEAK + 9) is None


def test_consolidation_too_long_rejects() -> None:
    assert strategy_with(max_consolidation_sessions=11).scan(data(momentum_bars())) is None


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"min_move": 0.7}, "the move (+61.6%) is too small"),
        ({"min_rank": 99.5}, "the rank (99) is below the bar"),
        ({"min_consolidation_sessions": 13}, "12 sessions is too short"),
    ],
)
def test_each_threshold_rejects(changes: dict[str, object], why: str) -> None:
    assert strategy_with(**changes).scan(data(momentum_bars())) is None, why


def test_lower_lows_reject() -> None:
    second = [*FLAG_SECOND[:-1], bar(78, 71.5)]  # the last session undercuts the first half's 72
    assert MomentumBreakout(RISK).scan(data(momentum_bars(second=second))) is None


def test_a_widening_range_rejects() -> None:
    # ~7.5% ranges against the first half's ~5.4%, lows still higher (from 74).
    wide = [
        bar(h, lo) for h, lo in [(79.8, 74), (79.5, 74.2), (79.9, 74.5), (79.6, 74), (80, 74.5)]
    ]
    second = [*wide, bar(79.5, 74)]
    assert MomentumBreakout(RISK).scan(data(momentum_bars(second=second))) is None


def test_a_close_below_the_averages_rejects() -> None:
    # A last session closing at 73.2 is below the 10-day and the 20-day.
    second = [*FLAG_SECOND[:-1], Bar(open=76, high=76.5, low=76, close=73.2, volume=1000)]
    assert MomentumBreakout(RISK).scan(data(momentum_bars(second=second))) is None


def test_an_average_that_is_not_rising_does_not_count() -> None:
    # The close (77.5) is above the 10-day (75.8), but the 10-day was 75.9 five sessions ago;
    # only the 20-day is rising.
    assert strategy_with(ride_smas=(10,)).scan(data(momentum_bars())) is None
    assert strategy_with(ride_smas=(20,)).scan(data(momentum_bars())) is not None


def test_a_higher_high_before_the_peak_means_no_fresh_move() -> None:
    # An earlier spike to 85 inside the move window: the 80 high doesn't top the move.
    flat = [*FLAT[:20], bar(85, 50), *FLAT[21:]]
    bars = [*flat, *MOVE, *FLAG_FIRST, *FLAG_SECOND]
    assert MomentumBreakout(RISK).scan(data(bars)) is None


def test_a_peak_on_the_first_bar_has_no_move_before_it() -> None:
    bars = [bar(80, 78), *FLAG_FIRST, *FLAG_SECOND]
    strategy = strategy_with(ride_smas=(2,), ma_rising_sessions=1)
    assert strategy.scan(data(bars)) is None


def test_too_little_history_for_the_rising_check_rejects() -> None:
    strategy = strategy_with(ma_rising_sessions=70)
    assert strategy.scan(data(momentum_bars())) is None


def test_scanning_an_earlier_session_ignores_later_bars() -> None:
    bars = momentum_bars()
    full = data(bars)
    strategy = MomentumBreakout(RISK)
    for at in range(30, len(bars)):
        assert strategy.scan(full, at) == strategy.scan(data(bars[: at + 1])), at


def test_llm_context() -> None:
    strategy = MomentumBreakout(RISK)
    setup = strategy.scan(data(momentum_bars()))
    assert setup is not None
    context = strategy.build_llm_context(setup)
    assert context["consolidation_sessions"] == 12
    assert context["prior_move"] == round(80 / 49.5 - 1, 4)
    assert context["distance_to_pivot"] == round(80 / 77.5 - 1, 4)
    assert context["structural_stop"] == 75
