"""Ranking and the cap (archived README §5.3 "Ranking and cap"; task 3.3, #138).

Every setup gets a **quality score**: the plain average of eight components, each scaled to
0-1 (the user's choice: equal weights, nothing fitted). A component without data scores
``missing_component`` (neutral).

| Component | Score |
|---|---|
| RS rating | rating / 100 |
| Momentum rank | the best 1/3/6-month percentile / 100 |
| Group RS rank | the industry group's percentile / 100 |
| RS line | 1 at a new 52-week high, 0.5 within 5% of it, 0 otherwise |
| Up/down volume | 1 at a ratio >= 1.0 (accumulation), else 0 |
| Tightness | stop width / ADR, ranked within the session's pool: the tightest scores 1 |
| Base count | 1st base 1, 2nd 2/3, 3rd or an unreliable count 1/3, 4th+ 0 (archived README §8,
#126) |
| Catalyst | the share of: a recent earnings gap-up, EPS growth >= 25%, accelerating EPS |

**The earnings surprise is not in the score**: it has no history, and the backtested pool
must be the pool the Analyst gets (archived README §5.3).

**Selection** (``select``): only setups of strategies fundable in the regime compete; a stock
more than 25% above its 50-day is excluded, except momentum and EP setups; each ticker takes
one slot, with its best-scoring setup (the user's choice). Then the fundable strategies **take
turns** (the user's choice, #138): each round, every strategy with setups left gives its best
remaining one, in score order, until ``cap`` are chosen. With one ranking, pullbacks (numerous,
RS >= 80, tight stops) took 81% of the slots in uptrends and VCPs 12%.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from auratrade.config import RankingConfig, Regime, RiskConfig, Strategy
from auratrade.indicators.earnings import latest_earnings_gap
from auratrade.indicators.relative_strength import percentile_ranks
from auratrade.indicators.volume import up_down_volume_ratio
from auratrade.strategies.base import Setup, SymbolData
from auratrade.strategies.registry import fundable

EXTENSION_EXEMPT = frozenset({Strategy.MOMENTUM_BREAKOUT, Strategy.EPISODIC_PIVOT})
COMPONENTS = (
    "rs_rating",
    "momentum_rank",
    "group_rs",
    "rs_line",
    "up_down",
    "tightness",
    "base_count",
    "catalyst",
)


@dataclass(frozen=True, slots=True)
class Fundamentals:
    """The latest quarter filed before the scan session (archived README §15: by EDGAR `filed`
    date)."""

    eps_growth: float | None
    eps_acceleration: float | None


@dataclass(frozen=True, slots=True)
class Features:
    """The candidate's features at the scan session, as archived README §5.3 lists them."""

    rs_rating: float | None
    momentum_rank: float | None
    group_rs: float | None
    """The industry group's RS percentile (``indicators.relative_strength.group_rs_ranks``)."""
    rs_line_new_high: bool | None
    rs_line_near_high: bool | None
    up_down_ratio: float | None
    adr: float | None
    base_stage: int | None
    base_reliable: bool
    earnings_gap: bool | None
    """A recent earnings gap-up; ``None`` when the symbol has no earnings dates at all."""
    eps_growth: float | None
    eps_acceleration: float | None
    extension: float | None
    """Close / the 50-day SMA - 1."""


def candidate_features(
    data: SymbolData,
    at: int,
    risk: RiskConfig,
    *,
    group_rs: float | None = None,
    fundamentals: Fundamentals | None = None,
    base_stage: int | None = None,
    base_reliable: bool = False,
) -> Features:
    """The features of ``data`` at session ``at``. The cross-sectional group rank, the
    point-in-time fundamentals and the base count (``indicators.bases``) come from the caller."""
    cfg = risk.ranking
    bars = data.bars
    new_high, near_high = _rs_line_highs(
        data, at, cfg.rs_line.high_window, cfg.rs_line.near_tolerance
    )
    up_down = data.cached(
        "up_down", cfg.up_down_sessions, lambda: up_down_volume_ratio(bars, cfg.up_down_sessions)
    )[at]
    sma = data.sma(cfg.extension_sma)[at]
    earnings_gap: bool | None = None
    if data.earnings_sessions:
        c = cfg.catalyst
        earnings_gap = (
            latest_earnings_gap(
                bars,
                data.earnings_sessions,
                at,
                lookback=c.gap_lookback_sessions,
                min_gap=c.min_gap,
                volume_window=c.volume_sessions,
                min_volume_ratio=c.min_volume_ratio,
            )
            is not None
        )
    return Features(
        rs_rating=data.rs_ratings[at],
        momentum_rank=data.momentum_ranks[at],
        group_rs=group_rs,
        rs_line_new_high=new_high,
        rs_line_near_high=near_high,
        up_down_ratio=up_down,
        adr=data.adr(cfg.adr_sessions)[at],
        base_stage=base_stage,
        base_reliable=base_reliable,
        earnings_gap=earnings_gap,
        eps_growth=None if fundamentals is None else fundamentals.eps_growth,
        eps_acceleration=None if fundamentals is None else fundamentals.eps_acceleration,
        extension=None if sma is None or sma <= 0 else bars[at].close / sma - 1,
    )


