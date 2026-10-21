"""Breadth-pillar inputs from per-ticker daily bars (README §5.2), point in time.

A ticker counts at a session only if it has a bar that session and passes the README §5.1
universe filters (``universe`` in risk.yaml) as of that session: raw close >= $10, 50-session
average dollar volume >= $10M, and >= 252 sessions of history. The same function serves the live
job (2 years of Postgres bars) and the 2.4 backtest.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from auratrade.config import BreadthConfig, UniverseConfig
from auratrade.indicators.breadth import net_new_highs_average
from auratrade.indicators.moving_averages import sma
from auratrade.indicators.series import rolling_max, rolling_min
from auratrade.indicators.universe import universe_flags


@dataclass(frozen=True, slots=True)
class TickerHistory:
    """One security's split-adjusted bars, oldest first; ``raw_closes`` are as traded."""

    dates: Sequence[date]
    highs: Sequence[float]
    lows: Sequence[float]
    closes: Sequence[float]
    raw_closes: Sequence[float]
    volumes: Sequence[float]


@dataclass(frozen=True, slots=True)
class SessionBreadth:
    members: int
    above: int
    new_highs: int
    new_lows: int

    @property
    def fraction_above(self) -> float | None:
        return self.above / self.members if self.members else None


def universe_members(history: TickerHistory, universe: UniverseConfig) -> list[bool]:
    """Per bar of ``history``: whether the security is in the universe at that session."""
    return universe_flags(
        history.closes,
        history.raw_closes,
        history.volumes,
        min_price=universe.min_price,
        min_dollar_volume=universe.min_dollar_volume,
        dollar_volume_sessions=universe.dollar_volume_sessions,
        min_history_sessions=universe.min_history_sessions,
    )


def session_breadth(
    histories: Iterable[TickerHistory],
    sessions: Sequence[date],
    cfg: BreadthConfig,
    universe: UniverseConfig,
) -> dict[date, SessionBreadth]:
    """Universe size, members above their 50 SMA, and 52-week new highs and lows, per session."""
    wanted = set(sessions)
    counts = {s: [0, 0, 0, 0] for s in sessions}
    for h in histories:
        averages = sma(h.closes, cfg.sma)
        highest = rolling_max(h.highs, cfg.new_high_sessions)
        lowest = rolling_min(h.lows, cfg.new_high_sessions)
        members = universe_members(h, universe)
        for i, day in enumerate(h.dates):
            if day not in wanted or not members[i]:
                continue
            c = counts[day]
            c[0] += 1
            average, top, bottom = averages[i], highest[i], lowest[i]
            c[1] += average is not None and h.closes[i] > average
            c[2] += top is not None and h.highs[i] >= top
            c[3] += bottom is not None and h.lows[i] <= bottom
    return {s: SessionBreadth(*c) for s, c in counts.items()}


def breadth_inputs(
    breadth: Mapping[date, SessionBreadth], sessions: Sequence[date], cfg: BreadthConfig
) -> dict[date, tuple[float | None, float | None]]:
    """Per session: (share of the universe above its 50 SMA, the 10-session average of new
    highs minus new lows). ``sessions`` are consecutive; the average is ``None`` until it has a
    full window."""
    points = [breadth[s] for s in sessions]
    net = net_new_highs_average(
        [p.new_highs for p in points], [p.new_lows for p in points], cfg.net_new_highs_sessions
    )
    return {s: (p.fraction_above, n) for s, p, n in zip(sessions, points, net, strict=True)}
