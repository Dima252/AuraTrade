"""The evening's full scan (README §5.3, task 3.7), pure: session T's universe through every
strategy scan, scored and capped.

Every setup of every strategy is kept, whatever the regime, the cap or the ranking: the breakout
tracker reads them all (README §5.12). The capped selection (``ranking.select``, the computed
regime's fundable strategies) gives the ranked ones their ``rank``, the order the Analyst and
the baseline book take them in.

The inputs are what Postgres holds (about two years of bars). Cross-sectional values (RS ratings,
momentum ranks) are ranked within each session's universe as ``indicators.universe`` rebuilds it
from the loaded bars; the industry group's RS rank uses T's ratings; the base count stays
neutral until the base table is kept current (``Features.base_stage`` None).
"""

import bisect
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from auratrade.config import Regime, RiskConfig
from auratrade.indicators.bars import OHLCV
from auratrade.indicators.earnings import EarningsTiming, reaction_session
from auratrade.indicators.relative_strength import group_rs_ranks, percentile_ranks
from auratrade.indicators.series import rolling_mean
from auratrade.indicators.universe import universe_flags
from auratrade.strategies.base import Correction, Setup, SymbolData
from auratrade.strategies.ranking import Features, Fundamentals, candidate_features, score_pool
from auratrade.strategies.ranking import select as select_capped
from auratrade.strategies.registry import build_strategies, scan_all
from auratrade.strategies.trend_template import rs_scores


@dataclass(frozen=True, slots=True)
class SymbolHistory:
    """One security's bars up to T, oldest first, with what the ranking reads about it."""

    ticker_id: int
    symbol: str
    dates: Sequence[date]
    bars: Sequence[OHLCV]
    """Split-adjusted."""
    raw_closes: Sequence[float]
    industry: str | None = None
    earnings: Sequence[tuple[date, EarningsTiming]] = ()
    fundamentals: Fundamentals | None = None
    """The latest quarter filed before T (README §15: readable from the next session)."""


@dataclass(frozen=True, slots=True)
class WatchItem:
    """One setup of session T's scan."""

    ticker_id: int
    symbol: str
    setup: Setup
    features: Features
    components: Mapping[str, float]
    score: float
    rank: int | None
    """Its place in the capped list (1 = first); None when not selected."""
    close: float
    atr: float | None
    adr: float | None
    """A fraction."""
    sma_50: float | None
    dollar_volume: float | None
    """The ``sim.volume_sessions`` average dollar volume (slippage)."""
    industry: str | None


def membership(histories: Sequence[SymbolHistory], risk: RiskConfig) -> dict[date, set[int]]:
    """The universe on each loaded session (README §5.1), by ticker id."""
    u = risk.universe
    out: dict[date, set[int]] = defaultdict(set)
    for h in histories:
        flags = universe_flags(
            [b.close for b in h.bars],
            h.raw_closes,
            [b.volume for b in h.bars],
            min_price=u.min_price,
            min_dollar_volume=u.min_dollar_volume,
            dollar_volume_sessions=u.dollar_volume_sessions,
            min_history_sessions=u.min_history_sessions,
        )
        for d, member in zip(h.dates, flags, strict=True):
            if member:
                out[d].add(h.ticker_id)
    return dict(out)


def cross_sections(
    histories: Sequence[SymbolHistory], members: Mapping[date, set[int]], risk: RiskConfig
) -> tuple[dict[int, list[float | None]], dict[int, list[float | None]]]:
    """Each security's RS rating and best momentum rank per bar, ranked within that session's
    universe (README §5.3); None where it wasn't a member."""
    lookbacks = risk.strategies.MOMENTUM_BREAKOUT.rank_lookbacks
    rs: dict[date, dict[int, float]] = defaultdict(dict)
    returns: dict[tuple[date, int], dict[int, float]] = defaultdict(dict)
    for h in histories:
        closes = [b.close for b in h.bars]
        for i, (d, score) in enumerate(
            zip(h.dates, rs_scores(closes, risk.trend_template), strict=True)
        ):
            if h.ticker_id not in members.get(d, ()):
                continue
            if score is not None:
                rs[d][h.ticker_id] = score
            for n in lookbacks:
                if i >= n:
                    returns[d, n][h.ticker_id] = closes[i] / closes[i - n] - 1
    rs_pct = {d: percentile_ranks(v) for d, v in rs.items()}
    momentum_pct = {k: percentile_ranks(v) for k, v in returns.items()}
    rs_out: dict[int, list[float | None]] = {}
    momentum_out: dict[int, list[float | None]] = {}
    for h in histories:
        rs_out[h.ticker_id] = [rs_pct.get(d, {}).get(h.ticker_id) for d in h.dates]
        best: list[float | None] = []
        for d in h.dates:
            values = [
                r
                for n in lookbacks
                if (r := momentum_pct.get((d, n), {}).get(h.ticker_id)) is not None
            ]
            best.append(max(values) if values else None)
        momentum_out[h.ticker_id] = best
    return rs_out, momentum_out


