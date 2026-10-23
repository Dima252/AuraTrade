"""One session of the regime classifier: pillars -> score -> state machine, plus the crisis
override (README §5.2). Pure: the job and the backtest supply the inputs.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from auratrade.config import Regime, RegimeConfig
from auratrade.regime.pillars import (
    Crisis,
    IndexTrend,
    PillarScore,
    TrackedBreakout,
    breadth_pillar,
    crisis_override,
    distribution_pillar,
    index_trend_pillar,
    leadership_pillar,
    volatility_pillar,
)
from auratrade.regime.state import IndexBars, IndexFollowThrough, RegimeState, advance, track


@dataclass(frozen=True, slots=True)
class SessionInputs:
    session: date
    index_bars: IndexBars
    """Per index (``RegimeConfig.indexes``): prior and current bar, volume normalized."""
    index_trend: Mapping[str, IndexTrend]
    fraction_above: float | None
    net_new_highs: float | None
    vix: float | None
    vix3m: float | None
    macro_stale_sessions: int | None
    """Sessions since the latest VIX / VIX3M value (the older of the two) on or before this
    session, from either source; ``None`` if there is none."""
    breakouts: Sequence[TrackedBreakout] = ()
    """The ungated breakout tracker (task 3.7); empty until it exists, which scores 0."""
    expiration_session: bool = False
    prior_vix_ratios: Sequence[float | None] = field(default_factory=tuple)
    """VIX/VIX3M at the preceding sessions, newest last (for the crisis override)."""

    @property
    def vix_ratio(self) -> float | None:
        if self.vix is None or self.vix3m is None:
            return None
        return self.vix / self.vix3m


@dataclass(frozen=True, slots=True)
class Pillars:
    index_trend: PillarScore
    breadth: PillarScore
    volatility: PillarScore
    distribution: PillarScore
    leadership: PillarScore

    @property
    def score(self) -> int:
        return (
            self.index_trend + self.breadth + self.volatility + self.distribution + self.leadership
        )


@dataclass(frozen=True, slots=True)
class RegimeResult:
    session: date
    previous: Regime | None
    state: RegimeState
    pillars: Pillars
    crisis: Crisis
    follow_through: IndexFollowThrough | None
    distribution_counts: Mapping[str, int]
    features: Mapping[str, Any]

    @property
    def regime(self) -> Regime:
        return self.state.regime

    @property
    def score(self) -> int:
        return self.pillars.score

    @property
    def changed(self) -> bool:
        return self.previous is not self.state.regime


def classify(
    state: RegimeState | None,
    inputs: SessionInputs,
    cfg: RegimeConfig,
    *,
    macro_stale_limit: int,
) -> RegimeResult:
    """Classify ``inputs.session``, given the state after the previous session (``None`` starts
    the machine). ``macro_stale_limit`` is ``data.macro_stale_sessions``."""
    if set(inputs.index_bars) != set(cfg.indexes):
        raise ValueError(f"index bars must cover exactly {list(cfg.indexes)}")
    tracking = track(
        state, inputs.index_bars, expiration_session=inputs.expiration_session, cfg=cfg
    )
    counts = tracking.distribution_count()
    stale = inputs.macro_stale_sessions is None or inputs.macro_stale_sessions > macro_stale_limit
    leadership = leadership_pillar(inputs.breakouts, cfg.leadership)
    pillars = Pillars(
        index_trend=index_trend_pillar(inputs.index_trend),
        breadth=breadth_pillar(inputs.fraction_above, inputs.net_new_highs, cfg.breadth),
        volatility=volatility_pillar(inputs.vix, inputs.vix3m, stale=stale, cfg=cfg.volatility),
        distribution=distribution_pillar(max(counts.values(), default=0), cfg.distribution),
        leadership=leadership.score,
    )
    new_state = advance(
        state,
        tracking,
        inputs.index_bars,
        pillars.score,
        cfg,
        index_trend_down=pillars.index_trend == -1,
    )

    crisis_prev, crisis_bar = inputs.index_bars[cfg.crisis.index]
    crisis = crisis_override(
        [*inputs.prior_vix_ratios, inputs.vix_ratio],
        macro_stale=stale,
        index_return=crisis_bar.close / crisis_prev.close - 1,
        cfg=cfg.crisis,
    )
    ftd = tracking.follow_through
    features: dict[str, Any] = {
        "fraction_above": inputs.fraction_above,
        "net_new_highs": inputs.net_new_highs,
        "vix": inputs.vix,
        "vix3m": inputs.vix3m,
        "vix_ratio": inputs.vix_ratio,
        "macro_stale_sessions": inputs.macro_stale_sessions,
        "distribution_days": dict(counts),
        "distribution_today": dict(tracking.distribution_today),
        "expiration_session": inputs.expiration_session,
        "leadership_breakouts": leadership.breakouts,
        "leadership_share": leadership.share_above,
        "crisis_reasons": list(crisis.reasons),
        "rally_day": {i: r.day for i, r in tracking.rally.items()},
        "follow_through": None
        if ftd is None
        else {"index": ftd.index, "day": ftd.ftd.day, "gain": ftd.ftd.gain, "weak": ftd.ftd.weak},
    }
    return RegimeResult(
        session=inputs.session,
        previous=None if state is None else state.regime,
        state=new_state,
        pillars=pillars,
        crisis=crisis,
        follow_through=ftd,
        distribution_counts=counts,
        features=features,
    )
