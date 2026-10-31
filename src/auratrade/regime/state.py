"""The regime state machine (README §5.2; #27).

One step per session, at the close. The label computed at session ``t`` describes ``t + 1``.
Sessions are numbered by ``seq`` (0 = the first session stepped), so the state is
self-contained and can be stored as JSON with each ``market_regime`` row. The thresholds are
``regime.transitions`` in ``risk.yaml``.

- **Hysteresis:** downgrades to ``CORRECTION`` take ``correction_sessions`` in a row at
  ``correction_max_score`` or below (the index-trend pillar at -1 makes it immediate);
  ``CONFIRMED_UPTREND`` -> ``UNDER_PRESSURE`` is immediate below ``debounce_score`` and
  otherwise takes ``debounce_sessions`` in a row below ``confirmed_min_score``. Upgrades need
  ``upgrade_sessions`` in a row at ``confirmed_min_score`` or above, counted only while in the
  current state.
- **New uptrends need a follow-through day** on either index, or the ``no_ftd`` fallback:
  ``no_ftd_sessions`` in a row at ``no_ftd_min_score`` or above inside ``CORRECTION``.
- **``EARLY_UPTREND`` ends** only on a rally-low undercut by either index, or on 6 distribution
  days since entering it (higher index count); never on the score, and never by timeout. It
  upgrades only after ``early_uptrend_min_sessions`` labels in it.
- **On one session** a downgrade beats an upgrade, and an FTD beats the ``no_ftd`` fallback.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, assert_never

from auratrade.config import Regime, RegimeConfig
from auratrade.indicators.bars import Bar
from auratrade.indicators.distribution import (
    FollowThroughDay,
    RallyAttempt,
    distribution_days,
    start_rally_attempt,
    step_rally_attempt,
)

type IndexBars = Mapping[str, tuple[Bar, Bar]]
"""Per index: (prior session's bar, this session's bar), volume half-day normalized."""


@dataclass(frozen=True, slots=True)
class RegimeState:
    regime: Regime
    seq: int
    entered_seq: int
    no_ftd: bool = False
    upgrade_streak: int = 0
    debounce_streak: int = 0
    downgrade_streak: int = 0
    """Sessions in a row at <= ``correction_max_score`` in ``CONFIRMED_UPTREND`` /
    ``UNDER_PRESSURE``, carried from the one into the other."""
    fallback_streak: int = 0
    fallback_lows: Mapping[str, float] = field(default_factory=dict)
    """Per index, the lowest low over the current ``no_ftd`` streak."""
    rally: Mapping[str, RallyAttempt] = field(default_factory=dict)
    """``CORRECTION`` only: each index's rally attempt (``low_session`` is a ``seq``)."""
    rally_lows: Mapping[str, float] = field(default_factory=dict)
    """``EARLY_UPTREND`` only: each index's rally low; an undercut ends the uptrend."""
    distribution_days: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    """Per index, the ``seq`` of each distribution day still inside the window."""
    last_ftd_seq: int | None = None
    """Distribution counts restart after a follow-through day."""

    @property
    def sessions_in_state(self) -> int:
        """Labels in the current state so far, this session included."""
        return self.seq - self.entered_seq + 1

    def to_json(self) -> dict[str, Any]:
        return {
            "regime": self.regime.value,
            "seq": self.seq,
            "entered_seq": self.entered_seq,
            "no_ftd": self.no_ftd,
            "upgrade_streak": self.upgrade_streak,
            "debounce_streak": self.debounce_streak,
            "downgrade_streak": self.downgrade_streak,
            "fallback_streak": self.fallback_streak,
            "fallback_lows": dict(self.fallback_lows),
            "rally": {
                i: {"rally_low": r.rally_low, "low_session": r.low_session, "day": r.day}
                for i, r in self.rally.items()
            },
            "rally_lows": dict(self.rally_lows),
            "distribution_days": {i: list(d) for i, d in self.distribution_days.items()},
            "last_ftd_seq": self.last_ftd_seq,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "RegimeState":
        return cls(
            regime=Regime(data["regime"]),
            seq=int(data["seq"]),
            entered_seq=int(data["entered_seq"]),
            no_ftd=bool(data["no_ftd"]),
            upgrade_streak=int(data["upgrade_streak"]),
            debounce_streak=int(data["debounce_streak"]),
            # Absent from rows written before ``correction_sessions`` existed.
            downgrade_streak=int(data.get("downgrade_streak", 0)),
            fallback_streak=int(data["fallback_streak"]),
            fallback_lows={i: float(v) for i, v in data["fallback_lows"].items()},
            rally={
                i: RallyAttempt(
                    rally_low=float(r["rally_low"]),
                    low_session=int(r["low_session"]),
                    day=int(r["day"]),
                )
                for i, r in data["rally"].items()
            },
            rally_lows={i: float(v) for i, v in data["rally_lows"].items()},
            distribution_days={
                i: tuple(int(s) for s in d) for i, d in data["distribution_days"].items()
            },
            last_ftd_seq=None if data["last_ftd_seq"] is None else int(data["last_ftd_seq"]),
        )


@dataclass(frozen=True, slots=True)
class IndexFollowThrough:
    index: str
    ftd: FollowThroughDay


@dataclass(frozen=True, slots=True)
class Tracking:
    """What this session's bars did, before the score is known."""

    distribution_days: Mapping[str, tuple[int, ...]]
    distribution_today: Mapping[str, bool]
    rally: Mapping[str, RallyAttempt]
    follow_through: IndexFollowThrough | None
    last_ftd_seq: int | None

    def distribution_count(self, after_seq: int | None = None) -> dict[str, int]:
        """Per index, distribution days in the window, after the latest FTD and after
        ``after_seq`` if given."""
        floor = max(s for s in (self.last_ftd_seq, after_seq, -1) if s is not None)
        return {i: sum(1 for s in days if s > floor) for i, days in self.distribution_days.items()}


def _start_correction(bars: IndexBars, seq: int) -> dict[str, RallyAttempt]:
    # The session it enters CORRECTION sets the first correction low (README §5.2).
    return {i: start_rally_attempt(bar, seq) for i, (_, bar) in bars.items()}


def track(
    state: RegimeState | None,
    bars: IndexBars,
    *,
    expiration_session: bool,
    cfg: RegimeConfig,
) -> Tracking:
    """Distribution days and, inside ``CORRECTION``, the rally attempt and any FTD."""
    seq = 0 if state is None else state.seq + 1
    dist = cfg.distribution
    excluded = expiration_session and dist.exclude_expiration_sessions
    days: dict[str, tuple[int, ...]] = {}
    today: dict[str, bool] = {}
    for index, (prev, bar) in bars.items():
        is_day = distribution_days([prev, bar], min_decline=dist.min_decline)[1]
        today[index] = is_day
        earlier = () if state is None else state.distribution_days.get(index, ())
        kept = tuple(s for s in earlier if s > seq - dist.window_sessions)
        days[index] = (*kept, seq) if is_day and not excluded else kept

    if state is None or state.regime is not Regime.CORRECTION:
        last_ftd = None if state is None else state.last_ftd_seq
        rally = _start_correction(bars, seq) if state is None else dict(state.rally)
        return Tracking(days, today, rally, None, last_ftd)

    # One rally attempt across both indexes: an undercut by either restarts both day counts;
    # each index keeps its own low.
    if any(bar.low < state.rally[i].rally_low for i, (_, bar) in bars.items()):
        rally = {
            i: (
                RallyAttempt(rally_low=bar.low, low_session=seq, day=0)
                if bar.low < state.rally[i].rally_low
                else replace(state.rally[i], day=0)
            )
            for i, (_, bar) in bars.items()
        }
        return Tracking(days, today, rally, None, state.last_ftd_seq)

    ftc = cfg.follow_through
    rally = {}
    follow_through: IndexFollowThrough | None = None
    for index, (prev, bar) in bars.items():
        rally[index], ftd = step_rally_attempt(
            state.rally[index],
            prev,
            bar,
            seq,
            min_gain=ftc.min_gain,
            first_day=ftc.first_day,
            last_strong_day=ftc.last_strong_day,
        )
        if ftd is not None and follow_through is None:
            follow_through = IndexFollowThrough(index, ftd)
    last_ftd = seq if follow_through is not None else state.last_ftd_seq
    return Tracking(days, today, rally, follow_through, last_ftd)


def _enter(
    regime: Regime,
    seq: int,
    tracking: Tracking,
    bars: IndexBars,
    *,
    no_ftd: bool = False,
    rally_lows: Mapping[str, float] | None = None,
) -> RegimeState:
    return RegimeState(
        regime=regime,
        seq=seq,
        entered_seq=seq,
        no_ftd=no_ftd,
        rally=_start_correction(bars, seq) if regime is Regime.CORRECTION else {},
        rally_lows=dict(rally_lows or {}),
        distribution_days=tracking.distribution_days,
        last_ftd_seq=tracking.last_ftd_seq,
    )


def advance(
    state: RegimeState | None,
    tracking: Tracking,
    bars: IndexBars,
    score: int,
    cfg: RegimeConfig,
    *,
    index_trend_down: bool = False,
) -> RegimeState:
    """The state after this session. ``state=None`` starts the machine in ``CORRECTION`` (fail
    closed); a real FTD is needed to leave it. ``index_trend_down``: the index-trend pillar is
    at -1, which makes a downgrade to ``CORRECTION`` immediate."""
    if state is None:
        return _enter(Regime.CORRECTION, 0, tracking, bars)
    seq = state.seq + 1
    t = cfg.transitions
    same = replace(
        state,
        seq=seq,
        rally=tracking.rally,
        distribution_days=tracking.distribution_days,
        last_ftd_seq=tracking.last_ftd_seq,
    )
    lows = {i: bar.low for i, (_, bar) in bars.items()}
    down = state.downgrade_streak + 1 if score <= t.correction_max_score else 0
    to_correction = down >= t.correction_sessions or (down > 0 and index_trend_down)

    match state.regime:
        case Regime.CONFIRMED_UPTREND:
            if to_correction:
                return _enter(Regime.CORRECTION, seq, tracking, bars)
            if score < t.debounce_score:
                pressure = _enter(Regime.UNDER_PRESSURE, seq, tracking, bars)
                return replace(pressure, downgrade_streak=down)
            if score < t.confirmed_min_score:
                streak = state.debounce_streak + 1
                if streak >= t.debounce_sessions:
                    return _enter(Regime.UNDER_PRESSURE, seq, tracking, bars)
                return replace(same, debounce_streak=streak)
            return replace(same, debounce_streak=0)

        case Regime.UNDER_PRESSURE:
            if to_correction:
                return _enter(Regime.CORRECTION, seq, tracking, bars)
            if score >= t.confirmed_min_score:
                streak = state.upgrade_streak + 1
                if streak >= t.upgrade_sessions:
                    return _enter(Regime.CONFIRMED_UPTREND, seq, tracking, bars)
                return replace(same, upgrade_streak=streak, downgrade_streak=0)
            return replace(same, upgrade_streak=0, downgrade_streak=down)

        case Regime.CORRECTION:
            if tracking.follow_through is not None:
                rally_lows = {i: r.rally_low for i, r in tracking.rally.items()}
                return _enter(Regime.EARLY_UPTREND, seq, tracking, bars, rally_lows=rally_lows)
            if score >= t.no_ftd_min_score:
                streak = state.fallback_streak + 1
                fallback_lows = {
                    i: min(state.fallback_lows.get(i, low), low) for i, low in lows.items()
                }
                if streak >= t.no_ftd_sessions:
                    return _enter(
                        Regime.EARLY_UPTREND,
                        seq,
                        tracking,
                        bars,
                        no_ftd=True,
                        rally_lows=fallback_lows,
                    )
                return replace(same, fallback_streak=streak, fallback_lows=fallback_lows)
            return replace(same, fallback_streak=0, fallback_lows={})

        case Regime.EARLY_UPTREND:
            undercut = any(low < state.rally_lows[i] for i, low in lows.items())
            since_entry = tracking.distribution_count(after_seq=state.entered_seq)
            if undercut or max(since_entry.values(), default=0) >= t.early_uptrend_max_distribution:
                return _enter(Regime.CORRECTION, seq, tracking, bars)
            if score >= t.confirmed_min_score:
                streak = state.upgrade_streak + 1
                if streak >= t.upgrade_sessions and seq - state.entered_seq >= (
                    t.early_uptrend_min_sessions
                ):
                    return _enter(Regime.CONFIRMED_UPTREND, seq, tracking, bars)
                return replace(same, upgrade_streak=streak)
            return replace(same, upgrade_streak=0)

        case _:  # pragma: no cover - every regime is handled above
            assert_never(state.regime)
