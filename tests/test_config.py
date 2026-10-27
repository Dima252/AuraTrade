from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from auratrade.config import (
    DEFAULT_MODEL,
    DEFAULT_RISK_CONFIG,
    AverageKind,
    Effort,
    GateKind,
    Regime,
    RiskConfig,
    Settings,
    Strategy,
    TradingMode,
    TrailMA,
    load_risk_config,
    load_settings,
)
from auratrade.indicators.bars import Bar
from auratrade.indicators.climax import climax_signals

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


class TestClimaxConfig:
    def test_readme_values(self) -> None:
        climax = load_risk_config().climax
        assert [(c.up_days, c.of_sessions) for c in climax.up_day_clusters] == [(7, 8), (8, 10)]
        assert climax.min_above_long_average == 0.70
        assert (climax.fast_gain_min, climax.fast_gain_window) == (0.25, 15)
        assert climax.fast_gain_min_move_sessions == 15
        assert (climax.sell_half_signals, climax.sell_all_signals) == (2, 3)
        assert climax.exhaustion_min_gap == 0.01  # user decision, task 3.5a

    def test_keys_match_the_indicator(self) -> None:
        climax = load_risk_config().climax
        bars = [Bar(open=10, high=10, low=10, close=10, volume=1000) for _ in range(30)]
        signals = climax_signals(
            bars,
            at=29,
            move_start=0,
            long_average=None,
            exhaustion_min_gap=climax.exhaustion_min_gap,
            up_day_clusters=[(c.up_days, c.of_sessions) for c in climax.up_day_clusters],
            min_above_long_average=climax.min_above_long_average,
            fast_gain_min=climax.fast_gain_min,
            fast_gain_window=climax.fast_gain_window,
            fast_gain_min_move_sessions=climax.fast_gain_min_move_sessions,
        )
        assert signals.count == 0

    @pytest.mark.parametrize(
        ("field", "value", "error"),
        [
            ("up_day_clusters", [{"up_days": 9, "of_sessions": 8}], "cannot exceed"),
            ("sell_all_signals", 1, "sell_all_signals"),
            ("fast_gain_min_move_sessions", -1, "fast_gain_min_move_sessions"),
        ],
    )
    def test_invalid(self, field: str, value: object, error: str) -> None:
        raw = risk_dict()
        raw["climax"] = {**raw["climax"], field: value}  # type: ignore[dict-item]
        with pytest.raises(ValidationError, match=error):
            RiskConfig.model_validate(raw)


class TestTrendTemplateConfig:
    def test_readme_values(self) -> None:
        tt = load_risk_config().trend_template
        assert (tt.short_sma, tt.mid_sma, tt.long_sma) == (50, 150, 200)
        assert tt.long_sma_rising_sessions == 22
        assert tt.range_sessions == 252
        assert (tt.min_above_low, tt.max_below_high, tt.min_rs_rating) == (0.30, 0.25, 70)
        assert [(w.lookback, w.weight) for w in tt.rs_weights] == [
            (63, 0.4),
            (126, 0.2),
            (189, 0.2),
            (252, 0.2),
        ]
        assert tt.required_rules[Strategy.VCP_BREAKOUT] == frozenset(range(1, 9))
        assert tt.required_rules[Strategy.EMERGING_LEADER] == {1, 5, 6, 7, 8}
        assert tt.required_rules[Strategy.EPISODIC_PIVOT] == frozenset()

    @pytest.mark.parametrize(
        ("change", "error"),
        [
            ({"mid_sma": 40}, "short_sma < mid_sma < long_sma"),
            ({"required_rules": {"VCP_BREAKOUT": [1]}}, "missing"),
            ({"required_rules": {s.value: [9] for s in Strategy}}, "less than or equal to 8"),
            ({"rs_weights": []}, "at least 1"),
        ],
    )
    def test_invalid_values(self, change: dict[str, object], error: str) -> None:
        raw = risk_dict()
        raw["trend_template"] = {**raw["trend_template"], **change}  # type: ignore[dict-item]
        with pytest.raises(ValidationError, match=error):
            RiskConfig.model_validate(raw)


