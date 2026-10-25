"""The Analyst verdict scorecard (archived README §5.12 "Counting honestly", #73), pure.

Every verdict (BUY, WATCH, SKIP) is played forward under its strategy's default entry policy and
exit plan, as if it had been taken. Verdicts aren't independent observations, so:

- **One unit per setup**: (ticker, strategy, structure), the structure being what the verdict
  cache keys a re-review on (``verdict_cache.structure``: the code pivot to the cent, a
  pullback's tested average). Re-reviewing a setup doesn't add observations.
- **In force:** a verdict governs the trading sessions whose previous evening it was still
  cached: its age at that evening is at most ``verdict_max_age_sessions`` (the verdict cache's
  window, the same for every verdict). A unit triggers only on such a session.
- **Label:** the verdict in force when the unit triggered, or its last verdict if it never did.
  Each unit carries that verdict's prompt version and answering model.
- **Triggered** means the simulated entry filled. BUY vs SKIP expectancy is compared on
  triggered units; trigger rates are reported separately.
- **Confidence intervals** use a block bootstrap over trigger sessions: whole sessions are
  resampled, with the units that triggered on them.
"""

import math
import random
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from auratrade.config import Strategy

type UnitKey = tuple[str, Strategy, float]
"""(symbol, strategy, structure)."""


class Verdict(StrEnum):
    BUY = "BUY"
    WATCH = "WATCH"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class Review:
    """One Analyst verdict on a setup, given on the evening of ``session``."""

    symbol: str
    strategy: Strategy
    structure: float
    session: date
    verdict: Verdict
    conviction: int | None
    prompt_version: str
    model: str

    @property
    def unit(self) -> UnitKey:
        return (self.symbol, self.strategy, self.structure)


def units(reviews: Iterable[Review]) -> dict[UnitKey, list[Review]]:
    """Reviews grouped by unit, oldest first."""
    out: dict[UnitKey, list[Review]] = defaultdict(list)
    for r in reviews:
        out[r.unit].append(r)
    for rs in out.values():
        rs.sort(key=lambda r: r.session)
    return dict(out)


def in_force(
    reviews: Sequence[Review], session: date, position: Mapping[date, int], max_age: int
) -> Review | None:
    """The verdict governing trading session ``session``: the latest given on an earlier
    evening, if it was at most ``max_age`` sessions old on the evening before ``session``."""
    at = position[session]
    given = [r for r in reviews if position[r.session] < at]
    if not given:
        return None
    latest = given[-1]
    return latest if at - 1 - position[latest.session] <= max_age else None


@dataclass(frozen=True, slots=True)
class Observation:
    """One unit, as the statistics read it."""

    unit: UnitKey
    label: Review
    """The verdict in force at the trigger, or the last verdict."""
    triggered: bool
    trigger_session: date | None
    r: Mapping[int, float | None]
    """R after each horizon (5, 10, 20 sessions); None while the data hasn't reached it."""

    @property
    def verdict(self) -> Verdict:
        return self.label.verdict


def observation(
    reviews: Sequence[Review],
    *,
    trigger_session: date | None,
    position: Mapping[date, int],
    max_age: int,
    r: Mapping[int, float | None] | None = None,
) -> Observation:
    """A unit's observation; ``trigger_session`` must fall while a verdict is in force."""
    if not reviews:
        raise ValueError("a unit needs at least one review")
    if trigger_session is None:
        return Observation(reviews[0].unit, reviews[-1], False, None, r or {})
    label = in_force(reviews, trigger_session, position, max_age)
    if label is None:
        raise ValueError(f"no verdict in force on {trigger_session}")
    return Observation(reviews[0].unit, label, True, trigger_session, r or {})


# ---- statistics ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class Summary:
    """One group's figures at one horizon."""

    setups: int
    triggered: int
    trigger_rate: float | None
    scored: int
    """Triggered units with an R at the horizon."""
    sessions: int
    """Distinct trigger sessions behind ``scored``."""
    expectancy: float | None
    """The mean R of the scored units."""
    interval: Interval | None


@dataclass(frozen=True, slots=True)
class Comparison:
    """``a`` minus ``b`` (e.g. BUY - SKIP) on triggered units at one horizon."""

    a: Summary
    b: Summary
    difference: float | None
    interval: Interval | None
    sessions: int
    """Distinct trigger sessions behind both groups."""


