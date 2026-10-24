"""Real-data run of the strategy scans (tasks 3.2a, 3.2b) over the tuning period's
point-in-time universe: what they find, how often, and with what levels.

    uv run python scripts/scan_real_data.py            # writes docs/backtests/scans.md

Every strategy (the `EpisodicPivot` one as its backtest proxy) runs on every universe member on
every session of 2017-2022 (2016 warms up the 200-day and the 252-session windows), with the
RS rating and momentum rank ranked within that session's universe. `EmergingLeader` reads the
regime's `CORRECTION` spells from a replay of the live classifier over the same history, as
`backtest/regime.py` runs it, and SPY's closes. **The holdout stays
closed:** nothing after 2022 is loaded. Nothing is tuned here; the counts and levels are for a
human to judge, and the samples are for a visual check against charts. The scans are judged
on trades in the 3.8 baseline backtest. Every setup goes to var/scans/setups.csv (git-ignored).
A `## Reading` section written by hand at the end of the report is kept when it is regenerated.

Each session's setups are then scored and capped as the evening scan does (3.3): the quality
score, the regime's fundable strategies, one slot per ticker, the top 40, and the verdict cache
replayed over the selections. Two of the eight components score neutral here: the industry
group's RS (the history has no point-in-time group map yet) and the base count (it needs the
count as of each session). The catalyst and the EP proxy's earnings flag use the history's EDGAR
earnings dates and point-in-time fundamentals (by `filed` date). Not wired: half-day volume
normalization (a handful of sessions a year).
"""

from __future__ import annotations

import argparse
import bisect
import csv
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from auratrade.config import REPO_ROOT, Regime, RiskConfig, Strategy, load_risk_config
from auratrade.history.edgar import EARNINGS, FUNDAMENTALS
from auratrade.history.read import (
    SecuritySeries,
    index_securities,
    load_macro,
    load_membership,
    load_securities,
    load_series,
)
from auratrade.history.store import get_table, table_key
from auratrade.indicators.earnings import EarningsTiming
from auratrade.indicators.relative_strength import percentile_ranks
from auratrade.regime.classifier import classify
from auratrade.regime.inputs import LoadedData, MacroSeries, build_inputs
from auratrade.regime.state import RegimeState
from auratrade.storage import ObjectStore
from auratrade.strategies.base import Setup, SymbolData
from auratrade.strategies.emerging_leader import EmergingDetail
from auratrade.strategies.episodic_pivot import EpisodicPivotDetail
from auratrade.strategies.leader_pullback import PullbackDetail
from auratrade.strategies.momentum_breakout import MomentumDetail
from auratrade.strategies.ranking import (
    COMPONENTS,
    Features,
    Fundamentals,
    candidate_features,
    score_pool,
    select,
)
from auratrade.strategies.registry import build_strategies
from auratrade.strategies.trend_template import rs_scores
from auratrade.strategies.vcp_breakout import VcpDetail
from auratrade.strategies.verdict_cache import CachedVerdict, needs_review, structure
from auratrade.strategies.watchlist import correction_spells, corrections_for, reaction_sessions

REPORT = REPO_ROOT / "docs" / "backtests" / "scans.md"
DETAIL = REPO_ROOT / "var" / "scans"
LOAD_START = date(2016, 1, 1)
FIRST_REPORTED = date(2017, 1, 1)
TUNING_END = date(2022, 12, 30)
SAMPLES = 12
SEED = 3_2


@dataclass(frozen=True, slots=True)
class Found:
    security: str
    symbol: str
    day: date
    setup: Setup
    close: float
    next_high: float | None
    """The next session's high, to see how often the pivot is reached the next day."""
    features: Features


@dataclass(slots=True)
class Ranked:
    """One session's selection: the pool, the top 40 and how many the cache re-reviews."""

    day: date
    regime: Regime
    pool: int
    eligible: int
    chosen: list[Strategy]
    reviews: int
    scores: list[float]


