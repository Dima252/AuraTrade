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
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    SecretStr,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
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
    exhaustion_min_gap: Fraction
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
    trend_template: TrendTemplateConfig
    bases: BasesConfig
    regime: RegimeConfig
    plans: PlansConfig

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


# --- risk.yaml: scanner and plans (Phase 3) ------------------------------------------------
# Defined after RiskConfig so these sections stay appended; `RiskConfig.model_rebuild()` below
# resolves the forward references.


class RsWeight(_Strict):
    lookback: PositiveInt
    weight: Positive


TrendTemplateRule = Annotated[int, Field(ge=1, le=8)]


class TrendTemplateConfig(_Strict):
    """Minervini's 8-rule Trend Template (README §5.3); see `strategies.trend_template`."""

    short_sma: PositiveInt
    mid_sma: PositiveInt
    long_sma: PositiveInt
    long_sma_rising_sessions: PositiveInt
    range_sessions: PositiveInt
    min_above_low: Positive
    max_below_high: Fraction
    min_rs_rating: Annotated[float, Field(ge=0.0, le=100.0)]
    rs_weights: tuple[RsWeight, ...] = Field(min_length=1)
    required_rules: dict[Strategy, frozenset[TrendTemplateRule]]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.short_sma < self.mid_sma < self.long_sma:
            raise ValueError("need short_sma < mid_sma < long_sma")
        missing = set(Strategy) - self.required_rules.keys()
        if missing:
            raise ValueError(f"required_rules is missing {sorted(missing)}")
        return self


class BasesConfig(_Strict):
    """Base detection and IBD counting (README §5.3); see `indicators.bases.BaseRules`."""

    min_sessions: PositiveInt
    max_depth: Annotated[float, Field(gt=0.0, lt=1.0)]
    swing_reversal: Annotated[float, Field(gt=0.0, lt=1.0)]
    pivot_min_position: Annotated[float, Field(gt=0.0, lt=1.0)]
    new_stage_advance: Positive


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
    universe_security_types: tuple[str, ...] = Field(min_length=1)
    universe_min_price: Positive
    universe_min_dollar_volume: Positive
    universe_dollar_volume_sessions: PositiveInt
    universe_min_history_sessions: PositiveInt


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


class TrailMA(StrEnum):
    """Trailing moving averages, loosest first: a step tighter is 50 SMA -> 21 EMA -> 10-day."""

    SMA_50 = "SMA_50"
    EMA_21 = "EMA_21"
    TEN_DAY = "TEN_DAY"


class AverageKind(StrEnum):
    SMA = "SMA"
    EMA = "EMA"


class TrailSpec(_Strict):
    kind: AverageKind
    window: PositiveInt


class FloatRange(_Strict):
    min: float
    max: float

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.min > self.max:
            raise ValueError("min must be <= max")
        return self

    def contains(self, value: float) -> bool:
        return self.min <= value <= self.max


class IntRange(_Strict):
    min: int
    max: int

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.min > self.max:
            raise ValueError("min must be <= max")
        return self

    def contains(self, value: float) -> bool:
        return self.min <= value <= self.max


class KullamagiPartial(_Strict):
    """(a) Sell a fraction at the EOD review of session ``after_sessions`` if >= +min_r R."""

    kind: Literal["KULLAMAGI"] = "KULLAMAGI"
    fraction: Annotated[float, Field(gt=0.0, lt=1.0)]
    after_sessions: PositiveInt


class ONeilPartial(_Strict):
    """(b) Sell a fraction once the decision price is ``gain`` above the entry (0.2 = +20%)."""

    kind: Literal["ONEIL"] = "ONEIL"
    fraction: Annotated[float, Field(gt=0.0, lt=1.0)]
    gain: Positive


class MinerviniPartial(_Strict):
    """(c) Sell a fraction once the decision price is ``r_multiple`` R above the entry."""

    kind: Literal["MINERVINI"] = "MINERVINI"
    fraction: Annotated[float, Field(gt=0.0, lt=1.0)]
    r_multiple: Positive


