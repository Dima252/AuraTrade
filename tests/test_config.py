from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from auratrade.config import (
    DEFAULT_MODEL,
    DEFAULT_RISK_CONFIG,
    Effort,
    Regime,
    RiskConfig,
    Settings,
    Strategy,
    TradingMode,
    load_risk_config,
    load_settings,
)

ENV_VARS = [
    "TRADING_MODE",
    "ENVIRONMENT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ANALYST_EFFORT",
    "STORAGE_BACKEND",
    "STORAGE_S3_BUCKET",
    "DATABASE_URL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg, arg-type]


class TestSettings:
    def test_defaults_are_safe(self) -> None:
        s = make()
        assert s.trading_mode is TradingMode.DRY_RUN
        assert s.environment == "dev"
        assert s.analyst_model == s.pm_model == DEFAULT_MODEL == "claude-opus-5-5"
        assert s.pm_effort is Effort.HIGH
        assert s.analyst_max_candidates == 40
        assert s.storage_backend == "local"
        assert s.risk_config_path == DEFAULT_RISK_CONFIG

    @pytest.mark.parametrize("value", ["paper", "PAPER", " Paper "])
    def test_paper_mode_from_env(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("TRADING_MODE", value)
        assert make().trading_mode is TradingMode.PAPER

    @pytest.mark.parametrize("value", ["live", "LIVE"])
    def test_live_is_refused(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("TRADING_MODE", value)
        with pytest.raises(ValidationError, match="Phase 8"):
            make()

    def test_unknown_mode_is_an_error_not_a_fallback(self) -> None:
        with pytest.raises(ValidationError):
            make(trading_mode="real")

    def test_live_environment_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            make(environment="live")

    def test_s3_needs_a_bucket(self) -> None:
        with pytest.raises(ValidationError, match="STORAGE_S3_BUCKET"):
            make(storage_backend="s3")
        assert make(storage_backend="s3", storage_s3_bucket="b").storage_s3_bucket == "b"

    def test_invalid_effort(self) -> None:
        with pytest.raises(ValidationError):
            make(analyst_effort="extreme")

    def test_secret_values_skip_unset_and_empty(self) -> None:
        s = make(telegram_bot_token="tok-123", anthropic_api_key="", database_url="pg://x")
        assert sorted(s.secret_values()) == ["pg://x", "tok-123"]

    def test_secrets_are_hidden_in_repr(self) -> None:
        assert "tok-123" not in repr(make(telegram_bot_token="tok-123"))

    def test_env_file_and_env_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = tmp_path / ".env"
        env.write_text("TRADING_MODE=paper\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
        s = load_settings(env)
        assert s.trading_mode is TradingMode.PAPER
        assert s.telegram_chat_id == "42"
        monkeypatch.setenv("TRADING_MODE", "dry_run")
        assert load_settings(env).trading_mode is TradingMode.DRY_RUN

    def test_missing_env_file_is_ignored(self, tmp_path: Path) -> None:
        assert load_settings(tmp_path / "absent").trading_mode is TradingMode.DRY_RUN
        assert load_settings(None).trading_mode is TradingMode.DRY_RUN


def risk_dict() -> dict[str, object]:
    raw = yaml.safe_load(DEFAULT_RISK_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


class TestRiskConfig:
    def test_repo_config_loads_with_readme_values(self) -> None:
        risk = load_risk_config()
        confirmed = risk.regimes.CONFIRMED_UPTREND
        assert confirmed.base_risk == 0.01
        assert {f.strategy for f in confirmed.strategies} == {
            Strategy.VCP_BREAKOUT,
            Strategy.MOMENTUM_BREAKOUT,
            Strategy.EPISODIC_PIVOT,
            Strategy.LEADER_PULLBACK,
        }
        assert all(f.min_conviction == 4 for f in risk.regimes.UNDER_PRESSURE.strategies)
        assert risk.regimes.CORRECTION.strategies == ()
        assert risk.guardrails.max_heat == 0.06
        assert risk.guardrails.max_risk_per_trade == 0.015
        assert risk.halts.drawdown == 0.15
        assert risk.jobs.chain_wait_minutes == 30
        assert risk.jobs.duration_alert_minutes == 10
        assert risk.data.min_coverage == 0.9

    def test_every_regime_has_limits(self) -> None:
        risk = load_risk_config()
        for regime in Regime:
            assert getattr(risk.regimes, regime.value).max_exposure <= 1.0

    def test_hash_ignores_comments_and_layout(self, tmp_path: Path) -> None:
        path = tmp_path / "risk.yaml"
        path.write_text("# a comment\n" + yaml.safe_dump(risk_dict(), indent=4), encoding="utf-8")
        assert load_risk_config(path).config_hash == load_risk_config().config_hash

    def test_hash_changes_with_a_value(self) -> None:
        raw = risk_dict()
        raw["halts"] = {**raw["halts"], "daily_loss": 0.03}  # type: ignore[dict-item]
        changed = RiskConfig.model_validate(raw)
        assert changed.config_hash != load_risk_config().config_hash
        assert len(changed.short_hash) == 12

    def test_unknown_key_is_rejected(self) -> None:
        raw = risk_dict()
        raw["halts"] = {**raw["halts"], "dialy_loss": 0.03}  # type: ignore[dict-item]
        with pytest.raises(ValidationError, match="dialy_loss"):
            RiskConfig.model_validate(raw)

    def test_fraction_out_of_range_is_rejected(self) -> None:
        raw = risk_dict()
        raw["guardrails"] = {**raw["guardrails"], "max_heat": 6}  # type: ignore[dict-item]
        with pytest.raises(ValidationError, match="max_heat"):
            RiskConfig.model_validate(raw)

    def test_config_is_immutable(self) -> None:
        risk = load_risk_config()
        with pytest.raises(ValidationError):
            risk.halts.daily_loss = 0.5  # type: ignore[misc]

    def test_non_mapping_file(self, tmp_path: Path) -> None:
        path = tmp_path / "risk.yaml"
        path.write_text("- just a list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_risk_config(path)