class TestPlansConfig:
    def test_readme_values(self) -> None:
        plans = load_risk_config().plans
        assert {t: (s.kind, s.window) for t, s in plans.trails.items()} == {
            TrailMA.TEN_DAY: (AverageKind.SMA, 10),
            TrailMA.EMA_21: (AverageKind.EMA, 21),
            TrailMA.SMA_50: (AverageKind.SMA, 50),
        }
        menus = plans.partial_menus
        assert (menus.KULLAMAGI.after_sessions.min, menus.KULLAMAGI.after_sessions.max) == (3, 5)
        assert menus.KULLAMAGI.min_r == 1.5
        assert (menus.ONEIL.fraction.min, menus.ONEIL.gain.min, menus.ONEIL.gain.max) == (
            0.25,
            0.20,
            0.25,
        )
        assert (menus.MINERVINI.r_multiple.min, menus.MINERVINI.r_multiple.max) == (2.0, 3.0)
        assert (plans.breakeven_at_r, plans.round_trip_gain) == (3.0, 0.10)
        hold = plans.eight_week_hold
        assert (hold.min_gain, hold.within_sessions, hold.hold_sessions) == (0.20, 15, 40)
        assert plans.heavy_volume_reversal.min_volume_ratio == 1.5
        assert plans.early_sessions == 20
        stop = plans.time_stop
        assert (stop.min_r, stop.halve_session, stop.exit_session) == (1.0, 10, 20)
        assert plans.climax_sell_fraction == plans.late_stage_sale.sell_fraction == 0.5
        assert plans.late_stage_sale.min_base_count == 4
        cushion = plans.earnings_cushion
        assert (cushion.exit_below_r, cushion.hold_full_from_r, cushion.trim_fraction) == (
            1.0,
            3.0,
            0.5,
        )
        breakout = plans.confirmation_gates[GateKind.BREAKOUT]
        assert (breakout.max_above_level, breakout.min_volume_ratio) == (0.05, 1.4)
        assert breakout.rs_line_near_high
        assert not breakout.above_tested_ma
        pullback = plans.confirmation_gates[GateKind.PULLBACK]
        assert pullback.min_volume_ratio == 1.0
        assert pullback.above_tested_ma
        assert pullback.rs_line_not_at_low

    @pytest.mark.parametrize(
        ("path", "value", "error"),
        [
            (
                ("entry",),
                {
                    "VCP_BREAKOUT": {
                        "trigger": "PIVOT",
                        "stop": "STRUCTURAL",
                        "tranches": 2,
                        "gate": "BREAKOUT",
                    }
                },
                "entry is missing",
            ),
            (("entry", "MOMENTUM_BREAKOUT", "gate"), "BREAKOUT", "exactly when"),
            (("entry", "VCP_BREAKOUT", "gate"), None, "exactly when"),
            (("exit_sets", "MINERVINI", "partial", "r_multiple"), 4.0, "outside"),
            (("exit_sets", "ONEIL", "loose_rule"), True, "loose_rule"),
            (
                ("default_plans", "VCP_BREAKOUT", "overrides"),
                {"partial": {"kind": "ONEIL", "fraction": 0.9, "gain": 0.2}},
                "fraction",
            ),
            (("partial_menus", "ONEIL", "gain"), {"min": 0.3, "max": 0.2}, "min must be"),
            (("time_stop", "exit_session"), 10, "exit_session"),
            (("eight_week_hold", "hold_sessions"), 10, "hold_sessions"),
            (("earnings_cushion", "exit_below_r"), 4.0, "exit_below_r"),
            (("trails",), {"TEN_DAY": {"kind": "SMA", "window": 10}}, "trails is missing"),
        ],
    )
    def test_invalid(self, path: tuple[str, ...], value: object, error: str) -> None:
        raw = risk_dict()
        node = raw["plans"]
        for key in path[:-1]:
            node = node[key]  # type: ignore[index]
        node[path[-1]] = value  # type: ignore[index]
        with pytest.raises(ValidationError, match=error):
            RiskConfig.model_validate(raw)

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