@dataclass(slots=True)
class ScanRun:
    sessions: list[date]
    members: list[int] = field(default_factory=list)
    found: list[Found] = field(default_factory=list)
    regimes: dict[date, Regime] = field(default_factory=dict)
    ranked: list[Ranked] = field(default_factory=list)
    components: dict[Strategy, Counter[str]] = field(default_factory=dict)
    """Sums of each component over the eligible setups in a funding regime, by strategy."""
    eligible: Counter[Strategy] = field(default_factory=Counter)
    exceptions: list[str] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)


def ranks_by_security(
    series: Mapping[str, SecuritySeries], members: Mapping[date, set[str]], risk: RiskConfig
) -> tuple[dict[str, list[float | None]], dict[str, list[float | None]]]:
    """Each security's RS rating and best momentum rank per bar, ranked within the session's
    universe (README §5.3). Non-members get None: they are never scanned."""
    lookbacks = risk.strategies.MOMENTUM_BREAKOUT.rank_lookbacks
    rs: dict[date, dict[str, float]] = defaultdict(dict)
    returns: dict[tuple[date, int], dict[str, float]] = defaultdict(dict)
    for key, s in series.items():
        closes = s.close
        for i, (d, score) in enumerate(
            zip(s.dates, rs_scores(closes, risk.trend_template), strict=True)
        ):
            if key not in members.get(d, ()):
                continue
            if score is not None:
                rs[d][key] = score
            for n in lookbacks:
                if i >= n:
                    returns[d, n][key] = closes[i] / closes[i - n] - 1
    rs_pct = {d: percentile_ranks(v) for d, v in rs.items()}
    mom_pct = {k: percentile_ranks(v) for k, v in returns.items()}
    rs_out: dict[str, list[float | None]] = {}
    mom_out: dict[str, list[float | None]] = {}
    for key, s in series.items():
        rs_out[key] = [rs_pct.get(d, {}).get(key) for d in s.dates]
        best: list[float | None] = []
        for d in s.dates:
            values = [r for n in lookbacks if (r := mom_pct.get((d, n), {}).get(key)) is not None]
            best.append(max(values) if values else None)
        mom_out[key] = best
    return rs_out, mom_out


def replay_regime(
    series: Mapping[str, SecuritySeries],
    indexes: Mapping[str, str],
    universe: Sequence[str],
    macro: Mapping[str, MacroSeries],
    sessions: Sequence[date],
    minutes: Mapping[date, int],
    risk: RiskConfig,
) -> dict[date, Regime]:
    """The live classifier from the second session, starting in `CORRECTION` (as the replay)."""
    data = LoadedData(
        sessions=list(sessions),
        session_minutes=dict(minutes),
        indexes={symbol: series[key].ticker_history() for symbol, key in indexes.items()},
        universe=[series[k].ticker_history() for k in universe if k in series],
        macro=dict(macro),
    )
    state: RegimeState | None = None
    out: dict[date, Regime] = {}
    for inputs in build_inputs(data, sessions[1:], risk.regime, risk.universe):
        result = classify(
            state, inputs, risk.regime, macro_stale_limit=risk.data.macro_stale_sessions
        )
        out[result.session] = result.regime
        state = result.state
    return out


def earnings_by_security(store: ObjectStore) -> dict[str, list[tuple[date, EarningsTiming]]]:
    table = get_table(store, table_key(EARNINGS))
    rows = [] if table is None else table.select(["security", "report_date", "timing"]).to_pylist()
    out: dict[str, list[tuple[date, EarningsTiming]]] = defaultdict(list)
    for row in rows:
        if row["timing"]:
            out[row["security"]].append((row["report_date"], EarningsTiming(row["timing"].lower())))
    return out


def fundamentals_by_security(store: ObjectStore) -> dict[str, list[tuple[date, Fundamentals]]]:
    """Each security's quarters by `filed` date, oldest first."""
    table = get_table(store, table_key(FUNDAMENTALS))
    columns = ["security", "filed_date", "eps_growth", "eps_acceleration"]
    rows = [] if table is None else table.select(columns).to_pylist()
    out: dict[str, list[tuple[date, Fundamentals]]] = defaultdict(list)
    for row in rows:
        out[row["security"]].append(
            (row["filed_date"], Fundamentals(row["eps_growth"], row["eps_acceleration"]))
        )
    for quarters in out.values():
        quarters.sort(key=lambda q: q[0])
    return out


