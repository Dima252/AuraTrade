"""Shared builders for the regime tests: a config with short windows and synthetic histories."""

from datetime import date, timedelta

from auratrade.config import DEFAULT_RISK_CONFIG, RegimeConfig, RiskConfig, load_risk_config
from auratrade.regime.breadth import TickerHistory

RISK = load_risk_config(DEFAULT_RISK_CONFIG)


def small_risk() -> RiskConfig:
    """The production config with windows short enough for hand-built histories."""
    cfg = RISK.regime
    regime = cfg.model_copy(
        update={
            "index_trend": cfg.index_trend.model_copy(
                update={"short_sma": 3, "long_sma": 5, "slope_sessions": 2}
            ),
            "breadth": cfg.breadth.model_copy(
                update={
                    "sma": 3,
                    "new_high_sessions": 5,
                    "net_new_highs_sessions": 3,
                }
            ),
            "replay_warmup_sessions": 8,
        }
    )
    universe = RISK.universe.model_copy(
        update={"min_history_sessions": 5, "dollar_volume_sessions": 3, "min_dollar_volume": 1000.0}
    )
    return RISK.model_copy(update={"regime": regime, "universe": universe})


def weekdays(start: date, n: int) -> list[date]:
    out, day = [], start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def history(
    dates: list[date],
    closes: list[float],
    *,
    volume: float = 1000.0,
    raw_scale: float = 1.0,
    spread: float = 0.0,
) -> TickerHistory:
    return TickerHistory(
        dates=dates,
        highs=[c * (1 + spread) for c in closes],
        lows=[c * (1 - spread) for c in closes],
        closes=closes,
        raw_closes=[c * raw_scale for c in closes],
        volumes=[volume] * len(closes),
    )


def ibd_regime(cfg: RegimeConfig) -> RegimeConfig:
    """``cfg`` at IBD's published thresholds (README §5.2 before #135): a CORRECTION score
    <= -1, CONFIRMED_UPTREND from +3, distribution -1 from 6 days. The state machine's tests
    use these, so they test the mechanism rather than the values the backtest tunes."""
    return cfg.model_copy(
        update={
            "transitions": cfg.transitions.model_copy(
                update={
                    "confirmed_min_score": 3,
                    "correction_max_score": -1,
                    "debounce_score": 2,
                    "no_ftd_min_score": 3,
                }
            ),
            "distribution": cfg.distribution.model_copy(update={"down_min_days": 6}),
        }
    )
