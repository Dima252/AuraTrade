from dataclasses import replace

import pytest

from auratrade.config import Strategy
from auratrade.strategies.base import Correction, SymbolData
from auratrade.strategies.emerging_leader import EmergingDetail, EmergingLeader, max_drawdown
from tests.strategies.scan_data import (
    FAILING,
    LEADER_CLOSES,
    RISK,
    SPY_CLOSES,
    data,
    line,
    with_strategy,
)

T = 34
CORRECTION = Correction(start=22, end=29)


def scanner(**changes: object) -> EmergingLeader:
    # Short windows so the hand-built history covers them: a 20-session high, 5 lead sessions.
    return EmergingLeader(
        with_strategy(RISK, "EMERGING_LEADER", high_sessions=20, lead_sessions=5, **changes)
    )


def leader(
    closes: list[float] = LEADER_CLOSES,
    spy: list[float] | None = SPY_CLOSES,
    corrections: tuple[Correction, ...] = (CORRECTION,),
    rs: float | None = 90,
) -> SymbolData:
    return replace(
        data(line(closes), rs=rs),
        benchmark_closes=None if spy is None else spy[: len(closes)],
        corrections=corrections,
    )


def test_the_stock_that_held_up_best_at_a_new_high_is_found() -> None:
    setup = scanner().scan(leader())
    assert setup is not None
    assert setup.strategy is Strategy.EMERGING_LEADER
    assert setup.session == T
    assert setup.pivot == 112.5  # T's high is the new 20-session high
    assert setup.structural_stop == 109.5  # the low of the last 5 sessions (30-34): 110 - 0.5
    detail = setup.detail
    assert isinstance(detail, EmergingDetail)
    assert detail.new_price_high
    assert detail.rs_line_new_high
    # From 5 sessions before the correction (17): the stock 109.5 -> 104, SPY 319 -> 285.
    assert detail.drawdown == pytest.approx(1 - 104 / 109.5)
    assert detail.index_drawdown == pytest.approx(1 - 285 / 319)
    assert detail.sessions_since_correction == 5


def test_max_drawdown() -> None:
    assert max_drawdown([10, 12, 9, 11, 6, 8]) == pytest.approx(0.5)
    assert max_drawdown([1, 2, 3]) == 0
    assert max_drawdown([]) == 0


def test_falling_more_than_1_5x_the_market_rejects() -> None:
    # The stock drops to 85 in the correction: 22% against SPY's 10.7% (2.1x).
    closes = [*LEADER_CLOSES[:23], 85, *LEADER_CLOSES[24:]]
    assert scanner().scan(leader(closes)) is None
    assert scanner(max_drawdown_vs_index=2.5).scan(leader(closes)) is not None


def test_no_correction_or_one_too_long_ago_rejects() -> None:
    assert scanner().scan(leader(corrections=())) is None
    assert scanner(max_sessions_since_correction=4).scan(leader()) is None
    later = Correction(start=T + 1, end=None)  # not started by T: the earlier one counts
    assert scanner().scan(leader(corrections=(CORRECTION, later))) is not None


def test_a_correction_still_going_is_cut_at_t() -> None:
    ongoing = scanner().scan(leader(corrections=(Correction(22, None),)))
    ends_later = scanner().scan(leader(corrections=(Correction(22, T + 10),)))
    assert ongoing is not None
    assert ends_later is not None
    assert isinstance(ongoing.detail, EmergingDetail)
    assert ongoing.detail.sessions_since_correction == 0
    assert ongoing == ends_later


def test_no_benchmark_or_a_market_without_a_drawdown_rejects() -> None:
    assert scanner().scan(leader(spy=None)) is None
    rising = [300.0 + i for i in range(len(LEADER_CLOSES))]
    assert scanner().scan(leader(spy=rising)) is None


def test_rs_and_trend_template_reject() -> None:
    assert scanner().scan(leader(rs=84)) is None
    assert scanner().scan(leader(rs=None)) is None
    base = leader()
    failing = replace(base, templates=[FAILING] * len(base.bars))
    assert scanner().scan(failing) is None


def test_rs_line_new_high_alone_is_enough() -> None:
    # The stock dips at T (below its 20-session high) while SPY falls harder: the RS line is
    # still at a new high.
    setup = scanner().scan(leader([*LEADER_CLOSES, 110.0], [*SPY_CLOSES, 280.0]))
    assert setup is not None
    assert isinstance(setup.detail, EmergingDetail)
    assert not setup.detail.new_price_high
    assert setup.detail.rs_line_new_high
    assert setup.pivot == 112.5  # the 20-session high


def test_neither_kind_of_new_high_rejects() -> None:
    assert scanner().scan(leader([*LEADER_CLOSES, 110.0], [*SPY_CLOSES, 302.0])) is None


def test_history_shorter_than_the_high_window_rejects() -> None:
    assert EmergingLeader(RISK).scan(leader()) is None  # 252 sessions needed


def test_symbol_data_checks_the_benchmark_length() -> None:
    with pytest.raises(ValueError, match="lengths differ"):
        replace(leader(), benchmark_closes=[1.0])


def test_llm_context() -> None:
    strategy = scanner()
    setup = strategy.scan(leader())
    assert setup is not None
    context = strategy.build_llm_context(setup)
    assert context["drawdown_vs_spy"] == round((1 - 104 / 109.5) / (1 - 285 / 319), 2)
    assert context["new_52_week_high"] is True
