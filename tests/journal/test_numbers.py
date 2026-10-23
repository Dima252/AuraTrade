"""Your numbers (journal.numbers)."""

import pytest

from auratrade.config import Strategy
from auratrade.guardrails.levels import StrategyRecord
from auratrade.journal.numbers import ClosedTrade, numbers, strategy_record

VCP, EP = Strategy.VCP_BREAKOUT, Strategy.EPISODIC_PIVOT


def t(gain: float, r: float = 0.0, strategy: Strategy = VCP) -> ClosedTrade:
    return ClosedTrade(strategy, gain, r)


def test_numbers() -> None:
    n = numbers([t(0.20, 2.5), t(-0.05, -1.0), t(0.10, 1.25), t(0.0, 0.0)])
    assert n.trades == 4
    assert n.win_rate == 0.5
    assert n.average_gain == pytest.approx(0.15)
    assert n.average_loss == pytest.approx(0.05)
    assert n.expectancy == pytest.approx(0.0625)
    assert n.expectancy_r == pytest.approx(0.6875)


def test_window_takes_the_latest_trades() -> None:
    n = numbers([t(0.30), t(-0.10), t(0.10)], window=2)
    assert (n.trades, n.average_gain, n.average_loss) == (2, 0.10, 0.10)


def test_no_trades() -> None:
    n = numbers([])
    assert (n.trades, n.win_rate, n.average_gain, n.expectancy) == (0, None, None, None)
    with pytest.raises(ValueError, match="window"):
        numbers([], window=0)


def test_strategy_record() -> None:
    trades = [t(0.5, strategy=EP)] + [t(0.10)] * 5 + [t(-0.05)] * 20
    assert strategy_record(trades, VCP, window=20) == StrategyRecord(25, None)
    assert strategy_record(trades, VCP, window=25) == StrategyRecord(25, pytest.approx(0.10))
    assert strategy_record(trades, EP, window=20) == StrategyRecord(1, 0.5)
