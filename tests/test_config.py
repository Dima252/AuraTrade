from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from auratrade.config import (
    DEFAULT_MODEL,
    DEFAULT_RISK_CONFIG,
    AverageKind,
    Effort,
    RiskConfig,
    Settings,
    TradingMode,
    load_risk_config,
    load_settings,
)

ENV_VARS = [
    "TRADING_MODE",
    "ENVIRONMENT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "PM_EFFORT",
    "STORAGE_BACKEND",
    "STORAGE_S3_BUCKET",
    "SITE_S3_BUCKET",
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
        assert s.pm_model == DEFAULT_MODEL == "claude-opus-5-5"
        assert s.pm_effort is Effort.HIGH
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
            make(pm_effort="extreme")

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
        assert risk.jobs.chain_wait_minutes == 30
        assert risk.jobs.duration_alert_minutes == 10
        assert risk.data.macro_stale_sessions == 2
        assert risk.ingest.bar_history_sessions == 504
        assert risk.execution.marketable_limit_slippage == 0.005
        assert risk.regime.indexes == ("SPY", "QQQ")
        assert risk.charts.daily_averages["21-day EMA"].kind is AverageKind.EMA

    def test_only_the_portfolios_sections_remain(self) -> None:
        # The setup system's sections went with its code in task 5.2 (#161).
        assert set(risk_dict()) == {
            "data",
            "ingest",
            "history",
            "jobs",
            "regime",
            "execution",
            "charts",
            "portfolio",
            "llm",
            "portfolio_manager",
        }

    def test_hash_ignores_comments_and_layout(self, tmp_path: Path) -> None:
        path = tmp_path / "risk.yaml"
        path.write_text("# a comment\n" + yaml.safe_dump(risk_dict(), indent=4), encoding="utf-8")
        assert load_risk_config(path).config_hash == load_risk_config().config_hash

    def test_hash_changes_with_a_value(self) -> None:
        raw = risk_dict()
        raw["jobs"] = {**raw["jobs"], "chain_wait_minutes": 31}  # type: ignore[dict-item]
        changed = RiskConfig.model_validate(raw)
        assert changed.config_hash != load_risk_config().config_hash
        assert len(changed.short_hash) == 12

    def test_unknown_key_is_rejected(self) -> None:
        raw = risk_dict()
        raw["jobs"] = {**raw["jobs"], "chian_wait_minutes": 30}  # type: ignore[dict-item]
        with pytest.raises(ValidationError, match="chian_wait_minutes"):
            RiskConfig.model_validate(raw)

    def test_fraction_out_of_range_is_rejected(self) -> None:
        raw = risk_dict()
        raw["portfolio"] = {**raw["portfolio"], "drift_band": 6}  # type: ignore[dict-item]
        with pytest.raises(ValidationError, match="drift_band"):
            RiskConfig.model_validate(raw)

    def test_config_is_immutable(self) -> None:
        risk = load_risk_config()
        with pytest.raises(ValidationError):
            risk.portfolio.drawdown_halt = 0.5  # type: ignore[misc]

    def test_non_mapping_file(self, tmp_path: Path) -> None:
        path = tmp_path / "risk.yaml"
        path.write_text("- just a list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_risk_config(path)


class TestDump:
    def test_dumped_config_round_trips(self) -> None:
        # Overrides dump only the keys given, so re-validating a dump changes nothing.
        risk = load_risk_config()
        again = RiskConfig.model_validate(risk.model_dump())
        assert again == risk
        assert again.config_hash == risk.config_hash


class TestPortfolioConfig:
    def test_preregistered_values(self) -> None:
        pf = load_risk_config().portfolio
        assert (pf.up_level, pf.down_level, pf.capped_level) == (1.3, 0.5, 1.0)
        assert (pf.min_level, pf.max_level, pf.max_shift) == (0.3, 1.5, 0.2)
        assert (pf.sma_months, pf.buffer) == (10, 0.02)
        assert (pf.vol_sessions, pf.vol_cap) == (21, 0.25)
        assert (pf.drawdown_halt, pf.stop_fraction, pf.drift_band) == (0.25, 0.3, 0.05)
        assert (pf.satellite_share, pf.max_etf_weight, pf.tax_rate) == (0.2, 0.1, 0.25)
        assert (pf.satellite_min_change, pf.satellite_tax_budget) == (0.02, 0.002)
        assert (pf.market, pf.leveraged, pf.cash, pf.leverage) == ("SPY", "SSO", "BIL", 2.0)
        assert pf.backtest.score_start_year == 1928
        assert (pf.backtest.window_years, pf.backtest.min_window_share) == (10, 0.6)
        assert pf.backtest.near_miss_cagr == 0.01

    @pytest.mark.parametrize(
        ("field", "value", "error"),
        [
            ("down_level", 1.4, "min_level <= down_level"),
            ("max_level", 2.5, "leverage"),
            ("satellite_list", ["QQQ", "QQQ"], "duplicate"),
            ("satellite_list", ["QQQ", "SSO"], "leveraged fund"),
            ("max_etf_weight", 0.5, "satellite_share"),
            ("stop_fraction", 0.0, "stop_fraction"),
            ("satellite_share", 1.0, "satellite_share < 1"),
            ("min_level", 0.1, "min_level >= it"),
            ("max_level", 1.9, "core above"),
        ],
    )
    def test_invalid(self, field: str, value: object, error: str) -> None:
        raw = risk_dict()
        raw["portfolio"][field] = value  # type: ignore[index]
        with pytest.raises(ValidationError, match=error):
            RiskConfig.model_validate(raw)
