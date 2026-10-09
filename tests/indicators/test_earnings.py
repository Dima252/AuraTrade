import pytest

from auratrade.indicators.bars import Bar
from auratrade.indicators.earnings import (
    EarningsGap,
    EarningsTiming,
    earnings_gap,
    latest_earnings_gap,
    reaction_session,
)

RULES = {"min_gap": 0.05, "volume_window": 3, "min_volume_ratio": 2.0}


def flat(close: float = 50, volume: float = 100) -> Bar:
    return Bar(open=close, high=close, low=close, close=close, volume=volume)


def gap(open_: float, close: float, volume: float) -> Bar:
    return Bar(
        open=open_, high=max(open_, close), low=min(open_, close), close=close, volume=volume
    )


def test_reaction_session() -> None:
    assert reaction_session(10, EarningsTiming.AFTER_CLOSE) == 11
    assert reaction_session(10, EarningsTiming.BEFORE_OPEN) == 10
    assert reaction_session(10, EarningsTiming.DURING_MARKET) == 10


def test_earnings_gap() -> None:
    bars = [flat(), flat(), flat(), gap(55, 54, 300)]
    assert earnings_gap(bars, 3, **RULES) == EarningsGap(
        session=3,
        gap=pytest.approx(0.10),
        day_gain=pytest.approx(0.08),
        volume_ratio=pytest.approx(3.0),
    )


def test_earnings_gap_thresholds() -> None:
    bars = [flat(), flat(), flat(), gap(55, 54, 300)]
    assert earnings_gap(bars, 3, **{**RULES, "min_gap": 0.15}) is None
    assert earnings_gap(bars, 3, **{**RULES, "min_volume_ratio": 4.0}) is None
    assert earnings_gap(bars, 2, **RULES) is None  # not enough prior sessions
    no_volume = [flat(volume=0), flat(volume=0), flat(volume=0), gap(55, 54, 300)]
    assert earnings_gap(no_volume, 3, **RULES) is None
    with pytest.raises(IndexError):
        earnings_gap(bars, 4, **RULES)


def test_latest_earnings_gap_looks_back_from_at_only() -> None:
    bars = [flat()] * 12
    bars[3] = gap(55, 54, 300)
    bars[8] = gap(57, 56, 300)  # prior close 50 (session 7)
    bars[11] = gap(60, 60, 300)  # after `at`: must not be seen
    sessions = [11, 3, 8]
    found = latest_earnings_gap(bars, sessions, 10, lookback=5, **RULES)
    assert found is not None
    assert found.session == 8
    found = latest_earnings_gap(bars, sessions, 7, lookback=5, **RULES)
    assert found is not None
    assert found.session == 3
    assert latest_earnings_gap(bars, sessions, 7, lookback=4, **RULES) is None


def test_latest_earnings_gap_skips_reports_without_a_gap() -> None:
    bars = [flat()] * 10
    bars[4] = gap(56, 55, 300)
    found = latest_earnings_gap(bars, [4, 8], 9, lookback=10, **RULES)  # session 8 didn't gap
    assert found is not None
    assert found.session == 4
    assert latest_earnings_gap(bars, [8], 9, lookback=10, **RULES) is None
    with pytest.raises(IndexError):
        latest_earnings_gap(bars, [4], 10, lookback=10, **RULES)