type AnyPartial = KullamagiPartial | ONeilPartial | MinerviniPartial
StrengthPartial = Annotated[
    KullamagiPartial | ONeilPartial | MinerviniPartial, Field(discriminator="kind")
]


class KullamagiMenu(_Strict):
    fraction: FloatRange
    after_sessions: IntRange
    min_r: Positive


class ONeilMenu(_Strict):
    fraction: FloatRange
    gain: FloatRange


class MinerviniMenu(_Strict):
    fraction: FloatRange
    r_multiple: FloatRange


class PartialMenus(_Strict):
    """The bounded strength-partial menu (README §7.3)."""

    KULLAMAGI: KullamagiMenu
    ONEIL: ONeilMenu
    MINERVINI: MinerviniMenu

    def violations(self, partial: AnyPartial) -> list[str]:
        menu: BaseModel = getattr(self, partial.kind)
        out: list[str] = []
        for name in type(menu).model_fields:
            bounds = getattr(menu, name)
            if not isinstance(bounds, FloatRange | IntRange):
                continue
            value: float = getattr(partial, name)
            if not bounds.contains(value):
                out.append(
                    f"{partial.kind} partial {name}={value} is outside [{bounds.min}, {bounds.max}]"
                )
        return out


class ExitPlan(_Strict):
    """One position's exit-menu choices (README §7.3). Written at entry; tighten-only after."""

    partial: StrengthPartial | None = None
    breakeven_after_partial: bool = False
    breakeven_at_r: bool = False
    round_trip: bool = False
    trail: TrailMA
    eight_week_hold: bool = False
    climax_sales: bool = False
    heavy_volume_reversal: bool = False
    late_stage_sale: bool = False
    time_stop: bool = False
    # Safety rules: in every default set, tested on vs off by the baseline backtest.
    earnings_cushion: bool = True
    regime_tightening: bool = True


class ExitPlanOverrides(_Strict):
    """Per-strategy switches on top of an exit set; only the keys given are applied."""

    partial: StrengthPartial | None = None
    breakeven_after_partial: bool | None = None
    breakeven_at_r: bool | None = None
    round_trip: bool | None = None
    trail: TrailMA | None = None
    eight_week_hold: bool | None = None
    climax_sales: bool | None = None
    heavy_volume_reversal: bool | None = None
    late_stage_sale: bool | None = None
    time_stop: bool | None = None
    earnings_cushion: bool | None = None
    regime_tightening: bool | None = None

    @model_serializer(mode="wrap")
    def _only_given(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Dump only the keys given, so a dumped config re-validates to the same overrides
        # (an explicit `partial: null` means "no partial", not "unchanged").
        data: dict[str, Any] = handler(self)
        return {name: value for name, value in data.items() if name in self.model_fields_set}

    def apply(self, plan: ExitPlan) -> ExitPlan:
        changes = {name: getattr(self, name) for name in self.model_fields_set}
        return ExitPlan.model_validate({**plan.model_dump(), **changes})


class ExitSetName(StrEnum):
    KULLAMAGI = "KULLAMAGI"
    MINERVINI = "MINERVINI"
    ONEIL = "ONEIL"


class DefaultPlan(_Strict):
    exit_set: ExitSetName
    overrides: ExitPlanOverrides = ExitPlanOverrides()


class TriggerKind(StrEnum):
    """Entry triggers (README §7.1); computed by `execution.triggers` (3.5b)."""

    PIVOT = "PIVOT"
    """The pivot (a pullback's prior-session high), or the opening-range high if the range
    traded above it."""
    CONSOLIDATION_OR_RANGE_HIGH = "CONSOLIDATION_OR_RANGE_HIGH"
    """max(consolidation high, opening-range high)."""
    OPENING_RANGE_HIGH = "OPENING_RANGE_HIGH"


class StopKind(StrEnum):
    STRUCTURAL = "STRUCTURAL"
    LOW_OF_DAY = "LOW_OF_DAY"


class GateKind(StrEnum):
    BREAKOUT = "BREAKOUT"
    PULLBACK = "PULLBACK"


class EntryPolicy(_Strict):
    trigger: TriggerKind
    stop: StopKind
    tranches: Literal[1, 2]
    gate: GateKind | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.tranches == 2) != (self.gate is not None):
            raise ValueError("a confirmation gate is needed exactly when there are 2 tranches")
        return self


