"""The five regime pillars, the crisis override and the expiration-session flag (README §5.2).

Pure functions on precomputed values. Each pillar scores +1, 0 or -1. A missing input scores 0
(neutral), the same rule README §4 sets for stale macro data. The job decides separately whether
missing data is a reason to fail.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from auratrade.config import (
    BreadthConfig,
    CrisisConfig,
    DistributionConfig,
    LeadershipConfig,
    VolatilityConfig,
)

type PillarScore = Literal[-1, 0, 1]

FRIDAY = 4
EXPIRATION_MONTHS = (3, 6, 9, 12)


@dataclass(frozen=True, slots=True)
class IndexTrend:
    """One index at the session: its close, the two SMAs and the short SMA's slope."""

    close: float
    short_sma: float | None
    long_sma: float | None
    short_rising: bool
    short_falling: bool


def index_trend_pillar(indexes: Mapping[str, IndexTrend]) -> PillarScore:
    """+1 when **every** index closes above its 50 SMA, the 50 is above the 200 and the 50 is
    rising; -1 when **either** closes below its 200 SMA with the 50 falling."""
    if not indexes:
        return 0
    if any(
        i.long_sma is not None and i.close < i.long_sma and i.short_falling
        for i in indexes.values()
    ):
        return -1
    if all(
        i.short_sma is not None
        and i.long_sma is not None
        and i.close > i.short_sma > i.long_sma
        and i.short_rising
        for i in indexes.values()
    ):
        return 1
    return 0


def breadth_pillar(
    fraction_above: float | None, net_new_highs: float | None, cfg: BreadthConfig
) -> PillarScore:
    """``fraction_above``: share of the universe above its 50 SMA. ``net_new_highs``: the
    10-session average of new highs minus new lows."""
    if fraction_above is None or net_new_highs is None:
        return 0
    if fraction_above < cfg.down_max_above or net_new_highs < 0:
        return -1
    if fraction_above > cfg.up_min_above and net_new_highs > 0:
        return 1
    return 0


def volatility_pillar(
    vix: float | None, vix3m: float | None, *, stale: bool, cfg: VolatilityConfig
) -> PillarScore:
    """Stale macro data (more than ``data.macro_stale_sessions``) scores 0 (README §4)."""
    if stale or vix is None or vix3m is None:
        return 0
    ratio = vix / vix3m
    if ratio > cfg.down_min_ratio or vix > cfg.down_min_vix:
        return -1
    if vix < cfg.up_max_vix and ratio < cfg.up_max_ratio:
        return 1
    return 0


def distribution_pillar(day_count: int, cfg: DistributionConfig) -> PillarScore:
    """``day_count``: the higher of the per-index distribution-day counts."""
    if day_count >= cfg.down_min_days:
        return -1
    if day_count <= cfg.up_max_days:
        return 1
    return 0


@dataclass(frozen=True, slots=True)
class TrackedBreakout:
    """A breakout-tracker entry (task 3.7): every scan trigger, ungated."""

    sessions_ago: int
    """0 = triggered at this session."""
    above_trigger: bool | None
    """Above its trigger ``result_sessions`` later; ``None`` while the result is incomplete."""


@dataclass(frozen=True, slots=True)
class Leadership:
    score: PillarScore
    breakouts: int
    share_above: float | None


def leadership_pillar(breakouts: Sequence[TrackedBreakout], cfg: LeadershipConfig) -> Leadership:
    """Breakouts from T-24 to T-5 only: their 5-session result is complete (decision #82).
    Fewer than ``min_breakouts`` scores 0."""
    first = cfg.result_sessions
    last = cfg.result_sessions + cfg.window_sessions - 1
    results = [b.above_trigger for b in breakouts if first <= b.sessions_ago <= last]
    if any(r is None for r in results):
        raise ValueError("a breakout in the leadership window has no complete result")
    n = len(results)
    if n < cfg.min_breakouts:
        return Leadership(0, n, None)
    share = sum(1 for r in results if r) / n
    if share >= cfg.up_min_share:
        return Leadership(1, n, share)
    if share <= cfg.down_max_share:
        return Leadership(-1, n, share)
    return Leadership(0, n, share)


@dataclass(frozen=True, slots=True)
class Crisis:
    active: bool
    reasons: tuple[str, ...]


def crisis_override(
    ratios: Sequence[float | None],
    *,
    macro_stale: bool,
    index_return: float | None,
    cfg: CrisisConfig,
) -> Crisis:
    """The crisis override (README §5.2), decided at a session's close for the next session.

    - ``ratios``: VIX/VIX3M at this session and the sessions before it, newest last, each from
      the latest values on or before that session (CBOE, or FRED when CBOE is late). The last
      ``vix_ratio_sessions`` all above ``vix_ratio_above`` trigger it, for as long as that lasts.
    - ``index_return``: SPY's close-to-close return at this session. A drop of ``index_drop``
      covers this session (intraday, from the monitor) and the next one (this label).
    - ``macro_stale``: both sources more than ``data.macro_stale_sessions`` stale. Without
      VIX data the check can't be made, so it fails closed (user ruling 2026-10-10).
    """
    reasons: list[str] = []
    if macro_stale:
        reasons.append("macro_stale")
    else:
        recent = list(ratios[-cfg.vix_ratio_sessions :])
        if len(recent) == cfg.vix_ratio_sessions and all(
            r is not None and r > cfg.vix_ratio_above for r in recent
        ):
            reasons.append("vix_term_inversion")
    if index_return is not None and index_return <= -cfg.index_drop:
        reasons.append("index_drop")
    return Crisis(bool(reasons), tuple(reasons))


def quarterly_expiration_sessions(sessions: Collection[date]) -> set[date]:
    """Quarterly option-expiration sessions: the third Friday of March, June, September and
    December, or the last session before it when that Friday is a holiday. S&P 500 and
    Nasdaq-100 rebalances take effect on the same sessions."""
    days = sorted(sessions)
    if not days:
        return set()
    flagged: set[date] = set()
    for year in range(days[0].year, days[-1].year + 1):
        for month in EXPIRATION_MONTHS:
            first = date(year, month, 1)
            third_friday = first + timedelta(days=(FRIDAY - first.weekday()) % 7 + 14)
            # The expiration moves to the prior session in the same week (e.g. Good Friday).
            candidates = [d for d in days if third_friday - timedelta(days=4) <= d <= third_friday]
            if candidates:
                flagged.add(candidates[-1])
    return flagged
