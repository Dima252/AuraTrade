from auratrade.config import Strategy
from auratrade.strategies.leader_pullback import LeaderPullback
from auratrade.strategies.vcp_breakout import VcpBreakout
from auratrade.strategies.verdict_cache import CachedVerdict, needs_review, structure
from tests.strategies.scan_data import ADVANCE, PULLBACK, RISK, data, vcp_bars

MAX_AGE = RISK.ranking.verdict_max_age_sessions


def test_vcp_structure_is_the_pivot_and_a_pullbacks_is_the_tested_average() -> None:
    vcp = VcpBreakout(RISK).scan(data(vcp_bars()))
    pullback = LeaderPullback(RISK).scan(data([*ADVANCE, *PULLBACK]))
    assert vcp is not None
    assert pullback is not None
    assert structure(vcp) == 97.0
    assert structure(pullback) == 10


def test_review_rules() -> None:
    vcp = VcpBreakout(RISK).scan(data(vcp_bars()))
    assert vcp is not None
    fresh = CachedVerdict("TEST", Strategy.VCP_BREAKOUT, 97.0, sessions_ago=MAX_AGE)
    assert not needs_review(vcp, fresh, MAX_AGE)  # unchanged and 3 sessions old: cached
    assert needs_review(vcp, None, MAX_AGE)  # new ticker
    assert needs_review(vcp, CachedVerdict("OTHER", Strategy.VCP_BREAKOUT, 97.0, 1), MAX_AGE)
    assert needs_review(vcp, CachedVerdict("TEST", Strategy.LEADER_PULLBACK, 97.0, 1), MAX_AGE)
    assert needs_review(vcp, CachedVerdict("TEST", Strategy.VCP_BREAKOUT, 96.5, 1), MAX_AGE)
    stale = CachedVerdict("TEST", Strategy.VCP_BREAKOUT, 97.0, sessions_ago=MAX_AGE + 1)
    assert needs_review(vcp, stale, MAX_AGE)


def test_a_pullbacks_moving_trigger_is_not_a_change() -> None:
    first = LeaderPullback(RISK).scan(data([*ADVANCE, *PULLBACK]))
    assert first is not None
    cached = CachedVerdict("TEST", Strategy.LEADER_PULLBACK, structure(first), sessions_ago=1)
    moved = first.__class__(
        first.symbol, first.strategy, first.session + 1, first.pivot - 0.8, first.structural_stop,
        first.detail,
    )  # fmt: skip
    assert not needs_review(moved, cached, MAX_AGE)
