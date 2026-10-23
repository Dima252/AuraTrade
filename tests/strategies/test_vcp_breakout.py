from itertools import pairwise

import pytest

from auratrade.config import Strategy
from auratrade.indicators.bars import Bar
from auratrade.strategies.base import SymbolData
from auratrade.strategies.vcp_breakout import VcpBreakout, VcpDetail, contractions
from tests.strategies.scan_data import (
    FAILING,
    FINAL_RISE,
    RISK,
    THIRD,
    bar,
    data,
    momentum_bars,
    vcp_bars,
    with_strategy,
)

T = 74


def strategy_with(**changes: object) -> VcpBreakout:
    return VcpBreakout(with_strategy(RISK, "VCP_BREAKOUT", **changes))


def test_textbook_vcp_is_found_with_its_pivot_and_stop() -> None:
    setup = VcpBreakout(RISK).scan(data(vcp_bars()))
    assert setup is not None
    assert setup.strategy is Strategy.VCP_BREAKOUT
    assert setup.session == T
    assert setup.pivot == 97  # the final contraction's high
    assert setup.structural_stop == 91.18  # its low
    detail = setup.detail
    assert isinstance(detail, VcpDetail)
    assert [round(c.depth, 4) for c in detail.contractions] == [0.25, 0.12, 0.06]
    assert (detail.base_start, detail.base_sessions) == (50, 24)
    assert (detail.base_high, detail.base_low) == (100, 75)
    # 6 sessions at 500 shares against the 50-session average (44 x 1,000 + 6 x 500) / 50 = 940.
    assert detail.final_volume_ratio == pytest.approx(500 / 940)
    assert detail.distance_to_pivot == pytest.approx(97 / 94.75 - 1)


def test_contractions_follow_the_base_extremes() -> None:
    found = contractions(vcp_bars(), 50, T, confirm_sessions=3)
    assert [(c.high_session, c.high, c.low_session, c.low) for c in found] == [
        (50, 100, 55, 75),
        (60, 98, 64, 86.24),
        (68, 97, 71, 91.18),
    ]


def test_a_high_needs_confirming_sessions_after_it() -> None:
    # The rise to 95.5 at T is the 4th high; it has no sessions after it, so it isn't a
    # contraction. With confirm_sessions = 7 the 3rd high (6 sessions before T) isn't either.
    assert len(contractions(vcp_bars(), 50, T, confirm_sessions=7)) == 2


def test_a_contraction_still_falling_at_t_counts_at_its_depth_so_far() -> None:
    bars = vcp_bars(final_rise=[])
    found = contractions(bars, 50, len(bars) - 1, confirm_sessions=3)
    assert found[-1].low_session == len(bars) - 1
    assert len(found) == 3


def test_a_deeper_pullback_merges_into_the_earlier_contraction() -> None:
    # The third pullback reaches 85, below the second's 86.24: the second contraction runs
    # 98 -> 85 (13.3%) and there is no third. Too loose a final contraction for the default
    # cap (10%); found with the cap lifted, pivot 98.
    third = [bar(h, lo, volume=500) for h, lo in [(96, 94), (92, 89), (88, 85)]]
    bars = vcp_bars(third=third)
    found = contractions(bars, 50, T, confirm_sessions=3)
    assert [(c.high, c.low) for c in found] == [(100, 75), (98, 85)]
    assert VcpBreakout(RISK).scan(data(bars)) is None
    setup = strategy_with(max_final_depth=0.2).scan(data(bars))
    assert setup is not None
    assert (setup.pivot, setup.structural_stop) == (98, 85)


def test_contractions_always_get_shallower() -> None:
    # Any history, from a base that starts at its highest high (as the scan does): each high
    # <= the last, each low > the last.
    bars = [*vcp_bars(), *vcp_bars()[50:], *momentum_bars()]
    end = len(bars) - 1
    for lo in range(0, end - 3, 7):
        start = max(range(lo, end + 1), key=lambda i: (bars[i].high, -i))
        found = contractions(bars, start, end, confirm_sessions=1)
        for earlier, later in pairwise(found):
            assert later.high <= earlier.high
            assert later.low > earlier.low
            assert later.depth < earlier.depth


