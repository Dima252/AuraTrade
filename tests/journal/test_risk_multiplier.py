"""The progressive-exposure multiplier, row by row (README §8)."""

import pytest

from auratrade.config import load_risk_config
from auratrade.journal.risk_multiplier import (
    Day,
    MultiplierState,
    Reading,
    Row,
    TradeResult,
    replay,
    step,
    trade_result,
)

M = load_risk_config().multiplier
START = 10_000.0
WIN, LOSS, SCRATCH = 1.0, -1.0, 0.0


def proven(equity: float = START) -> MultiplierState:
    """Past "prove it first": two wins, equity unchanged (no new high)."""
    state, _ = replay(START, [Day(START, (WIN, WIN))], M)
    if equity != START:
        state, _ = step(state, Day(equity), M)
    return state


def run(state: MultiplierState, *days: Day) -> tuple[MultiplierState, list[Row]]:
    rows = []
    for day in days:
        state, reading = step(state, day, M)
        rows.append(reading.row)
    return state, rows


def test_trade_results() -> None:
    assert trade_result(0.5, M) is TradeResult.WIN
    assert trade_result(0.49, M) is TradeResult.SCRATCH
    assert trade_result(-0.49, M) is TradeResult.SCRATCH
    assert trade_result(-0.5, M) is TradeResult.LOSS


def test_readings_carry_the_table_multipliers() -> None:
    _, readings = replay(START, [Day(START)], M)
    assert readings == [Reading(Row.PROVE_IT, 0.5)]


# --- Row 4: prove it first ---------------------------------------------------------------


def test_prove_it_first_ends_after_two_wins_not_necessarily_consecutive() -> None:
    _, rows = run(
        MultiplierState.start(START),
        Day(START, (WIN,)),
        Day(START, (LOSS, SCRATCH)),
        Day(START, (WIN,)),
    )
    assert rows == [Row.PROVE_IT, Row.PROVE_IT, Row.NORMAL]


def test_prove_it_restarts_only_after_a_manual_drawdown_halt_reset() -> None:
    _, rows = run(proven(), Day(START), Day(START, drawdown_halt_reset=True))
    assert rows == [Row.NORMAL, Row.PROVE_IT]


def test_start_needs_positive_equity() -> None:
    with pytest.raises(ValueError, match="equity"):
        MultiplierState.start(0.0)
    with pytest.raises(ValueError, match="equity"):
        step(proven(), Day(float("nan")), M)


# --- Rows 1 and 2: drawdown tiers --------------------------------------------------------


def test_drawdown_tier_boundaries() -> None:
    state = proven()
    _, rows = run(state, Day(9_501))
    assert rows == [Row.NORMAL]  # 4.99%
    _, rows = run(state, Day(9_500))
    assert rows == [Row.DRAWDOWN]  # 5%
    _, rows = run(state, Day(9_001))
    assert rows == [Row.DRAWDOWN]
    _, rows = run(state, Day(9_000))
    assert rows == [Row.DEEP_DRAWDOWN]  # 10%


def test_a_tier_ends_on_recovery() -> None:
    _, rows = run(proven(), Day(9_000), Day(9_400), Day(9_600))
    assert rows == [Row.DEEP_DRAWDOWN, Row.DRAWDOWN, Row.NORMAL]


def test_three_wins_step_size_up_one_tier_at_a_time() -> None:
    """Wins in the deep tier end it; the 5% tier then applies, and needs its own 3 wins."""
    state, rows = run(
        proven(),
        Day(8_800),
        Day(8_800, (WIN,)),
        Day(8_800, (WIN, SCRATCH)),
        Day(8_800, (WIN,)),
        Day(8_800, (WIN, WIN)),
        Day(8_800, (WIN,)),
    )
    assert rows == [
        Row.DEEP_DRAWDOWN,
        Row.DEEP_DRAWDOWN,
        Row.DEEP_DRAWDOWN,
        Row.DRAWDOWN,  # 3 wins in the deep tier
        Row.DRAWDOWN,
        Row.NORMAL,  # 3 wins in the 5% tier
    ]
    # The tiers stay ended at the same equity, and re-apply only on a new low.
    _, rows = run(state, Day(8_800), Day(8_850), Day(8_799))
    assert rows == [Row.NORMAL, Row.NORMAL, Row.DEEP_DRAWDOWN]


