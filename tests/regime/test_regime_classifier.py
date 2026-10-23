from dataclasses import replace
from datetime import date

import pytest

from auratrade.config import DEFAULT_RISK_CONFIG, Regime, load_risk_config
from auratrade.indicators.bars import Bar
from auratrade.regime.classifier import SessionInputs, classify
from auratrade.regime.pillars import IndexTrend, TrackedBreakout
from auratrade.regime.state import RegimeState

RISK = load_risk_config(DEFAULT_RISK_CONFIG)
CFG = RISK.regime
STALE = RISK.data.macro_stale_sessions
TREND = IndexTrend(close=110, short_sma=105, long_sma=100, short_rising=True, short_falling=False)
FLAT = Bar(100, 100, 100, 100, 1000)


def inputs(**changes: object) -> SessionInputs:
    base = SessionInputs(
        session=date(2026, 10, 9),
        index_bars={"SPY": (FLAT, FLAT), "QQQ": (FLAT, FLAT)},
        index_trend={"SPY": TREND, "QQQ": TREND},
        fraction_above=0.7,
        net_new_highs=12.0,
        vix=15.0,
        vix3m=18.0,
        macro_stale_sessions=0,
        breakouts=[TrackedBreakout(6, True)] * 12,
        prior_vix_ratios=(0.8,),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_a_healthy_market_scores_five() -> None:
    state = RegimeState(regime=Regime.CONFIRMED_UPTREND, seq=0, entered_seq=0)
    result = classify(state, inputs(), CFG, macro_stale_limit=STALE)
    assert result.score == 5
    assert result.regime is Regime.CONFIRMED_UPTREND
    assert not result.changed
    assert not result.crisis.active
    assert result.features["leadership_breakouts"] == 12
    assert result.features["vix_ratio"] == pytest.approx(15 / 18)


def test_the_first_session_starts_in_correction() -> None:
    result = classify(None, inputs(), CFG, macro_stale_limit=STALE)
    assert result.regime is Regime.CORRECTION
    assert result.previous is None
    assert result.changed


def test_stale_macro_data_scores_zero_and_triggers_the_crisis_override() -> None:
    state = RegimeState(regime=Regime.CONFIRMED_UPTREND, seq=0, entered_seq=0)
    within = classify(state, inputs(macro_stale_sessions=2), CFG, macro_stale_limit=STALE)
    assert within.pillars.volatility == 1
    assert not within.crisis.active
    stale = classify(state, inputs(macro_stale_sessions=3), CFG, macro_stale_limit=STALE)
    assert stale.pillars.volatility == 0
    assert stale.crisis.reasons == ("macro_stale",)
    missing = classify(state, inputs(macro_stale_sessions=None), CFG, macro_stale_limit=STALE)
    assert missing.crisis.active


def test_the_crisis_override_reads_the_prior_ratio_and_the_spy_return() -> None:
    state = RegimeState(regime=Regime.CONFIRMED_UPTREND, seq=0, entered_seq=0)
    inverted = inputs(vix=32.0, vix3m=30.0, prior_vix_ratios=(1.01,))
    assert classify(state, inverted, CFG, macro_stale_limit=STALE).crisis.reasons == (
        "vix_term_inversion",
    )
    drop = inputs(index_bars={"SPY": (FLAT, Bar(96, 100, 95, 96, 1000)), "QQQ": (FLAT, FLAT)})
    assert classify(state, drop, CFG, macro_stale_limit=STALE).crisis.reasons == ("index_drop",)


def test_an_ftd_is_reported_in_the_features() -> None:
    state = None
    closes = [100, 101, 101.5, 102]
    prev = FLAT
    for close in closes:
        bar = Bar(close, close, close, close, 1000)
        result = classify(
            state,
            inputs(index_bars={"SPY": (prev, bar), "QQQ": (FLAT, FLAT)}),
            CFG,
            macro_stale_limit=STALE,
        )
        state, prev = result.state, bar
    ftd_bar = Bar(103.5, 103.5, 103.5, 103.5, 2000)
    result = classify(
        state,
        inputs(index_bars={"SPY": (prev, ftd_bar), "QQQ": (FLAT, FLAT)}),
        CFG,
        macro_stale_limit=STALE,
    )
    assert result.regime is Regime.EARLY_UPTREND
    assert result.features["follow_through"] == {
        "index": "SPY",
        "day": 4,
        "gain": pytest.approx(103.5 / 102 - 1),
        "weak": False,
    }


def test_the_index_bars_must_match_the_configured_indexes() -> None:
    with pytest.raises(ValueError, match="index bars"):
        classify(None, inputs(index_bars={"SPY": (FLAT, FLAT)}), CFG, macro_stale_limit=STALE)


def test_no_vix_ratio_without_both_values() -> None:
    assert inputs(vix=None).vix_ratio is None


def test_the_index_trend_pillar_at_minus_one_makes_the_downgrade_immediate() -> None:
    # correction_sessions 2: a -1 score with a neutral index trend waits a session; with an
    # index below its falling 200-day it doesn't.
    two = CFG.model_copy(
        update={"transitions": CFG.transitions.model_copy(update={"correction_sessions": 2})}
    )
    state = RegimeState(regime=Regime.UNDER_PRESSURE, seq=0, entered_seq=0)
    weak = {"fraction_above": 0.2, "vix": 35.0, "vix3m": 30.0, "breakouts": ()}
    flat = replace(TREND, short_rising=False)
    waits = classify(
        state, inputs(**weak, index_trend={"SPY": flat, "QQQ": flat}), two, macro_stale_limit=STALE
    )
    assert (waits.pillars.index_trend, waits.score) == (0, -1)
    assert waits.regime is Regime.UNDER_PRESSURE
    broken = IndexTrend(
        close=90, short_sma=95, long_sma=100, short_rising=False, short_falling=True
    )
    trend_weak = inputs(**weak, index_trend={"SPY": flat, "QQQ": broken})
    now = classify(state, trend_weak, two, macro_stale_limit=STALE)
    assert (now.pillars.index_trend, now.score) == (-1, -2)
    assert now.regime is Regime.CORRECTION
