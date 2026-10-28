"""Runtime settings (README §14) and the typed, hashed `config/risk.yaml`.

Settings come from the environment (`.env` locally, SSM-backed environment variables in
AWS). Every trading threshold lives in `risk.yaml`, never in code or the environment.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, time
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
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    database_url: SecretStr | None = None
    # The external dead-man's switch the watchdog pings (README §5.13). The URL is the
    # credential: anyone holding it can ping.
    healthchecks_ping_url: SecretStr | None = None

    # The Portfolio Manager's model and effort (README §9).
    pm_model: str = DEFAULT_MODEL
    pm_effort: Effort = Effort.HIGH

    storage_backend: Literal["local", "s3"] = "local"
    storage_local_dir: Path = REPO_ROOT / "var" / "storage"
    storage_s3_bucket: str | None = None
    # The public site's bucket: `simulate` publishes the dashboard's snapshot under `data/`
    # (README §5.8, task 6.2). Unset (locally), the snapshot stays in storage only.
    site_s3_bucket: str | None = None
    # Lambda function name for a chained job; `{env}` and `{job}` are substituted.
    job_function_template: str = "auratrade-{env}-{job}"
    # The EventBridge Scheduler group whose schedules the watchdog checks (AWS only).
    schedule_group: str | None = None

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
            self.telegram_bot_token,
            self.database_url,
            self.healthchecks_ping_url,
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


class DataConfig(_Strict):
    macro_stale_sessions: PositiveInt


class OutlierConfig(_Strict):
    """A high (low) is a bad tick when it lies beyond both the bar's open-close body and the
    neighbouring bars' highs (lows) by more than these fractions, or `adr_multiple` times the
    stock's average daily range over `adr_sessions` if larger (README §5.1, #126)."""

    min_beyond_body: Positive
    min_beyond_neighbours: Positive
    adr_multiple: Positive
    adr_sessions: PositiveInt


class MacroIngestConfig(_Strict):
    # The delayed-quote JSON is final once its last trade is this long after the close
    # (16:15 on a full session, README §5.1 / #117).
    quote_settle_minutes: PositiveInt
    history_mismatch_tolerance: Annotated[float, Field(ge=0.0)]
    fred_lookback_sessions: PositiveInt


class IngestConfig(_Strict):
    """The evening ingest (README §5.1, §10; task 1.5)."""

    # Rolling window of bars kept in Postgres (2 years); older bars live in Parquet.
    bar_history_sessions: PositiveInt
    # A re-fetched overlap bar whose close moved by more than this is a split (or another
    # vendor re-adjustment): the ticker's whole window is re-fetched.
    split_close_tolerance: Positive
    outliers: OutlierConfig
    # Corporate actions are fetched by process date over a window padded this many
    # sessions back and ahead of the session (spike 0.3b, docs/spikes/alpaca-data.md).
    corporate_actions_lookback_sessions: PositiveInt
    corporate_actions_lookahead_sessions: PositiveInt
    macro: MacroIngestConfig
    # The setup system's scanner and tracker rows older than this move to Parquet (README §10).
    archive_after_sessions: PositiveInt
    # Concurrent requests per vendor: the bar requests are bound by response time, and the
    # vendor's rate limiter still applies across the workers.
    http_workers: PositiveInt
    # Each step's transaction stays open across its vendor calls (a first fill fetches for a
    # few minutes), and Neon ends a transaction idle for 5. Raised for the ingest's own
    # transactions only, and still bounded so a lost client's locks are freed.
    transaction_idle_minutes: PositiveInt
    db_size_limit_mb: PositiveInt
    db_size_alert_fraction: Fraction


class HistoryConfig(_Strict):
    """The Parquet history's backfill for the portfolio's backtest (README §5.1; task 3.18)."""

    # Alpaca's SIP daily bars start on 2016-01-04 (spike 0.3a).
    start: date
    workers: PositiveInt