def test_wins_only_count_while_the_tier_is_in_force() -> None:
    """Wins in the 5% tier don't carry into the 10% tier when the drawdown deepens."""
    _, rows = run(
        proven(),
        Day(9_400),
        Day(9_400, (WIN, WIN)),
        Day(8_900),
        Day(8_900, (WIN,)),
    )
    assert rows == [Row.DRAWDOWN, Row.DRAWDOWN, Row.DEEP_DRAWDOWN, Row.DEEP_DRAWDOWN]


def test_drawdown_beats_every_other_row() -> None:
    _, rows = run(MultiplierState.start(START), Day(8_000, (LOSS, LOSS, LOSS)))
    assert rows == [Row.DEEP_DRAWDOWN]


# --- Row 3: losing streak ----------------------------------------------------------------


def test_losing_streak_starts_at_three_losses_in_a_row() -> None:
    _, rows = run(proven(), Day(START, (LOSS, LOSS)), Day(START, (SCRATCH, LOSS)))
    assert rows == [Row.NORMAL, Row.LOSING_STREAK]  # a scratch doesn't break it
    _, rows = run(proven(), Day(START, (LOSS, LOSS, WIN, LOSS)))
    assert rows == [Row.NORMAL]


def test_losing_streak_ends_after_two_wins_in_a_row() -> None:
    state, _ = run(proven(), Day(START, (LOSS, LOSS, LOSS)))
    _, rows = run(state, Day(START, (WIN, LOSS)), Day(START, (WIN, SCRATCH)), Day(START, (WIN,)))
    assert rows == [Row.LOSING_STREAK, Row.LOSING_STREAK, Row.NORMAL]


def test_losing_streak_ends_on_a_new_equity_high() -> None:
    state, _ = run(proven(), Day(START, (LOSS, LOSS, LOSS)))
    _, rows = run(state, Day(START + 1))
    assert rows == [Row.NORMAL]


def test_another_loss_keeps_the_streak() -> None:
    state, _ = run(proven(), Day(START, (LOSS, LOSS, LOSS)))
    _, rows = run(state, Day(START, (WIN, LOSS, WIN)))
    assert rows == [Row.LOSING_STREAK]


# --- Row 5: unproven regime --------------------------------------------------------------


def test_unproven_regime_live_only() -> None:
    _, rows = run(
        proven(),
        Day(START, regime_closed_trades=4),
        Day(START, regime_closed_trades=5),
        Day(START),
    )
    assert rows == [Row.UNPROVEN_REGIME, Row.NORMAL, Row.NORMAL]


# --- Row 6: trading well -----------------------------------------------------------------


def test_trading_well_needs_ten_trades_a_recent_high_and_positive_expectancy() -> None:
    state, rows = run(proven(), Day(START, (WIN,) * 7))
    assert rows == [Row.NORMAL]  # 9 closed trades
    state, rows = run(state, Day(START + 100, (LOSS,)))
    assert rows == [Row.TRADING_WELL]  # 10 trades, new high today
    state, rows = run(state, Day(START + 50), Day(START + 50), Day(START + 50), Day(START + 50))
    assert rows == [Row.TRADING_WELL] * 4  # the high was within the last 5 sessions
    _, rows = run(state, Day(START + 50))
    assert rows == [Row.NORMAL]  # 5 sessions since the high


def test_trading_well_needs_positive_expectancy_over_the_last_ten() -> None:
    state, _ = run(proven(), Day(START, (LOSS,) * 2 + (WIN,) * 2 + (SCRATCH,) * 4))
    # Last 10 R: W W L L W W 0 0 0 0 -> +2, then two losses -> 0.
    _, rows = run(state, Day(START + 100, (LOSS, LOSS)))
    assert rows == [Row.NORMAL]


def test_no_new_high_ever_is_normal() -> None:
    _, rows = run(proven(), Day(START, (WIN,) * 10))
    assert rows == [Row.NORMAL]
