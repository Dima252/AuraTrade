"""Runtime settings (README §14) and the typed, hashed `config/risk.yaml`.

Settings come from the environment (`.env` locally, SSM-backed environment variables in
AWS). Every trading threshold lives in `risk.yaml`, never in code or the environment.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RISK_CONFIG = REPO_ROOT / "config" / "risk.yaml"
DEFAULT_MODEL = "claude-opus-5-5"


class TradingMode(StrEnum):
    DRY_RUN = "dry_run"
    PAPER = "paper"
    LIVE = "live"


class Effort(StrEnum):
    """`output_config.effort` levels for claude-opus-5-5 (its API default is medium)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class Settings(BaseSettings):
    """Process settings. Construct through `load_settings()`."""

    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", frozen=True, case_sensitive=False
    )

    trading_mode: TradingMode = TradingMode.DRY_RUN
    environment: Literal["dev", "paper"] = "dev"

    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    fred_api_key: SecretStr | None = None
    finnhub_api_key: SecretStr | None = None
    edgar_user_agent: str | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    database_url: SecretStr | None = None

    analyst_model: str = DEFAULT_MODEL
    pm_model: str = DEFAULT_MODEL
    eod_model: str = DEFAULT_MODEL
    weekly_model: str = DEFAULT_MODEL
    # The Analyst's effort (medium vs high) is chosen by test in task 4.5 (README §9).
    analyst_effort: Effort = Effort.MEDIUM
    pm_effort: Effort = Effort.HIGH
    eod_effort: Effort = Effort.HIGH
    weekly_effort: Effort = Effort.HIGH
    analyst_max_candidates: PositiveInt = 40

    storage_backend: Literal["local", "s3"] = "local"
    storage_local_dir: Path = REPO_ROOT / "var" / "storage"
    storage_s3_bucket: str | None = None
    # Lambda function name for a chained job; `{env}` and `{job}` are substituted.
    job_function_template: str = "auratrade-{env}-{job}"

    risk_config_path: Path = DEFAULT_RISK_CONFIG

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _normalise_mode(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.trading_mode is TradingMode.LIVE:
            # Live needs an explicit flag and separate credentials (README §14); both
            # arrive with the Phase 8 live stack. Until then `live` is refused outright.
            raise ValueError("TRADING_MODE=live is refused: live trading starts only in Phase 8")
        if self.storage_backend == "s3" and not self.storage_s3_bucket:
            raise ValueError("STORAGE_S3_BUCKET is required when STORAGE_BACKEND=s3")
        return self

    def secret_values(self) -> list[str]:
        """Every configured secret, for log and alert redaction."""
        secrets = (
            self.alpaca_api_key,
            self.alpaca_secret_key,
            self.anthropic_api_key,
            self.fred_api_key,
            self.finnhub_api_key,
            self.telegram_bot_token,
            self.database_url,
        )
        return [s.get_secret_value() for s in secrets if s is not None and s.get_secret_value()]


def load_settings(env_file: Path | None = REPO_ROOT / ".env") -> Settings:
    """Settings from the environment, plus `env_file` when it exists (local runs).

    Real environment variables win over the file, as in pydantic-settings.
    """
    use_file = env_file if env_file is not None and env_file.is_file() else None
    return Settings(_env_file=use_file)  # type: ignore[call-arg]


# --- risk.yaml ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
Positive = Annotated[float, Field(gt=0.0)]


class Regime(StrEnum):
    CORRECTION = "CORRECTION"
    EARLY_UPTREND = "EARLY_UPTREND"
    UNDER_PRESSURE = "UNDER_PRESSURE"
    CONFIRMED_UPTREND = "CONFIRMED_UPTREND"


class Strategy(StrEnum):
    VCP_BREAKOUT = "VCP_BREAKOUT"
    MOMENTUM_BREAKOUT = "MOMENTUM_BREAKOUT"
    EPISODIC_PIVOT = "EPISODIC_PIVOT"
    LEADER_PULLBACK = "LEADER_PULLBACK"
    EMERGING_LEADER = "EMERGING_LEADER"


class FundedStrategy(_Strict):
    strategy: Strategy
    min_conviction: Annotated[int, Field(ge=1, le=5)] | None = None


class RegimeLimits(_Strict):
    """One row of README §6. Risk and exposure are fractions of equity."""

    strategies: tuple[FundedStrategy, ...]
    base_risk: Fraction
    max_exposure: Fraction
    max_positions: Annotated[int, Field(ge=0)]
    max_positions_large_account: Annotated[int, Field(ge=0)] | None = None


class EarlyUptrendStep(_Strict):
    """README §6: `EARLY_UPTREND` limits step up after N sessions, on score ≥ +1 only."""

    starter_sessions: PositiveInt
    min_score_to_step_up: int
    stepped_base_risk: Fraction
    stepped_max_exposure: Fraction


class RegimesConfig(_Strict):
    CONFIRMED_UPTREND: RegimeLimits
    UNDER_PRESSURE: RegimeLimits
    EARLY_UPTREND: RegimeLimits
    CORRECTION: RegimeLimits
    early_uptrend_step: EarlyUptrendStep
    large_account_equity: Positive


class GuardrailsConfig(_Strict):
    """Numeric defaults from README §8; behaviour lives in `auratrade.guardrails`."""

    max_risk_per_trade: Fraction
    max_position_size: Fraction
    max_heat: Fraction
    heat_min_room_of_planned: Fraction
    max_per_cluster: PositiveInt
    cluster_correlation: Fraction
    cluster_correlation_sessions: PositiveInt
    pivot_tolerance: Fraction
    min_stop_below_pivot_atr: Positive
    max_chase_above_pivot: Fraction
    max_above_50d: Positive
    late_stage_base_count: PositiveInt
    late_stage_risk_factor: Fraction
    max_base_count: PositiveInt
    earnings_blackout_sessions: PositiveInt
    earnings_estimate_sessions: PositiveInt
    earnings_estimate_window_sessions: PositiveInt
    tranche2_max_sessions: PositiveInt


class HaltsConfig(_Strict):
    daily_loss: Fraction
    weekly_loss: Fraction
    drawdown: Fraction


class MultiplierConfig(_Strict):
    """Progressive-exposure multiplier (README §8)."""

    win_r: Positive
    loss_r: Positive
    deep_drawdown: Fraction
    deep_drawdown_multiplier: Positive
    drawdown: Fraction
    drawdown_multiplier: Positive
    tier_exit_wins: PositiveInt
    losing_streak: PositiveInt
    losing_streak_exit_wins: PositiveInt
    losing_streak_multiplier: Positive
    prove_it_wins: PositiveInt
    prove_it_multiplier: Positive
    unproven_regime_trades: PositiveInt
    unproven_regime_multiplier: Positive
    trading_well_high_sessions: PositiveInt
    trading_well_trades: PositiveInt
    trading_well_multiplier: Positive


class UpDayCluster(_Strict):
    up_days: PositiveInt
    of_sessions: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.up_days > self.of_sessions:
            raise ValueError("up_days cannot exceed of_sessions")
        return self


class ClimaxConfig(_Strict):
    """Climax-sale signals and actions (README §7.3); see `indicators.climax`."""

    up_day_clusters: tuple[UpDayCluster, ...]
    min_above_long_average: Positive
    fast_gain_min: Positive
    fast_gain_window: PositiveInt
    fast_gain_min_move_sessions: Annotated[int, Field(ge=0)]
    sell_half_signals: PositiveInt
    sell_all_signals: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.sell_all_signals < self.sell_half_signals:
            raise ValueError("sell_all_signals must be >= sell_half_signals")
        return self


class DataConfig(_Strict):
    min_coverage: Fraction
    macro_stale_sessions: PositiveInt


class JobsConfig(_Strict):
    chain_wait_minutes: PositiveInt
    duration_alert_minutes: PositiveInt
    # A RUNNING heartbeat older than this is a dead run (Lambda's limit is 15 minutes).
    stale_running_minutes: PositiveInt


class RiskConfig(_Strict):
    regimes: RegimesConfig
    guardrails: GuardrailsConfig
    halts: HaltsConfig
    multiplier: MultiplierConfig
    climax: ClimaxConfig
    data: DataConfig
    jobs: JobsConfig
    bases: BasesConfig

    @cached_property
    def config_hash(self) -> str:
        """SHA-256 of the validated values, so comments and formatting don't change it."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def short_hash(self) -> str:
        return self.config_hash[:12]


def load_risk_config(path: Path = DEFAULT_RISK_CONFIG) -> RiskConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping")
    return RiskConfig.model_validate(raw)


class BasesConfig(_Strict):
    """Base detection and IBD counting (README §5.3); see `indicators.bases.BaseRules`."""

    min_sessions: PositiveInt
    max_depth: Annotated[float, Field(gt=0.0, lt=1.0)]
    swing_reversal: Annotated[float, Field(gt=0.0, lt=1.0)]
    pivot_min_position: Annotated[float, Field(gt=0.0, lt=1.0)]
    new_stage_advance: Positive


RiskConfig.model_rebuild()
