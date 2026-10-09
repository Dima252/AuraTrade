import pytest

from auratrade.indicators.relative_strength import (
    GroupRs,
    RsLineState,
    group_rs_ranks,
    momentum_ranks,
    percentile_ranks,
    rs_line,
    rs_line_states,
    rs_score,
)


def test_rs_score_weights_each_lookback() -> None:
    # R1 = 121 / 110 - 1 = 0.1, R2 = 121 / 100 - 1 = 0.21.
    assert rs_score([100, 110, 121], [(1, 0.5), (2, 0.5)]) == [
        None,
        None,
        pytest.approx(0.5 * 0.1 + 0.5 * 0.21),
    ]


def test_rs_score_ibd_weights_need_252_sessions() -> None:
    closes = [100.0] * 252 + [150.0]
    weights = [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)]
    scores = rs_score(closes, weights)
    assert scores[251] is None
    assert scores[252] == pytest.approx(0.5)  # every lookback returned 50%


def test_rs_score_needs_weights() -> None:
    with pytest.raises(ValueError, match="weights"):
        rs_score([1.0], [])


def test_percentile_ranks() -> None:
    assert percentile_ranks({"a": 1, "b": 2, "c": 3}) == {"a": 0, "b": 50, "c": 100}


def test_percentile_ranks_ties_share_the_average_rank() -> None:
    ranks = percentile_ranks({"a": 1, "b": 1, "c": 3, "d": 4})
    assert ranks == {
        "a": pytest.approx(100 * 0.5 / 3),
        "b": pytest.approx(100 * 0.5 / 3),
        "c": pytest.approx(100 * 2 / 3),
        "d": 100,
    }
    assert percentile_ranks({"a": 5, "b": 5}) == {"a": 50, "b": 50}


def test_percentile_ranks_edge_sizes() -> None:
    assert percentile_ranks({"only": 7.0}) == {"only": 50}
    assert percentile_ranks({}) == {}


def test_momentum_ranks_per_lookback_and_skip_short_history() -> None:
    closes = {
        "A": [100, 110, 120],  # R1 = +9.1%, R2 = +20%
        "B": [100, 100, 90],  # R1 = -10%, R2 = -10%
        "C": [100, 120, 115],  # R1 = -4.2%, R2 = +15%
        "D": [100],  # too short for either
    }
    assert momentum_ranks(closes, [1, 2]) == {
        1: {"A": 100, "B": 0, "C": 50},
        2: {"A": 100, "B": 0, "C": 50},
    }
    assert momentum_ranks({"D": [100]}, [1]) == {1: {}}


def test_rs_line() -> None:
    assert rs_line([10, 20], [100, 100]) == [0.1, 0.2]
    with pytest.raises(ValueError, match="lengths differ"):
        rs_line([1.0], [1.0, 2.0])


def test_rs_line_states() -> None:
    line = [1.0, 1.1, 1.05, 0.9, 1.2]
    states = rs_line_states(line, high_window=3, near_tolerance=0.05, low_window=2)
    assert states == [
        None,
        None,
        # 1.05 vs high 1.1: within 5% (>= 1.045); lowest of (1.1, 1.05).
        RsLineState(new_high=False, near_high=True, at_low=True),
        RsLineState(new_high=False, near_high=False, at_low=True),
        RsLineState(new_high=True, near_high=True, at_low=False),
    ]


def test_rs_line_states_wait_for_the_longer_window() -> None:
    states = rs_line_states([1.0, 1.0, 1.0], high_window=1, near_tolerance=0.0, low_window=3)
    assert states[:2] == [None, None]
    assert states[2] == RsLineState(new_high=True, near_high=True, at_low=True)


def test_rs_line_states_tolerance_validation() -> None:
    with pytest.raises(ValueError, match="near_tolerance"):
        rs_line_states([1.0], high_window=1, near_tolerance=1.0, low_window=1)


def test_group_rs_ranks_use_member_medians() -> None:
    ratings = {"a": 90, "b": 80, "c": 40, "d": 60, "e": 60, "f": 99}
    groups = {"a": "G1", "b": "G1", "c": "G2", "d": "G2", "e": "G3"}  # f has no group
    assert group_rs_ranks(ratings, groups) == {
        "G1": GroupRs(median_rs=85, rank=1, percentile=100, members=2),
        "G2": GroupRs(median_rs=50, rank=3, percentile=0, members=2),
        "G3": GroupRs(median_rs=60, rank=2, percentile=50, members=1),
    }


def test_group_rs_ranks_ties_share_a_rank() -> None:
    ranks = group_rs_ranks({"a": 70, "b": 70, "c": 50}, {"a": "X", "b": "Y", "c": "Z"})
    assert [ranks[g].rank for g in ("X", "Y", "Z")] == [1, 1, 3]
    assert group_rs_ranks({"a": 70}, {}) == {}
