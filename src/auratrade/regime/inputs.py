"""Build ``SessionInputs`` from loaded histories (pure; the store and the backtest load them).

Missing index data fails closed: ``MissingDataError`` stops the regime job, so no regime is
written and the evening chain stops before the scan.
"""

import bisect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from auratrade.config import RegimeConfig
from auratrade.indicators.bars import Bar
from auratrade.indicators.moving_averages import falling, rising, sma
from auratrade.indicators.volume import normalize_session_volumes
from auratrade.regime.breadth import TickerHistory, breadth_inputs, session_breadth
from auratrade.regime.classifier import SessionInputs
from auratrade.regime.pillars import IndexTrend, TrackedBreakout, quarterly_expiration_sessions

FULL_SESSION_MINUTES = 390
VIX, VIX3M = "VIX", "VIX3M"


class MissingDataError(RuntimeError):
    """Data the regime can't be computed without (fail closed)."""


@dataclass(frozen=True, slots=True)
class MacroSeries:
    """One series' observations, oldest first, from whichever source supplied each date."""

    dates: Sequence[date]
    values: Sequence[float]

    def latest(self, on_or_before: date) -> tuple[date, float] | None:
        i = bisect.bisect_right(self.dates, on_or_before) - 1
        return None if i < 0 else (self.dates[i], self.values[i])


@dataclass(frozen=True, slots=True)
class LoadedData:
    sessions: Sequence[date]
    """Every session of the loaded range, consecutive, oldest first."""
    session_minutes: Mapping[date, int]
    indexes: Mapping[str, TickerHistory]
    universe: Sequence[TickerHistory]
    macro: Mapping[str, MacroSeries]


@dataclass(frozen=True, slots=True)
class _IndexSeries:
    bars: Mapping[date, Bar]
    trend: Mapping[date, IndexTrend]


def _index_series(
    name: str, history: TickerHistory, data: LoadedData, cfg: RegimeConfig
) -> _IndexSeries:
    # The averages assume one bar per session: a gap would shift every window, so it fails.
    first = bisect.bisect_left(data.sessions, history.dates[0]) if history.dates else 0
    expected = list(data.sessions[first:])
    if list(history.dates) != expected:
        missing = sorted(set(expected) - set(history.dates))
        raise MissingDataError(f"{name} bars missing for {missing[:5]} ({len(missing)} sessions)")
    volumes = normalize_session_volumes(
        history.volumes, [data.session_minutes[d] for d in history.dates], FULL_SESSION_MINUTES
    )
    it = cfg.index_trend
    short = sma(history.closes, it.short_sma)
    long = sma(history.closes, it.long_sma)
    up = rising(short, it.slope_sessions)
    down = falling(short, it.slope_sessions)
    bars, trend = {}, {}
    for i, day in enumerate(history.dates):
        close = history.closes[i]
        bars[day] = Bar(close, history.highs[i], history.lows[i], close, volumes[i])
        trend[day] = IndexTrend(close, short[i], long[i], up[i], down[i])
    return _IndexSeries(bars, trend)


def _sessions_after(sessions: Sequence[date], since: date, until: date) -> int:
    """Sessions in ``(since, until]``: how stale an observation from ``since`` is."""
    return bisect.bisect_right(sessions, until) - bisect.bisect_right(sessions, since)


def build_inputs(
    data: LoadedData,
    targets: Sequence[date],
    cfg: RegimeConfig,
    *,
    breakouts: Callable[[date], Sequence[TrackedBreakout]] = lambda _: (),
) -> list[SessionInputs]:
    """One ``SessionInputs`` per target session (each must be in ``data.sessions`` with a
    session before it)."""
    for name in cfg.indexes:
        if name not in data.indexes:
            raise MissingDataError(f"no bars for index {name}")
    series = {i: _index_series(i, data.indexes[i], data, cfg) for i in cfg.indexes}
    first = data.sessions.index(targets[0]) - cfg.breadth.net_new_highs_sessions + 1
    window = list(data.sessions[max(first, 0) : data.sessions.index(targets[-1]) + 1])
    breadth = breadth_inputs(
        session_breadth(data.universe, window, cfg.breadth), window, cfg.breadth
    )
    expirations = quarterly_expiration_sessions(data.sessions)

    def macro_at(day: date) -> tuple[float | None, float | None, int | None]:
        vix, vix3m = data.macro.get(VIX), data.macro.get(VIX3M)
        a = vix.latest(day) if vix is not None else None
        b = vix3m.latest(day) if vix3m is not None else None
        if a is None or b is None:
            return None, None, None
        stale = max(_sessions_after(data.sessions, d, day) for d in (a[0], b[0]))
        return a[1], b[1], stale

    out = []
    for day in targets:
        k = data.sessions.index(day)
        if k == 0:
            raise MissingDataError(f"no session before {day} in the loaded range")
        prev_day = data.sessions[k - 1]
        index_bars = {}
        for name, s in series.items():
            if day not in s.bars or prev_day not in s.bars:
                raise MissingDataError(f"{name} has no bar for {day} or {prev_day}")
            index_bars[name] = (s.bars[prev_day], s.bars[day])
        vix, vix3m, stale = macro_at(day)
        prior_ratios = []
        for back in range(cfg.crisis.vix_ratio_sessions - 1, 0, -1):
            pv, pv3m, _ = macro_at(data.sessions[max(k - back, 0)])
            prior_ratios.append(None if pv is None or pv3m is None else pv / pv3m)
        fraction_above, net = breadth.get(day, (None, None))
        out.append(
            SessionInputs(
                session=day,
                index_bars=index_bars,
                index_trend={name: s.trend[day] for name, s in series.items()},
                fraction_above=fraction_above,
                net_new_highs=net,
                vix=vix,
                vix3m=vix3m,
                macro_stale_sessions=stale,
                breakouts=breakouts(day),
                expiration_session=day in expirations,
                prior_vix_ratios=tuple(prior_ratios),
            )
        )
    return out