def test_trend_template_failure_rejects() -> None:
    assert VcpBreakout(RISK).scan(data(vcp_bars(), template=FAILING)) is None


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"max_depth": 0.2}, "the first contraction (25%) is too deep"),
        ({"min_depth": 0.3}, "the first contraction (25%) is too shallow"),
        ({"max_final_depth": 0.05}, "the final contraction (6%) is too loose"),
        ({"min_contractions": 4}, "too few contractions"),
        ({"max_contractions": 2}, "too many contractions"),
        ({"min_base_sessions": 25}, "the base (24 sessions) is too short"),
        ({"pivot_min_position": 0.95}, "the final contraction is low in the base"),
        ({"max_final_volume_ratio": 0.5}, "volume hasn't dried up enough (0.53)"),
    ],
)
def test_each_rule_rejects(changes: dict[str, object], why: str) -> None:
    assert strategy_with(**changes).scan(data(vcp_bars())) is None, why


def test_volume_that_does_not_dry_up_rejects() -> None:
    loud = [Bar(b.open, b.high, b.low, b.close, 1500) for b in THIRD]
    rise = [Bar(b.open, b.high, b.low, b.close, 1500) for b in FINAL_RISE]
    assert VcpBreakout(RISK).scan(data(vcp_bars(third=loud, final_rise=rise))) is None


def test_a_single_v_shaped_dip_is_no_vcp() -> None:
    down = [bar(100 - 3 * i, 98 - 3 * i) for i in range(10)]
    up = [bar(73 + 3 * i, 71 + 3 * i) for i in range(8)]
    bars = [*vcp_bars()[:51], *down, *up]
    assert VcpBreakout(RISK).scan(data(bars)) is None


def test_a_new_high_above_the_base_starts_over() -> None:
    bars = [*vcp_bars(), bar(101, 96)]
    assert VcpBreakout(RISK).scan(data(bars)) is None


def test_no_volume_average_yet_rejects() -> None:
    short = vcp_bars()[30:]  # 45 bars: no 50-session average at T
    assert VcpBreakout(RISK).scan(data(short)) is None


def test_scanning_an_earlier_session_ignores_later_bars() -> None:
    bars = vcp_bars()
    full = data(bars)
    strategy = VcpBreakout(RISK)
    for at in range(40, len(bars)):
        prefix = data(bars[: at + 1])
        assert strategy.scan(full, at) == strategy.scan(prefix), at


def test_llm_context_is_rounded_facts() -> None:
    strategy = VcpBreakout(RISK)
    setup = strategy.scan(data(vcp_bars()))
    assert setup is not None
    context = strategy.build_llm_context(setup)
    assert context["strategy"] == "VCP_BREAKOUT"
    assert context["pivot"] == 97
    assert context["structural_stop"] == 91.18
    assert context["stop_width"] == round(1 - 91.18 / 97, 4)
    assert context["contractions"] == 3
    assert context["contraction_depths"] == "25.0% -> 12.0% -> 6.0%"
    assert context["base_sessions"] == 24


def test_symbol_data_rejects_misaligned_series() -> None:
    bars = vcp_bars()
    with pytest.raises(ValueError, match="lengths differ"):
        SymbolData("X", bars, templates=[FAILING], momentum_ranks=[None] * len(bars))


def test_symbol_data_build_computes_the_trend_template() -> None:
    bars = vcp_bars()
    built = SymbolData.build(
        "X", bars, rs_ratings=[None] * len(bars), momentum_ranks=[None] * len(bars), risk=RISK
    )
    assert len(built.templates) == len(bars)
    assert not built.templates[-1].passes()  # no RS rating: rule 8 fails