def _rs_line_highs(
    data: SymbolData, at: int, window: int, tolerance: float
) -> tuple[bool | None, bool | None]:
    benchmark = data.benchmark_closes
    if benchmark is None or at < window - 1:
        return None, None
    line = [data.bars[i].close / benchmark[i] for i in range(at - window + 1, at + 1)]
    high = max(line)
    return line[-1] >= high, line[-1] >= high * (1 - tolerance)


@dataclass(frozen=True, slots=True)
class Scored:
    setup: Setup
    features: Features
    components: Mapping[str, float]
    score: float


def _tightness(setup: Setup, features: Features) -> float | None:
    if features.adr is None or features.adr <= 0:
        return None
    return (1 - setup.structural_stop / setup.pivot) / features.adr


def components(
    features: Features, tightness_rank: float | None, cfg: RankingConfig
) -> dict[str, float]:
    """The eight components, each 0-1. ``tightness_rank`` is the setup's percentile of stop
    width / ADR within the session's pool (0 = the tightest)."""
    missing = cfg.missing_component

    def known(value: float | None) -> float:
        return missing if value is None else value

    if features.base_stage is None:
        base = missing
    elif features.base_stage >= 4:
        base = 0.0
    elif not features.base_reliable:
        base = min(1 / 3, (4 - features.base_stage) / 3)
    else:
        base = (4 - features.base_stage) / 3
    if features.rs_line_new_high is None:
        rs_line = missing
    else:
        rs_line = 1.0 if features.rs_line_new_high else 0.5 if features.rs_line_near_high else 0.0
    catalyst_parts = [
        part
        for part in (
            features.earnings_gap,
            None
            if features.eps_growth is None
            else features.eps_growth >= cfg.catalyst.min_eps_growth,
            None if features.eps_acceleration is None else features.eps_acceleration > 0,
        )
        if part is not None
    ]
    return {
        "rs_rating": known(None if features.rs_rating is None else features.rs_rating / 100),
        "momentum_rank": known(
            None if features.momentum_rank is None else features.momentum_rank / 100
        ),
        "group_rs": known(None if features.group_rs is None else features.group_rs / 100),
        "rs_line": rs_line,
        "up_down": known(
            None if features.up_down_ratio is None else float(features.up_down_ratio >= 1.0)
        ),
        "tightness": known(None if tightness_rank is None else 1 - tightness_rank / 100),
        "base_count": base,
        "catalyst": sum(catalyst_parts) / len(catalyst_parts) if catalyst_parts else missing,
    }


def score_pool(candidates: Sequence[tuple[Setup, Features]], risk: RiskConfig) -> list[Scored]:
    """Score one session's setups. Tightness is ranked within this pool, so pass the whole
    session's pool (every strategy), before any selection."""
    ratios = {
        i: ratio
        for i, (setup, features) in enumerate(candidates)
        if (ratio := _tightness(setup, features)) is not None
    }
    ranks = percentile_ranks(ratios) if ratios else {}
    out = []
    for i, (setup, features) in enumerate(candidates):
        parts = components(features, ranks.get(i), risk.ranking)
        out.append(Scored(setup, features, parts, sum(parts.values()) / len(parts)))
    return out


def too_extended(scored: Scored, risk: RiskConfig) -> bool:
    """More than 25% above the 50-day (archived README §5.3); momentum and EP setups are exempt. An
    unknown extension excludes (fail closed)."""
    if scored.setup.strategy in EXTENSION_EXEMPT:
        return False
    extension = scored.features.extension
    return extension is None or extension > risk.guardrails.max_above_50d


def select(scored: Sequence[Scored], regime: Regime, risk: RiskConfig, cap: int) -> list[Scored]:
    """The Analyst's candidates: fundable, not too extended, one per ticker, the strategies
    taking turns; in the order chosen."""
    funded = {f.strategy for f in fundable(risk, regime)}
    best: dict[str, Scored] = {}
    for s in scored:
        if s.setup.strategy not in funded or too_extended(s, risk):
            continue
        held = best.get(s.setup.symbol)
        if held is None or _key(s) < _key(held):
            best[s.setup.symbol] = s
    queues: dict[Strategy, list[Scored]] = {}
    for s in sorted(best.values(), key=_key):
        queues.setdefault(s.setup.strategy, []).append(s)
    chosen: list[Scored] = []
    depth = 0
    while len(chosen) < cap:
        heads = sorted((q[depth] for q in queues.values() if depth < len(q)), key=_key)
        if not heads:
            break
        chosen.extend(heads[: cap - len(chosen)])
        depth += 1
    return chosen


_ORDER = {strategy: i for i, strategy in enumerate(Strategy)}


def _key(s: Scored) -> tuple[float, str, int]:
    return (-s.score, s.setup.symbol, _ORDER[s.setup.strategy])
