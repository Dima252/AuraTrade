import pytest
from pydantic import ValidationError

from auratrade.config import LeaderPullbackConfig, Strategy
from auratrade.indicators.moving_averages import ema
from auratrade.indicators.volatility import atr
from auratrade.strategies.leader_pullback import LeaderPullback, PullbackDetail
from tests.strategies.scan_data import (
    ADVANCE,
    FAILING,
    PULLBACK,
    RISK,
    data,
    line,
    with_strategy,
)

T = 62


def strategy_with(**changes: object) -> LeaderPullback:
    return LeaderPullback(with_strategy(RISK, "LEADER_PULLBACK", **changes))


def test_pullback_to_the_10_day_is_found() -> None:
    bars = [*ADVANCE, *PULLBACK]
    setup = LeaderPullback(RISK).scan(data(bars))
    assert setup is not None
    assert setup.strategy is Strategy.LEADER_PULLBACK
    assert setup.session == T
    assert setup.pivot == bars[T].high  # tomorrow's "prior session's high"
    ema10 = ema([b.close for b in bars], 10)[T]
    assert ema10 is not None
    assert setup.structural_stop == min(76.9, ema10)
    detail = setup.detail
    assert isinstance(detail, PullbackDetail)
    assert detail.tested_ema == 10
    assert (detail.swing_session, detail.swing_high, detail.pullback_sessions) == (59, 80, 3)
    assert detail.volume_ratio == pytest.approx(600 / ((47 * 1000 + 3 * 600) / 50))
    assert detail.depth == pytest.approx(1 - 76.9 / 80)


def test_a_deeper_pullback_tests_the_21_day() -> None:
    bars = [*ADVANCE, *line([78.5, 76.5, 74.8], volume=600)]
    closes = [b.close for b in bars]
    ema10, ema21, a = ema(closes, 10)[T], ema(closes, 21)[T], atr(bars, 14)[T]
    assert ema10 is not None
    assert ema21 is not None
    assert a is not None
    low = bars[T].low
    assert abs(low - ema10) > a >= abs(low - ema21)  # the case this test is for
    setup = LeaderPullback(RISK).scan(data(bars))
    assert setup is not None
    assert isinstance(setup.detail, PullbackDetail)
    assert setup.detail.tested_ema == 21


def test_a_pullback_through_both_averages_rejects() -> None:
    bars = [*ADVANCE, *line([77.0, 73.0, 69.0], volume=600)]
    assert LeaderPullback(RISK).scan(data(bars)) is None


def test_a_shallow_dip_is_not_a_pullback() -> None:
    # 80 -> 76.9 is 3.1, about 3 ATRs; a 3.5-ATR minimum rejects it.
    bars = [*ADVANCE, *PULLBACK]
    assert strategy_with(min_depth_atr=2.9).scan(data(bars)) is not None
    assert strategy_with(min_depth_atr=3.5).scan(data(bars)) is None


def test_heavy_volume_rejects() -> None:
    bars = [*ADVANCE, *line([79.0, 78.2, 77.4], volume=1500)]
    assert LeaderPullback(RISK).scan(data(bars)) is None


def test_rs_below_80_rejects() -> None:
    assert LeaderPullback(RISK).scan(data([*ADVANCE, *PULLBACK], rs=79)) is None
    assert LeaderPullback(RISK).scan(data([*ADVANCE, *PULLBACK], rs=None)) is None


def test_trend_template_failure_rejects() -> None:
    assert LeaderPullback(RISK).scan(data([*ADVANCE, *PULLBACK], template=FAILING)) is None


def test_a_stock_at_its_high_is_not_pulling_back() -> None:
    assert LeaderPullback(RISK).scan(data(ADVANCE)) is None
    assert LeaderPullback(RISK).scan(data([*ADVANCE, *PULLBACK]), at=T - 2) is None  # 1 session


def test_tolerance_and_warm_up_reject() -> None:
    bars = [*ADVANCE, *PULLBACK]
    assert strategy_with(max_atr_from_ema=0.01).scan(data(bars)) is None
    assert strategy_with(atr_sessions=70).scan(data(bars)) is None  # no ATR yet
    assert strategy_with(volume_sessions=70).scan(data(bars)) is None  # no volume average yet


def test_scanning_an_earlier_session_ignores_later_bars() -> None:
    bars = [*ADVANCE, *PULLBACK, *line([78.0, 79.0, 80.5])]
    full = data(bars)
    strategy = LeaderPullback(RISK)
    for at in range(30, len(bars)):
        assert strategy.scan(full, at) == strategy.scan(data(bars[: at + 1])), at


def test_llm_context() -> None:
    strategy = LeaderPullback(RISK)
    setup = strategy.scan(data([*ADVANCE, *PULLBACK]))
    assert setup is not None
    context = strategy.build_llm_context(setup)
    assert context["tested_ema"] == 10
    assert context["pullback_sessions"] == 3
    assert context["rs_rating"] == 90


@pytest.mark.parametrize(
    ("changes", "message"),
    [({"min_pullback_sessions": 11}, "swing_sessions"), ({"emas": (21, 10)}, "shortest first")],
)
def test_settings_are_validated(changes: dict[str, object], message: str) -> None:
    raw = {**RISK.strategies.LEADER_PULLBACK.model_dump(), **changes}
    with pytest.raises(ValidationError, match=message):
        LeaderPullbackConfig.model_validate(raw)
