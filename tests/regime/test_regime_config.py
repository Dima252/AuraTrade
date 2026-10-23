from typing import Any

import pytest
from pydantic import ValidationError

from auratrade.config import DEFAULT_RISK_CONFIG, RegimeConfig, load_risk_config

RAW: dict[str, Any] = load_risk_config(DEFAULT_RISK_CONFIG).regime.model_dump(mode="json")


def with_changes(section: str | None, **changes: Any) -> dict[str, Any]:
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in RAW.items()}
    target = raw if section is None else raw[section]
    target.update(changes)
    return raw


def test_the_shipped_values_match_the_readme() -> None:
    cfg = RegimeConfig.model_validate(RAW)
    assert cfg.indexes == ("SPY", "QQQ")
    # The provisional 4-pillar thresholds from the 2.4 backtest (#135).
    t = cfg.transitions
    assert (t.confirmed_min_score, t.debounce_score, t.correction_max_score) == (2, 1, -2)
    assert (t.no_ftd_min_score, t.early_uptrend_max_distribution) == (2, 6)
    assert (cfg.distribution.up_max_days, cfg.distribution.down_min_days) == (3, 8)
    assert cfg.index_trend.slope_sessions == 10
    assert not cfg.distribution.exclude_expiration_sessions
    assert cfg.replay_warmup_sessions == 252


@pytest.mark.parametrize(
    ("section", "changes", "message"),
    [
        ("transitions", {"debounce_score": 3}, "correction_max_score < debounce_score"),
        ("transitions", {"correction_max_score": 2}, "correction_max_score < debounce_score"),
        ("crisis", {"index": "IWM"}, "crisis.index"),
        (None, {"indexes": ["SPY", "SPY"]}, "distinct"),
        (None, {"surprise": 1}, "Extra inputs"),
    ],
)
def test_invalid_values_are_rejected(
    section: str | None, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        RegimeConfig.model_validate(with_changes(section, **changes))