class GateConfig(_Strict):
    """Tranche-2 confirmation gate (README §7.1), on the decision price."""

    max_above_level: Fraction
    min_range_position: Fraction
    min_volume_ratio: Positive
    rs_line_near_high: bool
    above_tested_ma: bool
    rs_line_not_at_low: bool


class EightWeekHoldConfig(_Strict):
    min_gain: Positive
    within_sessions: PositiveInt
    hold_sessions: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.hold_sessions < self.within_sessions:
            raise ValueError("hold_sessions must be >= within_sessions")
        return self


class HeavyVolumeReversalConfig(_Strict):
    min_volume_ratio: Positive


class TimeStopConfig(_Strict):
    min_r: Positive
    halve_session: PositiveInt
    halve_fraction: Annotated[float, Field(gt=0.0, lt=1.0)]
    exit_session: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.exit_session <= self.halve_session:
            raise ValueError("exit_session must be after halve_session")
        return self


class LateStageSaleConfig(_Strict):
    min_base_count: PositiveInt
    sell_fraction: Annotated[float, Field(gt=0.0, lt=1.0)]


class EarningsCushionConfig(_Strict):
    exit_below_r: float
    hold_full_from_r: float
    trim_fraction: Annotated[float, Field(gt=0.0, lt=1.0)]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.exit_below_r > self.hold_full_from_r:
            raise ValueError("exit_below_r must be <= hold_full_from_r")
        return self


class PlansConfig(_Strict):
    """Position plans (README §7.1-7.4); behaviour lives in `auratrade.plans`."""

    trails: dict[TrailMA, TrailSpec]
    partial_menus: PartialMenus
    breakeven_at_r: Positive
    round_trip_gain: Positive
    eight_week_hold: EightWeekHoldConfig
    heavy_volume_reversal: HeavyVolumeReversalConfig
    early_sessions: PositiveInt
    time_stop: TimeStopConfig
    climax_sell_fraction: Annotated[float, Field(gt=0.0, lt=1.0)]
    late_stage_sale: LateStageSaleConfig
    earnings_cushion: EarningsCushionConfig
    confirmation_gates: dict[GateKind, GateConfig]
    entry: dict[Strategy, EntryPolicy]
    exit_sets: dict[ExitSetName, ExitPlan]
    default_plans: dict[Strategy, DefaultPlan]

    @model_validator(mode="after")
    def _check(self) -> Self:
        required: tuple[tuple[str, set[str], set[str]], ...] = (
            ("trails", set(self.trails), set(TrailMA)),
            ("confirmation_gates", set(self.confirmation_gates), set(GateKind)),
            ("entry", set(self.entry), set(Strategy)),
            ("exit_sets", set(self.exit_sets), set(ExitSetName)),
            ("default_plans", set(self.default_plans), set(Strategy)),
        )
        for name, keys, wanted in required:
            if missing := wanted - keys:
                raise ValueError(f"{name} is missing {sorted(missing)}")
        problems = [
            f"{strategy}: {problem}"
            for strategy in Strategy
            for problem in self.menu_violations(self.default_exit_plan(strategy))
        ]
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def default_exit_plan(self, strategy: Strategy) -> ExitPlan:
        default = self.default_plans[strategy]
        return default.overrides.apply(self.exit_sets[default.exit_set])

    def menu_violations(self, plan: ExitPlan) -> list[str]:
        return [] if plan.partial is None else self.partial_menus.violations(plan.partial)


RiskConfig.model_rebuild()