def correction_spells(regimes: Mapping[date, Regime]) -> list[tuple[date, date | None]]:
    """Each `CORRECTION` spell's first and last session (None if it is still open)."""
    spells: list[tuple[date, date | None]] = []
    start: date | None = None
    previous: date | None = None
    for day in sorted(regimes):
        if regimes[day] is Regime.CORRECTION:
            start = start or day
        elif start is not None:
            spells.append((start, previous))
            start = None
        previous = day
    if start is not None:
        spells.append((start, None))
    return spells


def corrections_for(
    dates: Sequence[date], spells: Sequence[tuple[date, date | None]]
) -> tuple[Correction, ...]:
    """The spells in one security's bar positions (spells outside its bars are dropped)."""
    out = []
    for start, end in spells:
        first = bisect.bisect_left(dates, start)
        if first == len(dates):
            continue
        last = None if end is None else bisect.bisect_right(dates, end) - 1
        if last is not None and last < first:
            continue
        out.append(Correction(first, last))
    return tuple(out)


def reaction_sessions(
    dates: Sequence[date], reports: Sequence[tuple[date, EarningsTiming]]
) -> frozenset[int]:
    """Bar positions of the sessions that react to each report; a report on a day without a
    bar reacts on the next session, like one after the close."""
    out = set()
    for day, timing in reports:
        i = bisect.bisect_left(dates, day)
        on_session = i < len(dates) and dates[i] == day
        j = reaction_session(i, timing) if on_session else i
        if j < len(dates):
            out.add(j)
    return frozenset(out)


def scan_session(
    histories: Sequence[SymbolHistory],
    *,
    session: date,
    benchmark: Mapping[date, float],
    regimes: Mapping[date, Regime],
    regime: Regime,
    risk: RiskConfig,
    cap: int,
) -> list[WatchItem]:
    """Session ``session``'s full scan: every setup of the universe members with a bar on it,
    the capped selection ranked. ``benchmark`` is SPY's closes, ``regimes`` the computed regime
    of each session so far (the `CORRECTION` spells), ``regime`` the one selecting tonight."""
    members = membership(histories, risk)
    today = members.get(session, set())
    rs, momentum = cross_sections(histories, members, risk)
    spells = correction_spells(regimes)
    strategies = build_strategies(risk)
    ratings_today = {
        h.ticker_id: r
        for h in histories
        if h.dates and h.dates[-1] == session and (r := rs[h.ticker_id][-1]) is not None
    }
    groups = {h.ticker_id: h.industry for h in histories if h.industry is not None}
    group_rank = group_rs_ranks(
        {str(t): r for t, r in ratings_today.items()}, {str(t): g for t, g in groups.items()}
    )

    found: list[tuple[SymbolHistory, SymbolData, Setup, Features]] = []
    for h in histories:
        if h.ticker_id not in today or not h.dates or h.dates[-1] != session:
            continue
        closes = [benchmark.get(d) for d in h.dates]
        data = SymbolData.build(
            h.symbol,
            h.bars,
            rs_ratings=rs[h.ticker_id],
            momentum_ranks=momentum[h.ticker_id],
            risk=risk,
            earnings_sessions=reaction_sessions(h.dates, h.earnings),
            benchmark_closes=None if None in closes else [c or 0.0 for c in closes],
            corrections=corrections_for(h.dates, spells),
        )
        group = group_rank.get(h.industry) if h.industry is not None else None
        for setup in scan_all(strategies, data):
            features = candidate_features(
                data,
                data.last,
                risk,
                group_rs=None if group is None else group.percentile,
                fundamentals=h.fundamentals,
            )
            found.append((h, data, setup, features))

    scored = score_pool([(setup, features) for _, _, setup, features in found], risk)
    chosen = select_capped(scored, regime, risk, cap)
    rank = {id(s): i for i, s in enumerate(chosen, start=1)}
    out = []
    for (h, data, setup, features), s in zip(found, scored, strict=True):
        out.append(
            WatchItem(
                ticker_id=h.ticker_id,
                symbol=h.symbol,
                setup=setup,
                features=features,
                components=s.components,
                score=s.score,
                rank=rank.get(id(s)),
                close=h.bars[-1].close,
                atr=data.atr(risk.watchlist.atr_sessions)[data.last],
                adr=features.adr,
                sma_50=data.sma(risk.ranking.extension_sma)[data.last],
                dollar_volume=_dollar_volume(h.bars, risk.sim.volume_sessions),
                industry=h.industry,
            )
        )
    return out


def _dollar_volume(bars: Sequence[OHLCV], sessions: int) -> float | None:
    return rolling_mean([b.close * b.volume for b in bars], sessions)[-1] if bars else None
