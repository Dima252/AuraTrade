from __future__ import annotations

import math

import pytest

from auratrade.config import PortfolioConfig, load_risk_config
from auratrade.strategies.core_allocation import (
    Band,
    Halt,
    Trend,
    book_targets,
    core_level,
    core_weights,
    exposure,
    halt_at_close,
    halt_on_ranking,
    realized_volatility,
    rule_level,
    shift_band,
    sma,
    start_halt,
    trend_state,
    twin_targets,
    vol_capped,
)

CFG = load_risk_config().portfolio


def cfg(**update: object) -> PortfolioConfig:
    return CFG.model_copy(update=update)


def month_ends(last: float) -> list[float]:
    """Nine months at 100, then ``last``: SMA10 = (900 + last) / 10."""
    return [100.0] * 9 + [last]


class TestTrend:
    def test_sma_needs_enough_months(self) -> None:
        assert sma([1.0, 2.0], 3) is None
        assert sma([5.0, 1.0, 2.0, 3.0], 3) == 2.0

    def test_no_state_before_ten_month_ends(self) -> None:
        assert trend_state(None, [100.0] * 9, CFG) is None

    def test_the_first_state_has_no_buffer(self) -> None:
        assert trend_state(None, month_ends(100.0), CFG) is Trend.UP  # equal counts as UP
        assert trend_state(None, month_ends(99.9), CFG) is Trend.DOWN

    @pytest.mark.parametrize(
        ("previous", "last", "expected"),
        [
            # SMA 99.8, the DOWN edge 97.804: inside the band, the state stays.
            (Trend.UP, 98.0, Trend.UP),
            # SMA 99.7, the edge 97.706: below it.
            (Trend.UP, 97.0, Trend.DOWN),
            # SMA 100.2, the UP edge 102.204: inside.
            (Trend.DOWN, 102.0, Trend.DOWN),
            # SMA 100.3, the edge 102.306: above it.
            (Trend.DOWN, 103.0, Trend.UP),
            (Trend.DOWN, 97.0, Trend.DOWN),
            (Trend.UP, 103.0, Trend.UP),
        ],
    )
    def test_the_buffer(self, previous: Trend, last: float, expected: Trend) -> None:
        assert trend_state(previous, month_ends(last), CFG) is expected


class TestVolatility:
    def test_needs_enough_returns(self) -> None:
        assert realized_volatility([0.01] * 20, CFG) is None
        assert realized_volatility([0.01], cfg(vol_sessions=1)) is None

    def test_annualized_sample_deviation_of_the_last_returns(self) -> None:
        # Two returns ±1%: sample deviation sqrt(2 x 0.0001 / 1), x sqrt(252).
        got = realized_volatility([0.5, 0.01, -0.01], cfg(vol_sessions=2))
        assert got == pytest.approx(math.sqrt(0.0002) * math.sqrt(252))
        assert realized_volatility([0.003] * 21, CFG) == 0.0

    def test_the_cap_and_unknown_volatility(self) -> None:
        assert vol_capped(None, CFG)
        assert not vol_capped(0.25, CFG)
        assert vol_capped(0.2501, CFG)


class TestHalt:
    def test_turns_on_at_a_quarter_below_the_peak(self) -> None:
        halt = start_halt(100.0)
        halt, turned = halt_at_close(halt, 120.0, CFG)
        assert (halt, turned) == (Halt(False, 120.0), False)
        halt, turned = halt_at_close(halt, 90.01, CFG)
        assert (halt.on, turned) == (False, False)
        halt, turned = halt_at_close(halt, 90.0, CFG)  # exactly 75% of 120
        assert (halt, turned) == (Halt(True, 120.0), True)
        # While on, closes change nothing and it doesn't turn on again.
        assert halt_at_close(halt, 130.0, CFG) == (halt, False)

    def test_leaves_on_a_flip_up_with_the_peak_reset(self) -> None:
        assert halt_on_ranking(Halt(True, 120.0), True, 95.0) == Halt(False, 95.0)

    def test_leaves_at_a_new_peak(self) -> None:
        assert halt_on_ranking(Halt(True, 120.0), False, 121.0) == Halt(False, 121.0)
        assert halt_on_ranking(Halt(True, 120.0), False, 119.0) == Halt(True, 120.0)

    def test_off_stays_off(self) -> None:
        assert halt_on_ranking(Halt(False, 120.0), True, 50.0) == Halt(False, 120.0)


