import pytest

from auratrade.config import Strategy
from auratrade.indicators.bars import Bar
from auratrade.strategies.episodic_pivot import EpisodicPivot, EpisodicPivotDetail
from tests.strategies.scan_data import FAILING, GAP, QUIET, RISK, data, with_strategy

T = 60


def test_gap_on_heavy_volume_is_found() -> None:
    setup = EpisodicPivot(RISK).scan(data([*QUIET, GAP]))
    assert setup is not None
    assert setup.strategy is Strategy.EPISODIC_PIVOT
    assert setup.session == T
    assert setup.pivot == 22.5  # the gap day's open, standing in for the opening-range high
    assert setup.structural_stop == 20  # the prior close: a filled gap has failed
    detail = setup.detail
    assert isinstance(detail, EpisodicPivotDetail)
    assert detail.gap == pytest.approx(0.125)
    assert detail.volume_ratio == pytest.approx(4.0)
    assert detail.day_gain == pytest.approx(0.175)
    assert detail.earnings is False
    assert detail.prior_moves == {63: None, 126: None}  # not enough history before the gap
    assert detail.pre_gap_adr == pytest.approx(20.2 / 19.8 - 1)


def test_the_trend_template_does_not_apply() -> None:
    assert EpisodicPivot(RISK).scan(data([*QUIET, GAP], template=FAILING)) is not None


def test_an_earnings_reaction_session_is_flagged() -> None:
    setup = EpisodicPivot(RISK).scan(data([*QUIET, GAP], earnings=frozenset({T})))
    assert setup is not None
    assert isinstance(setup.detail, EpisodicPivotDetail)
    assert setup.detail.earnings is True


def test_prior_moves_are_reported_not_filtered() -> None:
    rally = [Bar(open=c, high=c, low=c, close=c, volume=1000) for c in range(10, 140)]
    quiet = [Bar(open=20, high=20.2, low=19.8, close=20, volume=1000) for _ in range(70)]
    bars = [*rally, *quiet, GAP]
    setup = EpisodicPivot(RISK).scan(data(bars))
    assert setup is not None
    assert isinstance(setup.detail, EpisodicPivotDetail)
    t = len(bars) - 1
    assert setup.detail.prior_moves[63] == pytest.approx(0.0)
    assert setup.detail.prior_moves[126] == pytest.approx(20 / bars[t - 1 - 126].close - 1)


@pytest.mark.parametrize(
    "gap_bar",
    [
        Bar(open=21.8, high=24, low=21.5, close=23.5, volume=4000),  # a 9% gap
        Bar(open=22.5, high=24, low=22, close=23.5, volume=2900),  # 2.9x volume
    ],
)
def test_small_gap_or_light_volume_rejects(gap_bar: Bar) -> None:
    assert EpisodicPivot(RISK).scan(data([*QUIET, gap_bar])) is None


def test_too_little_history_for_the_volume_average_rejects() -> None:
    risk = with_strategy(RISK, "EPISODIC_PIVOT", volume_sessions=61)
    assert EpisodicPivot(risk).scan(data([*QUIET, GAP])) is None


def test_llm_context() -> None:
    strategy = EpisodicPivot(RISK)
    setup = strategy.scan(data([*QUIET, GAP]))
    assert setup is not None
    context = strategy.build_llm_context(setup)
    assert context["gap"] == 0.125
    assert context["volume_ratio"] == 4.0
    assert context["earnings_gap"] is False
    assert context["prior_move_63"] is None


def test_no_prior_volume_rejects() -> None:
    silent = [Bar(open=20, high=20.2, low=19.8, close=20, volume=0) for _ in range(60)]
    assert EpisodicPivot(RISK).scan(data([*silent, GAP])) is None
    assert EpisodicPivot(RISK).scan(data([GAP])) is None
