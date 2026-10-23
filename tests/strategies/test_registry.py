import pytest
from pydantic import ValidationError

from auratrade.config import (
    ExitSetName,
    Regime,
    RiskConfig,
    Strategy,
    TriggerKind,
)
from auratrade.strategies.episodic_pivot import EpisodicPivot
from auratrade.strategies.registry import build_strategies, cross_section, fundable, scan_all
from auratrade.strategies.vcp_breakout import VcpBreakout
from tests.strategies.scan_data import GAP, QUIET, RISK, data, momentum_bars, vcp_bars


def test_registry_holds_the_core_strategies() -> None:
    strategies = build_strategies(RISK)
    assert set(strategies) == {
        Strategy.VCP_BREAKOUT,
        Strategy.MOMENTUM_BREAKOUT,
        Strategy.EPISODIC_PIVOT,
    }
    for name, strategy in strategies.items():
        assert strategy.strategy is name
        assert strategy.system_prompt


def test_scan_all_runs_every_strategy() -> None:
    strategies = build_strategies(RISK)
    # The textbook VCP is also a momentum flag (+69% into its base) when it ranks in the top 2%.
    both = scan_all(strategies, data(vcp_bars()))
    assert [s.strategy for s in both] == [Strategy.VCP_BREAKOUT, Strategy.MOMENTUM_BREAKOUT]
    vcp_only = scan_all(strategies, data(vcp_bars(), momentum=None))
    assert [s.strategy for s in vcp_only] == [Strategy.VCP_BREAKOUT]
    assert [s.strategy for s in scan_all(strategies, data(momentum_bars()))] == [
        Strategy.MOMENTUM_BREAKOUT
    ]
    assert [s.strategy for s in scan_all(strategies, data([*QUIET, GAP]))] == [
        Strategy.EPISODIC_PIVOT
    ]
    assert scan_all(strategies, data(QUIET)) == []


def test_policies_come_from_risk_yaml() -> None:
    vcp = VcpBreakout(RISK)
    assert vcp.entry_policy == RISK.plans.entry[Strategy.VCP_BREAKOUT]
    assert vcp.entry_policy.trigger is TriggerKind.PIVOT
    assert vcp.exit_policy == RISK.plans.exit_sets[ExitSetName.MINERVINI]
    ep = EpisodicPivot(RISK)
    assert ep.entry_policy.trigger is TriggerKind.OPENING_RANGE_HIGH
    assert ep.exit_policy == RISK.plans.exit_sets[ExitSetName.KULLAMAGI]


def test_default_risk_is_the_regime_base_risk_where_fundable() -> None:
    vcp = VcpBreakout(RISK)
    assert vcp.default_risk(Regime.CONFIRMED_UPTREND) == RISK.regimes.CONFIRMED_UPTREND.base_risk
    assert vcp.default_risk(Regime.UNDER_PRESSURE) == RISK.regimes.UNDER_PRESSURE.base_risk
    assert vcp.default_risk(Regime.EARLY_UPTREND) is None
    assert vcp.default_risk(Regime.CORRECTION) is None


def test_fundable_lists_the_regime_strategies() -> None:
    assert {f.strategy for f in fundable(RISK, Regime.CONFIRMED_UPTREND)} >= {
        Strategy.VCP_BREAKOUT,
        Strategy.MOMENTUM_BREAKOUT,
        Strategy.EPISODIC_PIVOT,
    }
    assert fundable(RISK, Regime.CORRECTION) == ()


def test_llm_context_rejects_another_strategy_setup() -> None:
    setup = VcpBreakout(RISK).scan(data(vcp_bars()))
    assert setup is not None
    with pytest.raises(ValueError, match="given to"):
        EpisodicPivot(RISK).build_llm_context(setup)


def test_cross_section_takes_the_best_momentum_rank() -> None:
    # A: the best 1-month return; B: the best 6-month return; C: last on both.
    n = 260
    a = [100.0] * (n - 21) + [100.0 + 2 * i for i in range(21)]  # +40% in 1 and 3 months
    b = [50.0 + i for i in range(n)]  # +69% in 6 months, less than A in 1 and 3
    c = [100.0] * n
    ranks = cross_section({"A": a, "B": b, "C": c}, RISK)
    assert ranks.momentum_ranks["A"] == 100
    assert ranks.momentum_ranks["B"] == 100
    assert ranks.momentum_ranks["C"] == 0
    assert set(ranks.rs_ratings) == {"A", "B", "C"}


def _dump() -> dict[str, object]:
    raw: dict[str, object] = RISK.model_dump(mode="json")
    return raw


@pytest.mark.parametrize(
    ("strategy", "changes", "message"),
    [
        ("VCP_BREAKOUT", {"max_base_sessions": 10}, "max_base_sessions"),
        ("VCP_BREAKOUT", {"max_contractions": 1}, "max_contractions"),
        ("VCP_BREAKOUT", {"min_contractions": 1, "max_contractions": 3}, "at least 2"),
        ("MOMENTUM_BREAKOUT", {"max_consolidation_sessions": 5}, "max_consolidation"),
    ],
)
def test_strategy_settings_are_validated(
    strategy: str, changes: dict[str, object], message: str
) -> None:
    raw = _dump()
    section = raw["strategies"]
    assert isinstance(section, dict)
    section[strategy] = {**section[strategy], **changes}
    with pytest.raises(ValidationError, match=message):
        RiskConfig.model_validate(raw)
