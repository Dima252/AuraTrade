"""The historical full scan of the 3.8 baseline backtest (pre-registration step 1, README §15):
every strategy on every point-in-time universe member, each session, with its features as they
stood then; and the breakout tracker built from it (step 2).

- **Universe:** the 1.6 membership table (delisted securities included, raw-price filters).
- **Cross-sections:** RS ratings and momentum ranks ranked within each session's universe, as
  `scripts/scan_real_data.py` does.
- **Base count:** `indicators.bases.forming_stages` over each security's whole loaded history
  (decided with the user 2026-10-24). A history that starts at the listing (`listed_at` after
  the first loaded session, not a relisting) counts reliably from its first base; any other
  starts unreliable until a reset is seen.
- **Industry groups:** today's SIC codes rolled up as ingest does (`ingest.companies.rollup`,
  `industry_min_group_size`) over the securities ever in the universe; applied to the past, a
  small look-ahead the report states. Securities without a SIC code have no group (neutral).
- **Fundamentals** by EDGAR `filed` date, **earnings dates** from the EDGAR 8-K history.
- **`EmergingLeader`** reads the regime's `CORRECTION` spells, which the 5-pillar regime takes
  from the tracker, which reads the scan. Its scan at T reads only spells that started by T, cut
  at T, so the loop is causal: `scan_pass` scans it for several spell sets at once and the
  caller iterates to the fixed point (`backtest.regime_search`).

Nothing after ``last`` is loaded.
"""

from __future__ import annotations

import bisect
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from auratrade.calendar.sessions import Session
from auratrade.config import RiskConfig, Strategy
from auratrade.history.read import (
    HistoryBar,
    SecuritySeries,
    earnings_by_security,
    fundamentals_by_security,
    index_securities,
    known_on,
    load_macro,
    load_membership,
    load_securities,
    load_series,
    ranks_by_security,
)
from auratrade.indicators.bars import OHLCV
from auratrade.indicators.bases import BaseCountState, BaseRules, forming_stages
from auratrade.indicators.earnings import EarningsTiming
from auratrade.indicators.relative_strength import group_rs_ranks
from auratrade.ingest.companies import group_for, rollup
from auratrade.journal.breakout_tracker import (
    Tracked,
    Watched,
    above_trigger,
    for_pillar,
    triggers,
)
from auratrade.regime.inputs import MacroSeries
from auratrade.regime.pillars import TrackedBreakout
from auratrade.storage import ObjectStore
from auratrade.strategies.base import BaseStrategy, Setup, SymbolData
from auratrade.strategies.episodic_pivot import EpisodicPivot
from auratrade.strategies.ranking import Features, Fundamentals, candidate_features
from auratrade.strategies.watchlist import corrections_for, reaction_sessions

LOAD_START = date(2016, 1, 1)
FIRST_SCANNED = date(2017, 1, 1)
"""2016 is warm-up: the 200-day average and the 252-session windows fill (README §15)."""
TUNING_END = date(2022, 12, 30)

Spells = Sequence[tuple[date, date | None]]


@dataclass(frozen=True, slots=True)
class Found:
    """One setup of one session's full scan."""

    security: str
    index: int
    """The scan session's position in the security's bars."""
    setup: Setup
    features: Features | None = None
    """None in a tracker-only pass."""


@dataclass(slots=True)
class History:
    """The loaded history up to ``sessions[-1]`` and the cross-sectional values."""

    sessions: list[date]
    minutes: dict[date, int]
    securities: dict[str, dict[str, Any]]
    members: dict[date, set[str]]
    series: dict[str, SecuritySeries]
    indexes: dict[str, str]
    macro: dict[str, MacroSeries]
    earnings: dict[str, list[tuple[date, EarningsTiming]]]
    quarters: dict[str, list[tuple[date, Fundamentals]]]
    groups: dict[str, str]
    rs: dict[str, list[float | None]]
    momentum: dict[str, list[float | None]]
    group_rs: dict[date, dict[str, float]]
    """Each group's RS percentile on each session."""
    spy: dict[date, float]

    @property
    def scanned(self) -> list[str]:
        """Every security that is a universe member on some loaded session."""
        ever: set[str] = set().union(*self.members.values()) if self.members else set()
        return sorted(ever & self.series.keys())

    def symbol(self, security: str) -> str:
        return str(self.securities[security]["symbol"])


