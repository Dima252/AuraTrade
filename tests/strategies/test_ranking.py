from collections.abc import Mapping
from dataclasses import dataclass, replace

import pytest

from auratrade.config import Regime, Strategy
from auratrade.indicators.bars import Bar
from auratrade.strategies.base import Fact, Setup
from auratrade.strategies.ranking import (
    COMPONENTS,
    Features,
    Fundamentals,
    Scored,
    candidate_features,
    components,
    score_pool,
    select,
    too_extended,
)
from tests.strategies.scan_data import RISK, data, line, vcp_bars

CFG = RISK.ranking


@dataclass(frozen=True)
class Stub:
    def facts(self) -> Mapping[str, Fact]:
        return {}


def setup(
    symbol: str = "A",
    strategy: Strategy = Strategy.VCP_BREAKOUT,
    pivot: float = 100.0,
    stop: float = 95.0,
) -> Setup:
    return Setup(symbol, strategy, 0, pivot, stop, Stub())


FULL = Features(
    rs_rating=90,
    momentum_rank=80,
    group_rs=70,
    rs_line_new_high=True,
    rs_line_near_high=True,
    up_down_ratio=1.2,
    adr=0.03,
    base_stage=1,
    base_reliable=True,
    earnings_gap=True,
    eps_growth=0.4,
    eps_acceleration=0.1,
    extension=0.1,
)
BLANK = Features(
    rs_rating=None,
    momentum_rank=None,
    group_rs=None,
    rs_line_new_high=None,
    rs_line_near_high=None,
    up_down_ratio=None,
    adr=None,
    base_stage=None,
    base_reliable=False,
    earnings_gap=None,
    eps_growth=None,
    eps_acceleration=None,
    extension=None,
)


def test_components_of_a_strong_candidate() -> None:
    parts = components(FULL, tightness_rank=0.0, cfg=CFG)
    assert tuple(parts) == COMPONENTS
    assert parts == {
        "rs_rating": 0.9,
        "momentum_rank": 0.8,
        "group_rs": 0.7,
        "rs_line": 1.0,
        "up_down": 1.0,
        "tightness": 1.0,
        "base_count": 1.0,
        "catalyst": 1.0,
    }


def test_missing_data_scores_neutral() -> None:
    assert set(components(BLANK, None, CFG).values()) == {CFG.missing_component}


@pytest.mark.parametrize(
    ("stage", "reliable", "expected"),
    [(1, True, 1.0), (2, True, 2 / 3), (3, True, 1 / 3), (4, True, 0.0), (6, True, 0.0),
     (1, False, 1 / 3), (2, False, 1 / 3), (4, False, 0.0)],
)  # fmt: skip
def test_base_count_earlier_is_better_and_unreliable_is_a_third(
    stage: int, reliable: bool, expected: float
) -> None:
    features = replace(FULL, base_stage=stage, base_reliable=reliable)
    assert components(features, 0.0, CFG)["base_count"] == pytest.approx(expected)


def test_rs_line_and_up_down_levels() -> None:
    near = replace(FULL, rs_line_new_high=False, rs_line_near_high=True)
    neither = replace(FULL, rs_line_new_high=False, rs_line_near_high=False, up_down_ratio=0.9)
    assert components(near, 0.0, CFG)["rs_line"] == 0.5
    assert components(neither, 0.0, CFG)["rs_line"] == 0.0
    assert components(neither, 0.0, CFG)["up_down"] == 0.0


def test_catalyst_is_the_share_of_its_known_parts() -> None:
    mixed = replace(FULL, earnings_gap=False, eps_growth=0.3, eps_acceleration=-0.1)
    assert components(mixed, 0.0, CFG)["catalyst"] == pytest.approx(1 / 3)
    partial = replace(FULL, earnings_gap=None, eps_growth=0.1, eps_acceleration=None)
    assert components(partial, 0.0, CFG)["catalyst"] == 0.0


def test_score_is_the_average_and_tightness_ranks_within_the_pool() -> None:
    # Same features; stop widths 2%, 5% and 8% against a 3% ADR.
    pool = [(setup(s, stop=100 - w), FULL) for s, w in (("A", 2), ("B", 5), ("C", 8))]
    scored = score_pool(pool, RISK)
    assert [s.components["tightness"] for s in scored] == [1.0, 0.5, 0.0]
    for s in scored:
        assert s.score == pytest.approx(sum(s.components.values()) / 8)
    assert scored[0].score > scored[1].score > scored[2].score


def test_no_adr_leaves_tightness_neutral() -> None:
    [scored] = score_pool([(setup(), replace(FULL, adr=None))], RISK)
    assert scored.components["tightness"] == CFG.missing_component


def scored(symbol: str, strategy: Strategy, score: float, extension: float | None = 0.1) -> Scored:
    return Scored(setup(symbol, strategy), replace(FULL, extension=extension), {}, score)


def test_too_extended_excludes_except_momentum_and_ep() -> None:
    assert too_extended(scored("A", Strategy.VCP_BREAKOUT, 1, extension=0.26), RISK)
    assert not too_extended(scored("A", Strategy.VCP_BREAKOUT, 1, extension=0.25), RISK)
    assert too_extended(scored("A", Strategy.LEADER_PULLBACK, 1, extension=None), RISK)
    assert not too_extended(scored("A", Strategy.MOMENTUM_BREAKOUT, 1, extension=0.4), RISK)
    assert not too_extended(scored("A", Strategy.EPISODIC_PIVOT, 1, extension=None), RISK)