def known_on(quarters: Sequence[tuple[date, Fundamentals]], day: date) -> Fundamentals | None:
    """The latest quarter filed before ``day`` (README §15: readable from the next session)."""
    i = bisect.bisect_left([q[0] for q in quarters], day)
    return quarters[i - 1][1] if i > 0 else None


def rank_sessions(run: ScanRun, risk: RiskConfig, cap: int) -> None:
    """Score and cap each session's pool, and replay the verdict cache over the selections:
    every selected setup that needs a review gets a verdict that session."""
    by_day: dict[date, list[Found]] = defaultdict(list)
    for f in run.found:
        by_day[f.day].append(f)
    position = {d: i for i, d in enumerate(run.sessions)}
    cache: dict[str, tuple[CachedVerdict, int]] = {}
    max_age = risk.ranking.verdict_max_age_sessions
    for day in run.sessions:
        regime = run.regimes.get(day)
        if regime is None:
            continue
        found = by_day.get(day, [])
        scored = score_pool([(f.setup, f.features) for f in found], risk)
        eligible = select(scored, regime, risk, cap=len(scored))
        chosen = eligible[:cap]
        for s in eligible:
            run.components.setdefault(s.setup.strategy, Counter()).update(s.components)
            run.eligible[s.setup.strategy] += 1
        reviews = 0
        for s in chosen:
            held = cache.get(s.setup.symbol)
            cached = None
            if held is not None:
                verdict, given = held
                cached = CachedVerdict(
                    verdict.symbol, verdict.strategy, verdict.structure, position[day] - given
                )
            if needs_review(s.setup, cached, max_age):
                reviews += 1
                cache[s.setup.symbol] = (
                    CachedVerdict(s.setup.symbol, s.setup.strategy, structure(s.setup), 0),
                    position[day],
                )
        run.ranked.append(
            Ranked(
                day,
                regime,
                len(found),
                len(eligible),
                [s.setup.strategy for s in chosen],
                reviews,
                [s.score for s in chosen],
            )
        )


def run_scans(
    store: ObjectStore,
    sessions: Sequence[date],
    minutes: Mapping[date, int],
    risk: RiskConfig,
    *,
    cap: int = 40,
    log: Callable[[str], None] = lambda _: None,
) -> ScanRun:
    clock = time.monotonic()
    run = ScanRun([d for d in sessions if d >= FIRST_REPORTED])

    def lap(name: str) -> None:
        nonlocal clock
        run.seconds[name] = round(time.monotonic() - clock, 1)
        clock = time.monotonic()
        log(f"{name}: {run.seconds[name]} s")

    securities = load_securities(store)
    members = load_membership(store, {d.year for d in sessions})
    ever = set().union(*(members.get(d, set()) for d in sessions))
    indexes = index_securities(securities, risk.regime.indexes)
    series = load_series(store, sessions, securities=ever | set(indexes.values()))
    last = sessions[-1]
    macro = {
        name: MacroSeries(
            [d for d in m.dates if d <= last],
            [v for d, v in zip(m.dates, m.values, strict=True) if d <= last],
        )
        for name, m in load_macro(store).items()
    }
    lap("load")
    run.regimes = replay_regime(series, indexes, sorted(ever), macro, sessions, minutes, risk)
    spells = correction_spells(run.regimes)
    earnings = earnings_by_security(store)
    quarters = fundamentals_by_security(store)
    spy = series[indexes["SPY"]]
    spy_close = dict(zip(spy.dates, spy.close, strict=True))
    lap("regime")
    rs, momentum = ranks_by_security(series, members, risk)
    lap("ranks")
    run.members = [len(members.get(d, ())) for d in run.sessions]
    strategies = build_strategies(risk)
    for key in sorted(ever & series.keys()):
        s = series[key]
        symbol = str(securities[key]["symbol"])
        try:
            bars = s.bars()
            benchmark = [spy_close.get(d) for d in s.dates]
            data = SymbolData.build(
                symbol,
                bars,
                rs_ratings=rs[key],
                momentum_ranks=momentum[key],
                risk=risk,
                benchmark_closes=None if None in benchmark else [c or 0.0 for c in benchmark],
                corrections=corrections_for(s.dates, spells),
                earnings_sessions=reaction_sessions(s.dates, earnings.get(key, [])),
            )
            own = quarters.get(key, [])
            for i, d in enumerate(s.dates):
                if d < FIRST_REPORTED or key not in members.get(d, ()):
                    continue
                for strategy in strategies.values():
                    setup = strategy.scan(data, i)
                    if setup is not None:
                        nxt = bars[i + 1].high if i + 1 < len(bars) else None
                        features = candidate_features(data, i, risk, fundamentals=known_on(own, d))
                        run.found.append(Found(key, symbol, d, setup, bars[i].close, nxt, features))
        except Exception as exc:
            run.exceptions.append(f"{key} ({symbol}): {type(exc).__name__}: {exc}")
    lap("scans")
    rank_sessions(run, risk, cap)
    lap("ranking")
    return run