class JobsConfig(_Strict):
    chain_wait_minutes: PositiveInt
    duration_alert_minutes: PositiveInt
    # A RUNNING heartbeat older than this is a dead run (Lambda's limit is 15 minutes).
    stale_running_minutes: PositiveInt
    # How late a scheduled run's heartbeat may be before the watchdog alerts.
    watchdog_grace_minutes: PositiveInt


class RiskConfig(_Strict):
    data: DataConfig
    ingest: IngestConfig
    history: HistoryConfig
    jobs: JobsConfig
    regime: RegimeConfig
    execution: ExecutionConfig
    charts: ChartsConfig
    portfolio: PortfolioConfig
    llm: LlmConfig
    portfolio_manager: PortfolioManagerConfig

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


# --- risk.yaml: the regime (task 2.3) ------------------------------------------------------
# Defined after RiskConfig; `RiskConfig.model_rebuild()` below resolves the forward references.


class IndexTrendConfig(_Strict):
    short_sma: PositiveInt
    long_sma: PositiveInt
    slope_sessions: PositiveInt


class BreadthConfig(_Strict):
    sma: PositiveInt
    up_min_above: Fraction
    down_max_above: Fraction
    new_high_sessions: PositiveInt
    net_new_highs_sessions: PositiveInt


class VolatilityConfig(_Strict):
    up_max_vix: Positive
    up_max_ratio: Positive
    down_min_ratio: Positive
    down_min_vix: Positive


class DistributionConfig(_Strict):
    min_decline: Fraction
    window_sessions: PositiveInt
    up_max_days: Annotated[int, Field(ge=0)]
    down_min_days: PositiveInt
    exclude_expiration_sessions: bool


class LeadershipConfig(_Strict):
    result_sessions: PositiveInt
    window_sessions: PositiveInt
    min_breakouts: PositiveInt
    up_min_share: Fraction
    down_max_share: Fraction


class FollowThroughConfig(_Strict):
    min_gain: Positive
    first_day: PositiveInt
    last_strong_day: PositiveInt


class TransitionsConfig(_Strict):
    confirmed_min_score: int
    correction_max_score: int
    correction_sessions: PositiveInt
    debounce_score: int
    debounce_sessions: PositiveInt
    upgrade_sessions: PositiveInt
    early_uptrend_min_sessions: PositiveInt
    no_ftd_sessions: PositiveInt
    no_ftd_min_score: int
    early_uptrend_max_distribution: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.correction_max_score < self.debounce_score < self.confirmed_min_score:
            raise ValueError("need correction_max_score < debounce_score < confirmed_min_score")
        return self


class CrisisConfig(_Strict):
    vix_ratio_above: Positive
    vix_ratio_sessions: PositiveInt
    index: str
    index_drop: Fraction


class RegimeConfig(_Strict):
    """The regime classifier and state machine (README §5.2); see `auratrade.regime`."""

    indexes: tuple[str, ...] = Field(min_length=1)
    index_trend: IndexTrendConfig
    breadth: BreadthConfig
    volatility: VolatilityConfig
    distribution: DistributionConfig
    leadership: LeadershipConfig
    follow_through: FollowThroughConfig
    transitions: TransitionsConfig
    crisis: CrisisConfig
    replay_warmup_sessions: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.crisis.index not in self.indexes:
            raise ValueError("crisis.index must be one of indexes")
        if len(set(self.indexes)) != len(self.indexes):
            raise ValueError("indexes must be distinct")
        return self


class AverageKind(StrEnum):
    SMA = "SMA"
    EMA = "EMA"


class TrailSpec(_Strict):
    kind: AverageKind
    window: PositiveInt


# --- risk.yaml: the order layer (tasks 1.7, 5.1) --------------------------------------------


class DecisionPriceConfig(_Strict):
    """The decision price (README glossary, #120): the last IEX trade if at most
    ``trade_max_age_seconds`` old, else the quote midpoint if at most ``quote_max_age_seconds``
    old with a spread of at most ``max_spread`` of the midpoint."""

    trade_max_age_seconds: PositiveInt
    quote_max_age_seconds: PositiveInt
    max_spread: Fraction