class TestLevels:
    @pytest.mark.parametrize(
        ("state", "capped", "halted", "expected"),
        [
            (Trend.UP, False, False, 1.3),
            (Trend.DOWN, False, False, 0.5),
            (Trend.UP, True, False, 1.0),
            (Trend.UP, False, True, 1.0),
            (Trend.DOWN, True, True, 0.5),
        ],
    )
    def test_rule_level(self, state: Trend, capped: bool, halted: bool, expected: float) -> None:
        assert rule_level(state, capped, halted, CFG) == expected

    def test_the_band_is_wider_only_upward_in_a_clean_up_trend(self) -> None:
        assert shift_band(Trend.UP, False, False, 1.3, CFG) == Band(1.1, 1.5)
        assert shift_band(Trend.UP, False, False, 1.4, CFG) == Band(1.2, 1.5)
        assert shift_band(Trend.DOWN, False, False, 0.5, CFG) == Band(0.3, 0.5)
        assert shift_band(Trend.UP, True, False, 1.0, CFG) == Band(0.8, 1.0)
        assert shift_band(Trend.UP, False, True, 1.0, CFG) == Band(0.8, 1.0)
        assert shift_band(Trend.DOWN, False, False, 0.4, CFG) == Band(0.3, 0.4)


class TestWeights:
    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (1.3, {"SPY": 0.7, "SSO": 0.3}),
            (1.5, {"SPY": 0.5, "SSO": 0.5}),
            (2.0, {"SSO": 1.0}),
            (1.0, {"SPY": 1.0}),
            (0.5, {"SPY": 0.5, "BIL": 0.5}),
            (0.0, {"BIL": 1.0}),
        ],
    )
    def test_core_weights(self, level: float, expected: dict[str, float]) -> None:
        got = core_weights(level, CFG)
        assert got == pytest.approx(expected)
        assert set(got) == set(expected)
        assert exposure(got, CFG) == pytest.approx(level)

    @pytest.mark.parametrize("level", [-0.1, 2.1])
    def test_a_level_the_funds_cant_reach(self, level: float) -> None:
        with pytest.raises(ValueError, match="outside"):
            core_weights(level, CFG)

    def test_satellite_funds_carry_no_exposure(self) -> None:
        assert exposure({"QQQ": 0.2, "BIL": 0.8}, CFG) == 0.0

    def test_twin_is_the_rule_level_on_the_whole_account(self) -> None:
        assert twin_targets(1.3, CFG) == core_weights(1.3, CFG)

    @pytest.mark.parametrize(
        ("level", "core"), [(1.3, 1.375), (1.5, 1.625), (1.0, 1.0), (0.5, 0.375), (0.3, 0.125)]
    )
    def test_the_core_is_sized_for_a_neutral_satellite(self, level: float, core: float) -> None:
        assert core_level(level, CFG) == pytest.approx(core)

    @pytest.mark.parametrize("level", [1.5, 1.3, 1.0, 0.5, 0.3])
    def test_at_neutral_the_book_is_the_twin(self, level: float) -> None:
        got = book_targets(level, {"SPY": 0.2}, CFG)
        assert got == pytest.approx(twin_targets(level, CFG))
        assert exposure(got, CFG) == pytest.approx(level)

    def test_book_targets_add_the_satellite_to_the_core_share(self) -> None:
        got = book_targets(1.3, {"GLD": 0.05, "SPY": 0.05, "BIL": 0.10, "QQQ": 0.0}, CFG)
        # The core at 1.375: SPY 0.8 x 0.625 = 0.5, SSO 0.8 x 0.375 = 0.3.
        assert got == pytest.approx({"SPY": 0.5 + 0.05, "SSO": 0.3, "GLD": 0.05, "BIL": 0.10})
        assert "QQQ" not in got
        assert math.fsum(got.values()) == pytest.approx(1.0)
        # 15 of the satellite's 20 points out of the S&P: 1.3 - 0.15.
        assert exposure(got, CFG) == pytest.approx(1.15)
