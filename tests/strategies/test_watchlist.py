"""The evening's full scan (strategies.watchlist)."""

from collections.abc import Sequence
from datetime import date, timedelta

import pytest

from auratrade.config import Regime, RiskConfig, Strategy
from auratrade.indicators.bars import Bar
from auratrade.strategies.base import Correction
from auratrade.strategies.ranking import Fundamentals
from auratrade.strategies.watchlist import (
    SymbolHistory,
    correction_spells,
    corrections_for,
    cross_sections,
    membership,
    scan_session,
)
from tests.strategies.scan_data import RISK, vcp_bars

START = date(2025, 1, 1)
VOLUME = 20_000  # x the scan data's 1,000 shares: every symbol clears $10M a day


def days(n: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(n)]


def history(ticker_id: int, symbol: str, bars: Sequence[Bar], **kw: object) -> SymbolHistory:
    return SymbolHistory(
        ticker_id,
        symbol,
        days(len(bars)),
        list(bars),
        [b.close for b in bars],
        **kw,  # type: ignore[arg-type]
    )


def scaled(bars: Sequence[Bar]) -> list[Bar]:
    return [Bar(b.open, b.high, b.low, b.close, b.volume * VOLUME) for b in bars]


# A year of steady advance from 20 to 59, then the scan data's textbook VCP (pivot 98).
ADVANCE = [
    Bar(20 + i * 0.15, 20.3 + i * 0.15, 19.8 + i * 0.15, 20 + i * 0.15, 1000) for i in range(260)
]
LEADER = scaled([*ADVANCE, *vcp_bars()])
N = len(LEADER)


def laggard(ticker_id: int, drift: float) -> SymbolHistory:
    closes = [50 + drift * i for i in range(N)]
    bars = [Bar(c, c + 0.3, c - 0.3, c, 1_000_000) for c in closes]
    return history(ticker_id, f"L{ticker_id}", bars, industry="SLOW")


UNIVERSE = [
    history(1, "LEAD", LEADER, industry="FAST", fundamentals=Fundamentals(0.4, 0.1)),
    *(laggard(i, d) for i, d in [(2, 0.0), (3, -0.01), (4, 0.005), (5, -0.02)]),
]
T = UNIVERSE[0].dates[-1]
SPY = {d: 400.0 + i * 0.1 for i, d in enumerate(UNIVERSE[0].dates)}
UPTREND = dict.fromkeys(UNIVERSE[0].dates, Regime.CONFIRMED_UPTREND)


def scan(regime: Regime = Regime.CONFIRMED_UPTREND, cap: int = 40, **kw: object) -> list:
    return scan_session(
        UNIVERSE,
        session=T,
        benchmark=SPY,
        regimes=UPTREND,
        regime=regime,
        risk=RISK,
        cap=cap,
        **kw,  # type: ignore[arg-type]
    )


def test_membership_needs_a_years_history() -> None:
    members = membership(UNIVERSE, RISK)
    first = UNIVERSE[0].dates[RISK.universe.min_history_sessions - 2]
    assert first not in members
    assert members[T] == {1, 2, 3, 4, 5}


def test_cross_sections_rank_within_each_sessions_universe() -> None:
    members = membership(UNIVERSE, RISK)
    rs, momentum = cross_sections(UNIVERSE, members, RISK)
    assert rs[1][0] is None
    assert rs[1][-1] == 100.0
    assert rs[5][-1] == 0.0
    assert momentum[1][-1] == 100.0


def test_the_leader_is_found_scored_and_ranked() -> None:
    items = {i.setup.strategy: i for i in scan()}
    assert {i.symbol for i in items.values()} == {"LEAD"}
    # One slot per ticker (#138): its best-scoring setup takes it; the others stay listed.
    assert {s: i.rank for s, i in items.items()} == {
        Strategy.VCP_BREAKOUT: None,
        Strategy.MOMENTUM_BREAKOUT: None,
        Strategy.LEADER_PULLBACK: 1,
    }
    vcp = items[Strategy.VCP_BREAKOUT]
    assert vcp.setup.pivot == pytest.approx(97.0)
    assert vcp.features.rs_rating == 100.0
    assert vcp.features.group_rs == 100.0
    assert vcp.features.eps_growth == 0.4
    assert vcp.close == LEADER[-1].close
    assert vcp.atr is not None
    assert vcp.adr is not None
    assert vcp.sma_50 is not None
    assert vcp.dollar_volume == pytest.approx(sum(b.close * b.volume for b in LEADER[-50:]) / 50)
    assert vcp.industry == "FAST"
    assert 0 < vcp.score <= 1


def test_every_setup_is_kept_but_only_the_fundable_are_ranked() -> None:
    items = scan(Regime.CORRECTION)
    assert items
    assert all(i.rank is None for i in items)


def test_the_cap_limits_the_ranked() -> None:
    items = scan(cap=1)
    assert sorted(i.rank for i in items if i.rank is not None) == [1]


def test_a_symbol_without_a_bar_on_t_is_not_scanned() -> None:
    stale = history(9, "STALE", LEADER[:-1], industry="FAST")
    items = scan_session(
        [*UNIVERSE, stale],
        session=T,
        benchmark=SPY,
        regimes=UPTREND,
        regime=Regime.CONFIRMED_UPTREND,
        risk=RISK,
        cap=40,
    )
    assert "STALE" not in {i.symbol for i in items}


def test_a_young_member_is_ranked_on_the_lookbacks_it_has() -> None:
    young = risk_with_history(1)  # a member from its first $10M-a-day average (bar 50)
    members = membership(UNIVERSE, young)
    _, momentum = cross_sections(UNIVERSE, members, young)
    first = young.universe.dollar_volume_sessions - 1
    assert momentum[1][first - 1] is None
    assert momentum[1][first] is not None  # the 1-month return only


def risk_with_history(sessions: int) -> RiskConfig:
    u = RISK.universe.model_copy(update={"min_history_sessions": sessions})
    return RISK.model_copy(update={"universe": u})


def test_correction_spells_and_their_positions() -> None:
    d = [date(2020, 3, i) for i in range(2, 14)]
    labels = [Regime.CONFIRMED_UPTREND] * 2 + [Regime.CORRECTION] * 3 + [Regime.EARLY_UPTREND] * 4
    labels += [Regime.CORRECTION] * 3
    spells = correction_spells(dict(zip(d, labels, strict=True)))
    assert spells == [(d[2], d[4]), (d[9], None)]
    # A security listed from the 4th session: the first spell is cut to its bars.
    assert corrections_for(d[3:], spells) == (Correction(0, 1), Correction(6, None))
    # One that stopped trading before the second spell keeps only the first.
    assert corrections_for(d[:6], spells) == (Correction(2, 4),)
    # One that starts after a spell ended drops it.
    assert corrections_for(d[6:], spells) == (Correction(3, None),)