def _scored(obs: Iterable[Observation], horizon: int) -> list[tuple[date, float]]:
    out = []
    for o in obs:
        value = o.r.get(horizon)
        if o.triggered and o.trigger_session is not None and value is not None:
            out.append((o.trigger_session, value))
    return out


def _by_session(scored: Sequence[tuple[date, float]]) -> dict[date, list[float]]:
    out: dict[date, list[float]] = defaultdict(list)
    for day, value in scored:
        out[day].append(value)
    return dict(out)


def block_bootstrap(
    groups: Sequence[Mapping[date, Sequence[float]]],
    statistic: Callable[[Sequence[Sequence[float]]], float | None],
    *,
    samples: int,
    confidence: float,
    seed: int,
    block_of: Callable[[date], int] | None = None,
) -> Interval | None:
    """A percentile interval for ``statistic`` over ``groups`` (each a group's values by
    trigger session), resampling whole sessions with replacement from the union of their
    sessions. ``block_of`` resamples whole blocks of sessions instead (each session's block
    number), every value of a block's sessions together. Resamples where the statistic is
    undefined are dropped; None if all are."""
    sessions = sorted(set().union(*(g.keys() for g in groups)))
    if not sessions:
        return None
    members: dict[int, list[date]] = defaultdict(list)
    if block_of is not None:
        for day in sessions:
            members[block_of(day)].append(day)
    blocks = sorted(members)
    rng = random.Random(seed)  # noqa: S311 - resampling, not crypto
    values = []
    for _ in range(samples):
        if block_of is None:
            drawn = rng.choices(sessions, k=len(sessions))
        else:
            drawn = [d for b in rng.choices(blocks, k=len(blocks)) for d in members[b]]
        resampled = [[v for day in drawn for v in g.get(day, ())] for g in groups]
        value = statistic(resampled)
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    tail = (1 - confidence) / 2
    return Interval(_quantile(values, tail), _quantile(values, 1 - tail))


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """The nearest-rank quantile (no interpolation)."""
    i = min(max(math.ceil(q * len(sorted_values)) - 1, 0), len(sorted_values) - 1)
    return sorted_values[i]


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def summarize(
    obs: Sequence[Observation], horizon: int, *, samples: int, confidence: float, seed: int
) -> Summary:
    triggered = sum(o.triggered for o in obs)
    scored = _scored(obs, horizon)
    by_session = _by_session(scored)
    interval = block_bootstrap(
        [by_session],
        lambda groups: _mean(groups[0]),
        samples=samples,
        confidence=confidence,
        seed=seed,
    )
    return Summary(
        setups=len(obs),
        triggered=triggered,
        trigger_rate=triggered / len(obs) if obs else None,
        scored=len(scored),
        sessions=len(by_session),
        expectancy=_mean([v for _, v in scored]),
        interval=interval,
    )


def compare(
    a: Sequence[Observation],
    b: Sequence[Observation],
    horizon: int,
    *,
    samples: int,
    confidence: float,
    seed: int,
) -> Comparison:
    """``a`` vs ``b`` (e.g. BUY vs SKIP units of the same strategies and regimes)."""
    sa, sb = _by_session(_scored(a, horizon)), _by_session(_scored(b, horizon))

    def difference(groups: Sequence[Sequence[float]]) -> float | None:
        ma, mb = _mean(groups[0]), _mean(groups[1])
        return None if ma is None or mb is None else ma - mb

    summary_a = summarize(a, horizon, samples=samples, confidence=confidence, seed=seed)
    summary_b = summarize(b, horizon, samples=samples, confidence=confidence, seed=seed)
    point = (
        None
        if summary_a.expectancy is None or summary_b.expectancy is None
        else summary_a.expectancy - summary_b.expectancy
    )
    interval = block_bootstrap(
        [sa, sb], difference, samples=samples, confidence=confidence, seed=seed
    )
    return Comparison(
        a=summary_a,
        b=summary_b,
        difference=point,
        interval=interval,
        sessions=len(sa.keys() | sb.keys()),
    )


def by_verdict(obs: Iterable[Observation]) -> dict[Verdict, list[Observation]]:
    out: dict[Verdict, list[Observation]] = {v: [] for v in Verdict}
    for o in obs:
        out[o.verdict].append(o)
    return out


def by_tag(obs: Iterable[Observation]) -> dict[tuple[str, str], list[Observation]]:
    """Units by (prompt version, answering model): a prompt change or a fallback answer is
    reported apart, never blended in."""
    out: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for o in obs:
        out[o.label.prompt_version, o.label.model].append(o)
    return dict(out)
