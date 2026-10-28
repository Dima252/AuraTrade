"""The fill model's prices: slippage against the trader, before the tick."""

import pytest

from auratrade.sim.fills import buy_price, sell_price


def test_slippage_is_against_the_trader() -> None:
    assert buy_price(100.0, 0.002) == pytest.approx(100.2)
    assert sell_price(100.0, 0.002) == pytest.approx(99.8)
    assert buy_price(100.0, 0.0) == sell_price(100.0, 0.0) == 100.0