def industry_groups(
    securities: Mapping[str, Mapping[str, Any]], keys: Iterable[str], min_size: int
) -> dict[str, str]:
    """Each security's industry group from its SIC code (``ingest.companies``' rollup over
    ``keys``); securities without a code are left out."""
    sic = {k: int(securities[k]["sic"]) for k in keys if securities[k].get("sic") is not None}
    groups = rollup(Counter(sic.values()), min_size)
    present = set(groups.values())
    out = {}
    for key, code in sic.items():
        group = group_for(code, present)
        if group is not None:
            out[key] = group
    return out


def group_ranks(
    rs: Mapping[str, Sequence[float | None]],
    series: Mapping[str, SecuritySeries],
    groups: Mapping[str, str],
) -> dict[date, dict[str, float]]:
    """Each group's RS percentile per session, from that session's ratings (ratings exist only
    for universe members)."""
    ratings: dict[date, dict[str, float]] = defaultdict(dict)
    for key, values in rs.items():
        if key not in groups:
            continue
        for d, r in zip(series[key].dates, values, strict=True):
            if r is not None:
                ratings[d][key] = r
    return {
        d: {g: v.percentile for g, v in group_rs_ranks(day, groups).items()}
        for d, day in ratings.items()
    }


def count_state(security: Mapping[str, Any], first_loaded: date) -> BaseCountState | None:
    """Reliable from the first base when the history starts at the listing."""
    listed = security.get("listed_at")
    if listed is not None and listed > first_loaded and not security.get("relisted"):
        return BaseCountState.at_listing()
    return None


def load_history(
    store: ObjectStore,
    days: Sequence[Session],
    risk: RiskConfig,
    *,
    last: date,
    log: Callable[[str], None] = lambda _: None,
) -> History:
    """The history from `LOAD_START` to ``last`` and nothing after it."""
    clock = time.monotonic()
    sessions = [s.day for s in days if LOAD_START <= s.day <= last]
    wanted = set(sessions)
    securities = load_securities(store)
    members = load_membership(store, {d.year for d in sessions})
    members = {d: m for d, m in members.items() if d in wanted}
    ever: set[str] = set().union(*members.values()) if members else set()
    indexes = index_securities(securities, risk.regime.indexes)
    series = load_series(store, sessions, securities=ever | set(indexes.values()))
    macro = {
        name: MacroSeries(
            [d for d in m.dates if d <= last],
            [v for d, v in zip(m.dates, m.values, strict=True) if d <= last],
        )
        for name, m in load_macro(store).items()
    }
    earnings = {k: [r for r in v if r[0] <= last] for k, v in earnings_by_security(store).items()}
    quarters = {
        k: [q for q in v if q[0] <= last] for k, v in fundamentals_by_security(store).items()
    }
    log(f"load: {time.monotonic() - clock:.0f} s")
    rs, momentum = ranks_by_security(series, members, risk)
    groups = industry_groups(securities, ever, risk.ingest.industry_min_group_size)
    spy = series[indexes["SPY"]]
    log(f"ranks: {time.monotonic() - clock:.0f} s")
    return History(
        sessions=sessions,
        minutes={s.day: s.minutes for s in days if s.day in wanted},
        securities=securities,
        members=members,
        series=series,
        indexes=indexes,
        macro=macro,
        earnings=earnings,
        quarters=quarters,
        groups=groups,
        rs=rs,
        momentum=momentum,
        group_rs=group_ranks(rs, series, groups),
        spy=dict(zip(spy.dates, spy.close, strict=True)),
    )