class ExecutionConfig(_Strict):
    """The order layer (README §5.7); see ``auratrade.execution``."""

    decision_price: DecisionPriceConfig
    # A marketable sell limit sits this far below the decision price.
    marketable_limit_slippage: Fraction
    # How long a sale may work before it is re-priced (then sent at market), and how long the
    # market order gets before the run gives up and leaves it to the next one (README §5.7.2).
    sell_wait_seconds: PositiveInt
    market_order_wait_seconds: PositiveInt
    # A rebalance's buy works this long; what hasn't filled is cancelled (task 5.1).
    buy_wait_seconds: PositiveInt
    fill_poll_seconds: Positive
    # Attempts per order call that failed without an answer (a 5xx, a dropped connection),
    # each with the same client_order_id.
    call_attempts: PositiveInt
    # The single-writer lock (README §5.7.7): an order job alerts after waiting this long and
    # gives up (fails, so Lambda retries it) after the maximum.
    lock_wait_alert_seconds: PositiveInt
    lock_wait_max_seconds: PositiveInt
    # Server-side backstop: Postgres ends the lock's connection when it is idle this long, so a
    # frozen Lambda can't hold the lock for ever. Longer than any order section.
    lock_idle_timeout_minutes: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.lock_wait_max_seconds < self.lock_wait_alert_seconds:
            raise ValueError("lock_wait_max_seconds must be >= lock_wait_alert_seconds")
        return self


# --- risk.yaml: the charts and the portfolio ------------------------------------------------


class ChartsConfig(_Strict):
    """The Portfolio Manager's `get_chart` (README §5.4; task 3.4); see ``auratrade.charts``."""

    daily_sessions: PositiveInt
    daily_width: PositiveInt
    daily_height: PositiveInt
    daily_averages: dict[str, TrailSpec] = Field(min_length=1)
    weekly_weeks: PositiveInt
    weekly_width: PositiveInt
    weekly_height: PositiveInt
    weekly_averages: dict[str, TrailSpec] = Field(min_length=1)
    dpi: PositiveInt


class PortfolioBacktestConfig(_Strict):
    """The portfolio's backtest (`docs/backtests/portfolio-preregistration.md`, #156)."""

    start_equity: Positive
    score_start_year: PositiveInt
    # The synthesized funds (pre-registration §3.2): fees a year, and the 2x fund's financing.
    market_fee: Fraction
    leveraged_fee: Fraction
    cash_fee: Fraction
    financing_spread: Fraction
    market_slippage: Fraction
    leveraged_slippage: Fraction
    cash_slippage: Fraction
    # The gate's G3 and the near miss (§5.2-§5.3).
    window_years: PositiveInt
    min_window_share: Fraction
    near_miss_cagr: Fraction
    bootstrap_block_sessions: PositiveInt
    bootstrap_samples: PositiveInt
    confidence: Fraction
    seed: int
    timing_offsets: tuple[PositiveInt, ...] = Field(min_length=1)
    # The comparisons C1-C3 (§6), reported and never adopted.
    c1_sma_sessions: PositiveInt
    c2_up_level: Annotated[float, Field(ge=0.0)]
    c2_down_level: Annotated[float, Field(ge=0.0)]
    margin_spread: Fraction
    margin_maintenance: Fraction
    # The data checks (§4.4) and the blind check's stop conditions (§7).
    check_min_correlation: Annotated[float, Field(ge=-1.0, le=1.0)]
    check_max_cagr_gap: Fraction
    max_session_gap_days: PositiveInt
    blind_max_flips_per_year: Positive
    blind_max_halt_share: Fraction