# --- The report ---------------------------------------------------------------------------------

READING = "## Reading"


def kept_reading(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    at = text.find(READING)
    return text[at:].rstrip() + "\n" if at >= 0 else ""


def _q(values: Sequence[float], digits: int = 1, pct: bool = True) -> str:
    """Median and the 10th / 90th percentiles."""
    if not values:
        return "-"
    if len(values) < 10:
        return f"{statistics.median(values) * (100 if pct else 1):.{digits}f}"
    deciles = statistics.quantiles(values, n=10)
    scale = 100 if pct else 1
    unit = "%" if pct else ""
    return (
        f"{statistics.median(values) * scale:.{digits}f}{unit} "
        f"({deciles[0] * scale:.{digits}f}-{deciles[-1] * scale:.{digits}f})"
    )


def _stop_width(f: Found) -> float:
    return 1 - f.setup.structural_stop / f.setup.pivot


def render(run: ScanRun, risk: RiskConfig, reading: str) -> str:
    by: dict[Strategy, list[Found]] = defaultdict(list)
    for f in run.found:
        by[f.setup.strategy].append(f)
    years = sorted({d.year for d in run.sessions})
    order = [
        Strategy.VCP_BREAKOUT,
        Strategy.MOMENTUM_BREAKOUT,
        Strategy.EPISODIC_PIVOT,
        Strategy.LEADER_PULLBACK,
        Strategy.EMERGING_LEADER,
    ]
    lines = [
        "# Strategy scans on real data (tasks 3.2a, 3.2b)",
        "",
        f"Generated by `scripts/scan_real_data.py` on {datetime.now(UTC):%Y-%m-%d %H:%M} UTC, "
        f"`risk.yaml` {risk.short_hash}. Sessions {run.sessions[0]} to {run.sessions[-1]} "
        f"({len(run.sessions)}; 2016 is warm-up, the 2023+ holdout is not loaded). Universe "
        f"{min(run.members)}-{max(run.members)} members a session. Exceptions: "
        f"**{len(run.exceptions)}**. Run time: "
        + ", ".join(f"{k} {v:.0f} s" for k, v in run.seconds.items())
        + ".",
        "",
        "Every scan runs on every member every session, whatever the regime (the breakout "
        "tracker needs them all); the regime decides only what is fundable. A setup is a "
        "candidate for the **next** session; the same base usually shows up on several sessions "
        "in a row.",
        "",
        "## Setups per year",
        "",
        "| Strategy | "
        + " | ".join(str(y) for y in years)
        + " | Per session: median (max) | Symbols |",
        "|---|" + "---|" * len(years) + "---|---|",
    ]
    for strategy in order:
        found = by[strategy]
        per_year = Counter(f.day.year for f in found)
        per_session = Counter(f.day for f in found)
        counts = [per_session.get(d, 0) for d in run.sessions]
        lines.append(
            f"| `{strategy.value}` | "
            + " | ".join(f"{per_year.get(y, 0):,}" for y in years)
            + f" | {statistics.median(counts):.0f} ({max(counts)})"
            + f" | {len({f.security for f in found}):,} |"
        )
    regimes = list(reversed(Regime))  # CONFIRMED_UPTREND first
    spread = Counter(run.regimes.get(d) for d in run.sessions)
    lines += [
        "",
        "## Setups by regime",
        "",
        "Setups per session in each regime (the regime of the scan session, from the replay). "
        "Sessions: "
        + ", ".join(f"`{r.value}` {spread.get(r, 0)}" for r in regimes)
        + ". The regime decides what is fundable (README §6), not what is scanned.",
        "",
        "| Strategy | " + " | ".join(f"`{r.value}`" for r in regimes) + " |",
        "|---|" + "---|" * len(regimes),
    ]
    for strategy in order:
        hits = Counter(run.regimes.get(f.day) for f in by[strategy])
        lines.append(
            f"| `{strategy.value}` | "
            + " | ".join(f"{hits.get(r, 0) / max(1, spread.get(r, 0)):.1f}" for r in regimes)
            + " |"
        )
    vcp_keys = {(f.security, f.day) for f in by[Strategy.VCP_BREAKOUT]}
    both = sum((f.security, f.day) in vcp_keys for f in by[Strategy.MOMENTUM_BREAKOUT])
    lines += [
        "",
        f"Sessions where one stock is both a VCP and a momentum setup: {both:,}.",
        "",
        "## Levels",
        "",
        "Median (10th-90th percentile). The structural stop's width is measured from the pivot; "
        "the order-time limits (8% below the limit for VCP, 1 x ADR for momentum and 1.5 x the "
        "pre-gap ADR for EP, from the trigger) are applied by the guardrails (3.5b), not here. "
        '"Next-day reach": the next session\'s high reached the pivot.',
        "",
        "| Strategy | Stop width | Pivot above the close | Next-day reach |",
        "|---|---|---|---|",
    ]
    for strategy in order:
        found = by[strategy]
        reach = [f.next_high >= f.setup.pivot for f in found if f.next_high is not None]
        # An EP enters on its gap session, so the next session's reach doesn't apply.
        reached = (
            "n/a"
            if strategy is Strategy.EPISODIC_PIVOT
            else f"{(sum(reach) / len(reach) if reach else 0):.0%}"
        )
        lines.append(
            f"| `{strategy.value}` | {_q([_stop_width(f) for f in found])} | "
            f"{_q([f.setup.pivot / f.close - 1 for f in found])} | {reached} |"
        )
    vcp = [
        f.setup.detail for f in by[Strategy.VCP_BREAKOUT] if isinstance(f.setup.detail, VcpDetail)
    ]
    mom = [
        f.setup.detail
        for f in by[Strategy.MOMENTUM_BREAKOUT]
        if isinstance(f.setup.detail, MomentumDetail)
    ]
    ep = [
        f.setup.detail
        for f in by[Strategy.EPISODIC_PIVOT]
        if isinstance(f.setup.detail, EpisodicPivotDetail)
    ]
    pullbacks = [
        f.setup.detail
        for f in by[Strategy.LEADER_PULLBACK]
        if isinstance(f.setup.detail, PullbackDetail)
    ]
    emerging = [
        f.setup.detail
        for f in by[Strategy.EMERGING_LEADER]
        if isinstance(f.setup.detail, EmergingDetail)
    ]
    tested = Counter(d.tested_ema for d in pullbacks)
    contraction_mix = Counter(len(d.contractions) for d in vcp)
    lines += [
        "",
        "## What each scan found",
        "",
        "**`VcpBreakout`:** contractions "
        + ", ".join(f"{n}: {contraction_mix[n]:,}" for n in sorted(contraction_mix))
        + f"; base length {_q([float(d.base_sessions) for d in vcp], 0, pct=False)} sessions; "
        f"first contraction {_q([d.contractions[0].depth for d in vcp])}; final contraction "
        f"{_q([d.contractions[-1].depth for d in vcp])}; final volume ratio "
        f"{_q([d.final_volume_ratio for d in vcp], 2, pct=False)}.",
        "",
        "**`MomentumBreakout`:** consolidation "
        f"{_q([float(d.consolidation_sessions) for d in mom], 0, pct=False)} "
        f"sessions; prior move {_q([d.move for d in mom], 0)}; consolidation depth "
        f"{_q([d.depth for d in mom])}; second-half range vs first "
        f"{_q([d.second_half_range / d.first_half_range for d in mom], 2, pct=False)}.",
        "",
        f"**`EpisodicPivot` (proxy):** gap {_q([d.gap for d in ep])}; volume "
        f"{_q([d.volume_ratio for d in ep], 1, pct=False)} x; gap held into the close (close > "
        f"open) on {sum(d.day_gain > d.gap for d in ep) / max(1, len(ep)):.0%}; prior 6-month move "
        f"{_q([m for d in ep if (m := d.prior_moves.get(126)) is not None], 0)}.",
        "",
        "**`LeaderPullback`:** tested average "
        + ", ".join(f"{n}-day EMA: {tested[n]:,}" for n in sorted(tested))
        + f"; pullback {_q([float(d.pullback_sessions) for d in pullbacks], 0, pct=False)} "
        f"sessions, {_q([d.depth for d in pullbacks])} deep; volume ratio "
        f"{_q([d.volume_ratio for d in pullbacks], 2, pct=False)}.",
        "",
        "**`EmergingLeader`:** new 52-week high "
        f"{sum(d.new_price_high for d in emerging) / max(1, len(emerging)):.0%}, RS line at a "
        f"new high {sum(d.rs_line_new_high for d in emerging) / max(1, len(emerging)):.0%}; "
        f"drawdown vs SPY {_q([d.drawdown / d.index_drawdown for d in emerging], 2, pct=False)}; "
        "sessions since the correction "
        f"{_q([float(d.sessions_since_correction) for d in emerging], 0, pct=False)}.",
        "",
        *render_ranking(run, order, regimes),
        "## Samples for a visual check",
        "",
        f"{SAMPLES} random setups per strategy (seed {SEED}); check each against a daily chart.",
        "",
    ]
    rng = random.Random(SEED)  # noqa: S311 - a reproducible sample, not security
    for strategy in order:
        found = by[strategy]
        picked = sorted(
            rng.sample(found, min(SAMPLES, len(found))), key=lambda f: (f.day, f.symbol)
        )
        lines += [
            f"**`{strategy.value}`**",
            "",
            "| Session | Symbol | Close | Pivot | Stop | Detail |",
            "|---|---|---|---|---|---|",
        ]
        for f in picked:
            facts = f.setup.detail.facts()
            detail = ", ".join(f"{k} {v}" for k, v in facts.items() if k in _SAMPLE_FACTS)
            lines.append(
                f"| {f.day} | {f.symbol} | {f.close:.2f} | {f.setup.pivot:.2f} | "
                f"{f.setup.structural_stop:.2f} | {detail} |"
            )
        lines.append("")
    if run.exceptions:
        lines += ["## Exceptions", "", *(f"- {e}" for e in run.exceptions[:20]), ""]
    lines.append(reading or f"{READING}\n\n(to be written)\n")
    return "\n".join(lines)


def render_ranking(run: ScanRun, order: Sequence[Strategy], regimes: Sequence[Regime]) -> list[str]:
    """The 3.3 selection: how full the cap is, who fills it, and what the cache saves."""
    lines = [
        "## Ranking and the cap (3.3)",
        "",
        "Per session, medians. *Pool*: every setup scanned. *Eligible*: fundable in the "
        "session's regime, not too extended, one per ticker. *Chosen*: the top 40 by quality "
        "score. *Reviews*: the chosen setups the verdict cache sends to the Analyst (new, "
        "changed structure, or a verdict over 3 sessions old). `CORRECTION` funds nothing.",
        "",
        "| Regime | Sessions | Pool | Eligible | Chosen | Reviews | "
        + " | ".join(f"`{s.value}`" for s in order)
        + " |",
        "|---|---|---|---|---|---|" + "---|" * len(order),
    ]
    for regime in regimes:
        rows = [r for r in run.ranked if r.regime is regime]
        if not rows:
            continue
        chosen = sum(len(r.chosen) for r in rows)
        mix = Counter(s for r in rows for s in r.chosen)
        lines.append(
            f"| `{regime.value}` | {len(rows)} | "
            f"{statistics.median(r.pool for r in rows):.0f} | "
            f"{statistics.median(r.eligible for r in rows):.0f} | "
            f"{statistics.median(len(r.chosen) for r in rows):.0f} | "
            f"{statistics.median(r.reviews for r in rows):.0f} | "
            + " | ".join(f"{mix.get(s, 0) / max(1, chosen):.0%}" for s in order)
            + " |"
        )
    lines += [
        "",
        "Average component scores of the eligible setups, by strategy (all funding regimes):",
        "",
        "| Strategy | Eligible | " + " | ".join(COMPONENTS) + " | Score |",
        "|---|---|" + "---|" * (len(COMPONENTS) + 1),
    ]
    for strategy in order:
        n = run.eligible.get(strategy, 0)
        if not n:
            continue
        sums = run.components[strategy]
        means = [sums[c] / n for c in COMPONENTS]
        lines.append(
            f"| `{strategy.value}` | {n:,} | "
            + " | ".join(f"{m:.2f}" for m in means)
            + f" | {sum(means) / len(means):.2f} |"
        )
    chosen_all = sum(len(r.chosen) for r in run.ranked)
    reviews_all = sum(r.reviews for r in run.ranked)
    scores = [x for r in run.ranked for x in r.scores]
    lines += [
        "",
        f"The cache re-reviews {reviews_all:,} of {chosen_all:,} chosen setups "
        f"({reviews_all / max(1, chosen_all):.0%}); the rest keep their verdict. Quality score "
        f"of the chosen: {_q(scores, 2, pct=False)}.",
        "",
    ]
    return lines


_SAMPLE_FACTS = {
    "contraction_depths",
    "base_sessions",
    "final_volume_ratio",
    "consolidation_sessions",
    "prior_move",
    "consolidation_depth",
    "gap",
    "volume_ratio",
    "tested_ema",
    "pullback_depth",
    "pullback_volume_ratio",
    "drawdown_vs_spy",
    "sessions_since_correction",
    "new_52_week_high",
}


def write_detail(run: ScanRun, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "setups.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["session", "symbol", "security", "strategy", "close", "pivot", "stop", "facts"]
        )
        for f in run.found:
            writer.writerow(
                [
                    f.day,
                    f.symbol,
                    f.security,
                    f.setup.strategy.value,
                    f.close,
                    f.setup.pivot,
                    f.setup.structural_stop,
                    dict(f.setup.detail.facts()),
                ]
            )


def main(argv: Sequence[str] | None = None) -> int:
    from auratrade.calendar.sessions import load_calendar
    from auratrade.db.engine import engine_from_settings
    from auratrade.jobs.wiring import load_runtime_settings
    from auratrade.storage import make_store

    parser = argparse.ArgumentParser(prog="scripts/scan_real_data.py")
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    settings = load_runtime_settings()
    risk = load_risk_config(settings.risk_config_path)
    engine = engine_from_settings(settings)
    try:
        with engine.connect() as conn:
            calendar = load_calendar(conn)
    finally:
        engine.dispose()
    days = calendar.sessions_between(max(LOAD_START, calendar.first or LOAD_START), TUNING_END)
    run = run_scans(
        make_store(settings),
        [s.day for s in days],
        {s.day: s.minutes for s in days},
        risk,
        log=lambda line: print(line, file=sys.stderr),
    )
    write_detail(run, DETAIL)
    args.report.write_text(render(run, risk, kept_reading(args.report)), encoding="utf-8")
    print(
        f"wrote {args.report}; setups {len(run.found)}; exceptions {len(run.exceptions)}",
        file=sys.stderr,
    )
    return 1 if run.exceptions else 0


if __name__ == "__main__":
    sys.exit(main())
