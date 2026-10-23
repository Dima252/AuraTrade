"""The fill model on hand-built bars with known fills (README §5.12)."""

from datetime import timedelta

import pytest

from auratrade.config import SimConfig, load_risk_config
from auratrade.indicators.bars import Bar
from auratrade.sim.fills import (
    between,
    entry_fill,
    entry_on_bar,
    last_price,
    sell_limit_on_bar,
    slippage,
    stop_fill,
    stop_on_bar,
)
from tests.sim.helpers import at, bars, flat

SIM = load_risk_config().sim


def b(o: float, h: float, low: float, c: float) -> Bar:
    return Bar(o, h, low, c, 1000)


class TestSlippage:
    @pytest.mark.parametrize(
        ("dollar_volume", "fraction"),
        [(2e9, 0.0005), (5e8, 0.0005), (1e8, 0.001), (3e7, 0.002), (1.2e7, 0.003), (None, 0.003)],
    )
    def test_tiers_by_dollar_volume(self, dollar_volume: float | None, fraction: float) -> None:
        assert slippage(dollar_volume, SIM) == fraction

    def test_tiers_must_run_from_the_highest_down_to_zero(self) -> None:
        raw = SIM.model_dump()
        with pytest.raises(ValueError, match="highest dollar volume down"):
            SimConfig.model_validate({**raw, "slippage": list(reversed(raw["slippage"]))})
        with pytest.raises(ValueError, match="start at 0"):
            SimConfig.model_validate({**raw, "slippage": raw["slippage"][:-1]})
        with pytest.raises(ValueError, match="delayed_entry_minutes"):
            SimConfig.model_validate({**raw, "delayed_entry_minutes": raw["entry_minutes"]})


class TestBuyStopLimit:
    def test_not_triggered_below_the_trigger(self) -> None:
        step = entry_on_bar(
            b(49, 49.9, 48.8, 49.5), trigger=50, limit=51, slip=0.002, resting=False
        )
        assert (step.price, step.resting) == (None, False)

    def test_fills_at_the_trigger_plus_slippage(self) -> None:
        step = entry_on_bar(
            b(49.8, 50.4, 49.7, 50.3), trigger=50, limit=51, slip=0.002, resting=False
        )
        assert step.price == 50.10

    def test_a_gap_over_the_trigger_fills_at_the_open(self) -> None:
        step = entry_on_bar(
            b(50.5, 50.9, 50.4, 50.8), trigger=50, limit=51, slip=0.002, resting=False
        )
        assert step.price == 50.61  # 50.5 x 1.002 = 50.601, rounded up

    def test_a_jump_past_the_limit_rests_as_a_limit(self) -> None:
        step = entry_on_bar(
            b(51.5, 52, 51.2, 51.8), trigger=50, limit=51, slip=0.002, resting=False
        )
        assert (step.price, step.resting) == (None, True)

    def test_slippage_that_takes_the_fill_past_the_limit_rests_too(self) -> None:
        step = entry_on_bar(b(50, 50.2, 50, 50.1), trigger=50, limit=50, slip=0.002, resting=False)
        assert (step.price, step.resting) == (None, True)

    def test_a_breakout_that_never_comes_back_is_a_miss(self) -> None:
        day = [b(51.5, 52, 51.2, 51.8), b(51.8, 53, 51.6, 52.9), b(52.9, 54, 52.5, 53.5)]
        assert entry_fill(day, trigger=50, limit=51, slip=0.002) is None

    def test_one_that_comes_back_fills_at_the_limit_on_a_later_bar(self) -> None:
        day = [b(51.5, 52, 51.2, 51.8), b(51.8, 51.9, 50.7, 50.9), b(50.9, 51, 50.5, 50.6)]
        fill = entry_fill(day, trigger=50, limit=51, slip=0.002)
        assert fill is not None
        assert (fill.price, fill.index) == (51, 1)

    def test_the_triggering_bar_itself_never_fills_at_the_limit(self) -> None:
        # It opened past the limit and traded back under it within the minute: unknown order,
        # so only a later bar can fill it.
        assert entry_fill([b(51.5, 52, 50.5, 50.8)], trigger=50, limit=51, slip=0.002) is None

    def test_an_untriggered_day_is_not_filled(self) -> None:
        day = [b(49, 49.5, 48.5, 49.2), b(49.2, 49.9, 49, 49.8)]
        assert entry_fill(day, trigger=50, limit=51, slip=0.002) is None


class TestLimitBuy:
    def test_marketable_on_the_first_bar(self) -> None:
        fill = entry_fill([b(50.5, 50.8, 50.3, 50.6)], trigger=None, limit=51, slip=0.002)
        assert fill is not None
        assert fill.price == 50.61

    def test_rests_when_the_first_bar_opens_past_the_limit(self) -> None:
        day = [b(51.2, 51.5, 51.1, 51.3), b(51.3, 51.4, 50.9, 51)]
        fill = entry_fill(day, trigger=None, limit=51, slip=0.002)
        assert fill is not None
        assert (fill.price, fill.index) == (51, 1)


class TestStops:
    def test_not_reached(self) -> None:
        assert stop_on_bar(b(48, 48.5, 47.01, 47.5), stop=47, slip=0.002) is None

    def test_fills_at_the_stop_less_slippage(self) -> None:
        assert stop_on_bar(b(48, 48.5, 46.5, 46.8), stop=47, slip=0.002) == 46.90

    def test_a_gap_through_fills_at_the_open_less_slippage(self) -> None:
        assert stop_on_bar(b(45, 45.5, 44, 44.5), stop=47, slip=0.002) == 44.91

    def test_the_first_bar_to_reach_it(self) -> None:
        fill = stop_fill([b(48, 48, 47.5, 47.6), b(47.6, 47.7, 46.9, 47)], stop=47, slip=0)
        assert fill is not None
        assert (fill.price, fill.index) == (47, 1)
        assert stop_fill([b(48, 48, 47.5, 47.6)], stop=47, slip=0) is None


class TestSellLimit:
    def test_fills_at_the_limit_or_better(self) -> None:
        assert sell_limit_on_bar(b(49, 49.6, 48.9, 49.5), limit=50, slip=0.002) is None
        assert sell_limit_on_bar(b(49.5, 50.2, 49.4, 50), limit=50, slip=0.002) == 50
        assert sell_limit_on_bar(b(51, 51.2, 50.8, 51), limit=50, slip=0.002) == 50.89


class TestMinuteHelpers:
    def test_between_is_half_open(self) -> None:
        day = bars({0: flat(1), 5: flat(2), 6: flat(3), 9: flat(4)})
        assert [x.close for x in between(day, at(5), at(9))] == [2, 3]
        assert [x.close for x in between(day, at(6), None)] == [3, 4]

    def test_last_price_is_the_last_fresh_trade(self) -> None:
        day = bars({0: flat(10), 3: flat(11), 4: flat(12)})
        two_minutes = timedelta(minutes=2)
        assert last_price(day, at=at(6), max_age=two_minutes) == 12
        assert last_price(day, at=at(4), max_age=two_minutes) == 11
        assert last_price(day, at=at(7), max_age=two_minutes) is None