class PortfolioConfig(_Strict):
    """The portfolio manager (README §5.4, §6-§8; decisions #154-#155)."""

    satellite_share: Fraction
    sma_months: PositiveInt
    buffer: Fraction
    # Exposure to the S&P, as a multiple of the core's equity.
    up_level: Positive
    down_level: Annotated[float, Field(ge=0.0)]
    capped_level: Positive
    min_level: Annotated[float, Field(ge=0.0)]
    max_level: Positive
    max_shift: Annotated[float, Field(ge=0.0)]
    vol_sessions: PositiveInt
    vol_cap: Positive
    drawdown_halt: Fraction
    stop_fraction: Annotated[float, Field(gt=0.0, lt=1.0)]
    drift_band: Fraction
    max_etf_weight: Fraction
    satellite_min_change: Fraction
    satellite_tax_budget: Fraction
    retry_sessions: Annotated[int, Field(ge=0)]
    market: str
    leveraged: str
    leverage: Annotated[float, Field(gt=1.0)]
    cash: str
    satellite_list: tuple[str, ...] = Field(min_length=1)
    tax_rate: Fraction
    backtest: PortfolioBacktestConfig

    @model_validator(mode="after")
    def _check(self) -> Self:
        levels = (self.min_level, self.down_level, self.capped_level, self.up_level, self.max_level)
        if list(levels) != sorted(levels):
            raise ValueError(
                "portfolio needs min_level <= down_level <= capped_level <= up_level <= max_level"
            )
        if self.max_level > self.leverage:
            raise ValueError("portfolio max_level can't exceed the leveraged fund's leverage")
        if len(set(self.satellite_list)) != len(self.satellite_list):
            raise ValueError("portfolio satellite_list has a duplicate")
        if self.leveraged in self.satellite_list:
            raise ValueError("portfolio satellite_list can't hold the leveraged fund")
        if self.max_etf_weight > self.satellite_share:
            raise ValueError("portfolio max_etf_weight can't exceed satellite_share")
        # The core is sized so the book, with the satellite at its neutral (the market fund),
        # is at the level (#163): its level must stay within [0, leverage].
        if self.satellite_share >= 1 or self.min_level < self.satellite_share:
            raise ValueError("portfolio needs satellite_share < 1 and min_level >= it")
        top = (self.max_level - self.satellite_share) / (1 - self.satellite_share)
        if top > self.leverage:
            raise ValueError("portfolio max_level needs a core above the fund's leverage")
        return self


class TokenPrices(_Strict):
    """One model's list prices, US$ per million tokens."""

    input: Positive
    output: Positive
    cache_write: Positive
    cache_read: Positive


class LlmConfig(_Strict):
    """The Claude client (README §9; task 4.1); see ``auratrade.llm.client``."""

    max_tokens: PositiveInt
    max_turns: PositiveInt
    request_timeout_seconds: Positive
    max_retries: Annotated[int, Field(ge=0)]
    fallbacks: bool
    cost_alert_usd: Positive
    # Keyed by model id. A model missing here (a server-side fallback's) is priced at the
    # dearest listed, so a cost is never understated.
    prices: dict[str, TokenPrices] = Field(min_length=1)


class PmScorecardConfig(_Strict):
    """The scorecard of the Portfolio Manager's decisions (README §5.5; task 4.4)."""

    # An expectation within this of 0 calls the fund level with the market fund; it is right
    # when the outcome is within it too.
    flat_band: Positive
    # The size is right when the outcome is within this factor of the expectation, either way.
    size_factor: Annotated[float, Field(gt=1.0)]
    bootstrap_months: PositiveInt
    """The headline's interval starts once this many months are complete."""
    block_months: PositiveInt
    samples: PositiveInt
    confidence: Annotated[float, Field(gt=0.0, lt=1.0)]
    seed: int


class PortfolioManagerConfig(_Strict):
    """The Portfolio Manager's facts and tools (README §5.4; task 4.2)."""

    # Each satellite ETF's returns, by label, over this many sessions.
    return_sessions: dict[str, PositiveInt] = Field(min_length=1)
    volatility_sessions: Annotated[int, Field(ge=2)]
    sma_sessions: PositiveInt
    correlation_sessions: Annotated[int, Field(ge=2)]
    headlines: PositiveInt
    news_items: PositiveInt
    history_months: PositiveInt
    history_sessions: PositiveInt
    dry_run_equity: Positive
    deadline: time
    """US/Eastern on E."""
    headline_sessions: PositiveInt
    scorecard: PmScorecardConfig


RiskConfig.model_rebuild()