def scan_pass(
    h: History,
    risk: RiskConfig,
    strategies: Sequence[BaseStrategy],
    spell_sets: Sequence[Spells],
    *,
    first: date = FIRST_SCANNED,
    features: bool,
    morning: bool = False,
) -> list[list[Found]]:
    """``strategies`` on every member session from ``first``, once per spell set: one list of
    setups per spell set. Each security's data is built once and shared by the spell sets.

    ``morning``: the `EpisodicPivot` view at the gap session's open (#143): ``morning`` instead
    of ``scan``, and the features as of the prior close."""
    out: list[list[Found]] = [[] for _ in spell_sets]
    rules = BaseRules(**risk.bases.model_dump())
    for key in h.scanned:
        s = h.series[key]
        days = [i for i, d in enumerate(s.dates) if d >= first and key in h.members.get(d, ())]
        if not days:
            continue
        bars = s.bars()
        benchmark = [h.spy.get(d) for d in s.dates]
        base = SymbolData.build(
            h.symbol(key),
            bars,
            rs_ratings=h.rs[key],
            momentum_ranks=h.momentum[key],
            risk=risk,
            benchmark_closes=None if None in benchmark else [c or 0.0 for c in benchmark],
            earnings_sessions=reaction_sessions(s.dates, h.earnings.get(key, [])),
        )
        stages = (
            forming_stages(bars, rules, count_state(h.securities[key], h.sessions[0]))
            if features
            else []
        )
        for found, spells in zip(out, spell_sets, strict=True):
            data = replace(base, corrections=corrections_for(s.dates, spells)) if spells else base
            for i in days:
                at = i - 1 if morning else i
                if at < 0:
                    continue
                for strategy in strategies:
                    setup = (
                        strategy.morning(data, i)
                        if morning and isinstance(strategy, EpisodicPivot)
                        else strategy.scan(data, i)
                    )
                    if setup is None:
                        continue
                    found.append(
                        Found(
                            key,
                            i,
                            setup,
                            _features(h, key, data, at, stages[at], risk) if features else None,
                        )
                    )
    return out


def _features(
    h: History, key: str, data: SymbolData, i: int, stage: tuple[int, bool], risk: RiskConfig
) -> Features:
    day = h.series[key].dates[i]
    group = h.groups.get(key)
    return candidate_features(
        data,
        i,
        risk,
        group_rs=None if group is None else h.group_rs.get(day, {}).get(group),
        fundamentals=known_on(h.quarters.get(key, []), day),
        base_stage=stage[0],
        base_reliable=stage[1],
    )


# --- The breakout tracker (step 2) ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackerRow:
    security: str
    strategy: Strategy
    session: date
    """The trigger session."""
    above_trigger: bool | None
    """None when the history ends before its result session."""


def track(h: History, found: Iterable[Found], risk: RiskConfig) -> list[TrackerRow]:
    """Every trigger of the full scan and its result, by the live tracker rules (#142): a setup
    of T-1's scan whose T high reaches its pivot; above the pivot at the close
    ``result_sessions`` later, or at the last close before it without a bar then."""
    by_day: dict[date, list[Found]] = defaultdict(list)
    for f in found:
        by_day[h.series[f.security].dates[f.index]].append(f)
    position = {d: k for k, d in enumerate(h.sessions)}
    result_sessions = risk.regime.leadership.result_sessions
    rows = []
    for prev, listed in sorted(by_day.items()):
        k = position[prev] + 1
        if k >= len(h.sessions):
            continue
        session = h.sessions[k]
        bars: dict[str, OHLCV] = {}
        at: dict[str, tuple[str, int]] = {}
        for f in listed:
            s = h.series[f.security]
            j = f.index + 1
            if j < len(s.dates) and s.dates[j] == session:
                bars[f.setup.symbol] = HistoryBar(
                    s.open[j], s.high[j], s.low[j], s.close[j], s.volume[j]
                )
                at[f.setup.symbol] = (f.security, j)
        watched = [Watched(f.setup.symbol, f.setup.strategy, f.setup.pivot) for f in listed]
        for b in triggers(watched, bars, session):
            security, j = at[b.symbol]
            result_at = k + result_sessions
            result: bool | None = None
            if result_at < len(h.sessions):
                s = h.series[security]
                last = bisect.bisect_right(s.dates, h.sessions[result_at]) - 1
                result = above_trigger(b, close_then_now=b.close, result_close=s.close[last])
            rows.append(TrackerRow(security, b.strategy, session, result))
    return rows


def breakouts_by_session(
    rows: Sequence[TrackerRow], sessions: Sequence[date], risk: RiskConfig
) -> dict[date, list[TrackedBreakout]]:
    """What the leadership pillar reads at each session (``journal.breakout_tracker.for_pillar``
    over the rows of its window)."""
    cfg = risk.regime.leadership
    window = cfg.result_sessions + cfg.window_sessions
    per: dict[date, list[Tracked]] = defaultdict(list)
    for r in rows:
        per[r.session].append(Tracked(r.session, r.above_trigger))
    out = {}
    for k, day in enumerate(sessions):
        days = sessions[max(0, k - window + 1) : k + 1]
        out[day] = for_pillar([t for d in days for t in per.get(d, ())], days, cfg)
    return out