def test_select_keeps_fundable_unextended_setups_one_per_ticker_best_first() -> None:
    pool = [
        scored("A", Strategy.VCP_BREAKOUT, 0.6),
        scored("A", Strategy.LEADER_PULLBACK, 0.7),  # A's best: takes A's one slot
        scored("B", Strategy.MOMENTUM_BREAKOUT, 0.65, extension=0.4),  # exempt
        scored("C", Strategy.VCP_BREAKOUT, 0.9, extension=0.3),  # too extended
        scored("D", Strategy.EMERGING_LEADER, 0.95),  # not fundable in an uptrend
        scored("E", Strategy.EPISODIC_PIVOT, 0.65),
    ]
    chosen = select(pool, Regime.CONFIRMED_UPTREND, RISK, cap=40)
    assert [(s.setup.symbol, s.setup.strategy) for s in chosen] == [
        ("A", Strategy.LEADER_PULLBACK),
        ("B", Strategy.MOMENTUM_BREAKOUT),
        ("E", Strategy.EPISODIC_PIVOT),
    ]
    assert [s.setup.symbol for s in select(pool, Regime.CONFIRMED_UPTREND, RISK, cap=2)] == [
        "A",
        "B",
    ]
    assert select(pool, Regime.CONFIRMED_UPTREND, RISK, cap=0) == []
    early = select(pool, Regime.EARLY_UPTREND, RISK, cap=40)
    assert [s.setup.symbol for s in early] == ["D", "E"]
    assert select(pool, Regime.CORRECTION, RISK, cap=40) == []


def test_a_tie_keeps_the_earlier_strategy() -> None:
    pool = [scored("A", Strategy.LEADER_PULLBACK, 0.7), scored("A", Strategy.VCP_BREAKOUT, 0.7)]
    for order in (pool, pool[::-1]):
        [chosen] = select(order, Regime.CONFIRMED_UPTREND, RISK, cap=40)
        assert chosen.setup.strategy is Strategy.VCP_BREAKOUT


def test_candidate_features_on_a_history() -> None:
    bars = vcp_bars()
    spy = [100.0] * len(bars)  # flat: the RS line follows the price
    d = replace(data(bars, rs=91, momentum=88), benchmark_closes=spy)
    t = len(bars) - 1
    f = candidate_features(
        d,
        t,
        RISK,
        group_rs=60,
        fundamentals=Fundamentals(eps_growth=0.3, eps_acceleration=None),
        base_stage=2,
        base_reliable=True,
    )
    assert (f.rs_rating, f.momentum_rank, f.group_rs) == (91, 88, 60)
    assert (f.base_stage, f.base_reliable) == (2, True)
    assert (f.eps_growth, f.eps_acceleration) == (0.3, None)
    assert f.rs_line_new_high is None  # 75 bars: no 252-session window yet
    assert f.earnings_gap is None  # no earnings dates at all
    assert f.adr is not None
    assert f.up_down_ratio is not None
    sma = sum(b.close for b in bars[-50:]) / 50
    assert f.extension == pytest.approx(bars[t].close / sma - 1)


def test_rs_line_highs_and_earnings_gaps() -> None:
    # 260 rising sessions, then a 6% gap on 2x volume on an earnings reaction session.
    bars = line([20 + 0.1 * i for i in range(260)])
    gap = Bar(open=48.5, high=49.5, low=48.3, close=49.0, volume=2000)  # 5.7% above 45.9
    bars = [*bars, gap, *line([48.8, 48.6])]
    t = len(bars) - 1
    d = replace(
        data(bars),
        benchmark_closes=[100.0] * len(bars),
        earnings_sessions=frozenset({260}),
    )
    f = candidate_features(d, t, RISK)
    assert f.rs_line_new_high is False  # 48.6 is below the gap day's 49.0 ...
    assert f.rs_line_near_high is True  # ... but within 5%
    assert f.earnings_gap is True
    assert (
        candidate_features(replace(d, earnings_sessions=frozenset({100})), t, RISK).earnings_gap
        is False
    )
    assert candidate_features(replace(d, benchmark_closes=None), t, RISK).rs_line_new_high is None


def test_strategies_take_turns() -> None:
    # Five strong pullbacks and two weaker VCPs: the VCPs still get every other slot.
    pool = [scored(f"P{i}", Strategy.LEADER_PULLBACK, 0.9 - i / 100) for i in range(5)]
    pool += [scored("V0", Strategy.VCP_BREAKOUT, 0.6), scored("V1", Strategy.VCP_BREAKOUT, 0.5)]
    pool += [scored("M0", Strategy.MOMENTUM_BREAKOUT, 0.7)]
    chosen = [s.setup.symbol for s in select(pool, Regime.CONFIRMED_UPTREND, RISK, cap=6)]
    # Round 1 in score order: P0, M0, V0; round 2: P1, V1 (momentum has none left); then P2.
    assert chosen == ["P0", "M0", "V0", "P1", "V1", "P2"]
    assert len(select(pool, Regime.CONFIRMED_UPTREND, RISK, cap=40)) == len(pool)
    # A round cut short by the cap keeps its best.
    assert [s.setup.symbol for s in select(pool, Regime.CONFIRMED_UPTREND, RISK, cap=2)] == [
        "P0",
        "M0",
    ]
