"""The momentum book's numbers and report (task 3.13), pure: everything here reads simulated
runs and price series and decides nothing the pre-registration didn't fix
(`docs/backtests/momentum-preregistration.md` §5-§7).

- **Metrics** on daily closes: CAGR = (end / start)^(252 / sessions) - 1, volatility, max
  drawdown, CAGR / max drawdown, excess CAGR (book CAGR - benchmark CAGR); per calendar year.
- **Intervals** (reported, gate nothing): `journal.scorecard.block_bootstrap` over non-overlapping
  blocks of sessions from the first one, statistic = the mean daily value x 252.
- **The gate** G1-G4 and the holdout's H1-H2; **the adoption rule** for C1-C7 (§6).
- **References:** the equal-weighted ranked set, E to E, price only, no costs; the signal check,
  the monthly rank correlation between the signal on R and the return from E to the next E.
- **The blind counts** (§7) and their stop conditions, with no equity, return or drawdown in them.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from itertools import pairwise

from auratrade.config import BookBacktestConfig, Regime
from auratrade.guardrails.blocks import EntryBlock
from auratrade.indicators.relative_strength import percentile_ranks
from auratrade.journal.scorecard import Interval, block_bootstrap
from auratrade.sim.book import BookRun, ExitReason, RebalanceLog
from auratrade.strategies.momentum_book import SkipReason

TRADING_DAYS = 252
OVERLAY = frozenset({EntryBlock.CORRECTION, EntryBlock.CRISIS_OVERRIDE, EntryBlock.INDEX_TREND})


# --- curves and metrics --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Curve:
    """Daily closes of an account or an index, oldest first."""

    dates: tuple[date, ...]
    values: tuple[float, ...]

    def returns(self) -> list[float]:
        return [b / a - 1 for a, b in pairwise(self.values)]


def of_run(run: BookRun) -> Curve:
    return Curve(tuple(m.day for m in run.marks), tuple(m.equity for m in run.marks))


def aligned(closes: Mapping[date, float], dates: Sequence[date]) -> Curve:
    """``closes`` on ``dates``, a missing session carrying the close before it."""
    out: list[float] = []
    last: float | None = None
    for d in dates:
        last = closes.get(d, last)
        if last is None:
            raise ValueError(f"no benchmark close on or before {d}")
        out.append(last)
    return Curve(tuple(dates), tuple(out))


def cagr(values: Sequence[float]) -> float:
    sessions = len(values) - 1
    if sessions < 1 or values[0] <= 0:
        return 0.0
    return float((values[-1] / values[0]) ** (TRADING_DAYS / sessions) - 1)


def max_drawdown(values: Iterable[float]) -> float:
    peak, deepest = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            deepest = max(deepest, 1 - v / peak)
    return deepest


def annualized_stdev(daily: Sequence[float]) -> float:
    return statistics.stdev(daily) * math.sqrt(TRADING_DAYS) if len(daily) > 1 else 0.0


@dataclass(frozen=True, slots=True)
class Performance:
    cagr: float
    volatility: float
    max_drawdown: float

    @property
    def calmar(self) -> float | None:
        """CAGR / max drawdown; None without a drawdown."""
        return self.cagr / self.max_drawdown if self.max_drawdown > 0 else None


def performance(curve: Curve) -> Performance:
    return Performance(
        cagr(curve.values), annualized_stdev(curve.returns()), max_drawdown(curve.values)
    )


@dataclass(frozen=True, slots=True)
class YearRow:
    year: int
    book: float
    spy: float
    mtum: float

    @property
    def excess(self) -> float:
        return self.book - self.spy


def _year_returns(curve: Curve) -> dict[int, float]:
    """Each calendar year's return, from the last close of the year before (or the first)."""
    out: dict[int, float] = {}
    base = curve.values[0]
    for i, d in enumerate(curve.dates):
        last_of_year = i + 1 == len(curve.dates) or curve.dates[i + 1].year != d.year
        if last_of_year and i > 0:
            out[d.year] = curve.values[i] / base - 1
            base = curve.values[i]
    return out


def by_year(book: Curve, spy: Curve, mtum: Curve) -> list[YearRow]:
    b, s, m = _year_returns(book), _year_returns(spy), _year_returns(mtum)
    return [YearRow(y, b[y], s[y], m[y]) for y in sorted(b)]


# --- intervals ---------------------------------------------------------------------------


def differences(a: Curve, b: Curve) -> dict[date, float]:
    """Daily return of ``a`` minus ``b`` on their common sessions, by the return's session."""
    if a.dates != b.dates:
        raise ValueError("the curves must share their sessions")
    return {d: ra - rb for d, ra, rb in zip(a.dates[1:], a.returns(), b.returns(), strict=True)}


def annual_interval(daily: Mapping[date, float], cfg: BookBacktestConfig) -> Interval | None:
    """The 90% interval of the mean daily value x 252, resampling whole blocks of sessions."""
    days = sorted(daily)
    block = {d: i // cfg.bootstrap_block_sessions for i, d in enumerate(days)}

    def statistic(groups: Sequence[Sequence[float]]) -> float | None:
        values = groups[0]
        return statistics.fmean(values) * TRADING_DAYS if values else None

    return block_bootstrap(
        [{d: [v] for d, v in daily.items()}],
        statistic,
        samples=cfg.bootstrap_samples,
        confidence=cfg.confidence,
        seed=cfg.seed,
        block_of=block.__getitem__,
    )


def min_detectable(daily: Mapping[date, float]) -> float:
    """1.645 x the annualized tracking error / sqrt(years)."""
    values = list(daily.values())
    years = len(values) / TRADING_DAYS
    return 1.645 * annualized_stdev(values) / math.sqrt(years) if years > 0 else math.inf


# --- the gate ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Gate:
    excess_spy: float
    excess_mtum: float
    positive_years: int
    without_best: float
    """The excess over SPY with the best year left out, compounding the other years."""
    calmar: float | None
    spy_calmar: float | None
    max_drawdown: float
    cfg: BookBacktestConfig

    @property
    def g1(self) -> bool:
        return self.excess_spy > 0

    @property
    def g2(self) -> bool:
        return self.excess_mtum >= 0

    @property
    def g3(self) -> bool:
        return self.positive_years >= self.cfg.min_positive_years and self.without_best > 0

    @property
    def g4(self) -> bool:
        ratio_ok = self.calmar is not None and (
            self.spy_calmar is None or self.calmar >= self.spy_calmar
        )
        return ratio_ok and self.max_drawdown <= self.cfg.max_drawdown

    @property
    def passed(self) -> bool:
        return self.g1 and self.g2 and self.g3 and self.g4


def gate(book: Curve, spy: Curve, mtum: Curve, cfg: BookBacktestConfig) -> Gate:
    b, s, m = performance(book), performance(spy), performance(mtum)
    years = by_year(book, spy, mtum)
    best = max(years, key=lambda y: y.excess) if years else None
    rest = [y for y in years if y is not best]
    without_best = math.prod(1 + y.book for y in rest) - math.prod(1 + y.spy for y in rest)
    return Gate(
        excess_spy=b.cagr - s.cagr,
        excess_mtum=b.cagr - m.cagr,
        positive_years=sum(y.excess > 0 for y in years),
        without_best=without_best,
        calmar=b.calmar,
        spy_calmar=s.calmar,
        max_drawdown=b.max_drawdown,
        cfg=cfg,
    )


# --- the comparisons ---------------------------------------------------------------------


class Kind(StrEnum):
    CHANGE = "CHANGE"
    """C1, C2, C4, C6: adopted if better beyond the interval with no deeper drawdown."""
    REMOVE_SAFETY = "REMOVE_SAFETY"
    """C3, C7: the same test; otherwise the safety rule stays."""
    ADD_SAFETY = "ADD_SAFETY"
    """C5: adopted if its drawdown is shallower and the interval isn't wholly below zero."""


@dataclass(frozen=True, slots=True)
class Judgement:
    name: str
    kind: Kind
    mean: float
    """The mean daily return difference (variant - primary) x 252."""
    interval: Interval | None
    max_drawdown: float
    primary_drawdown: float

    @property
    def passed(self) -> bool:
        if self.interval is None:
            return False
        if self.kind is Kind.ADD_SAFETY:
            return self.max_drawdown < self.primary_drawdown and self.interval.high >= 0
        return self.interval.low > 0 and self.max_drawdown <= self.primary_drawdown


def judge(
    name: str, kind: Kind, variant: Curve, primary: Curve, cfg: BookBacktestConfig
) -> Judgement:
    diff = differences(variant, primary)
    return Judgement(
        name,
        kind,
        statistics.fmean(diff.values()) * TRADING_DAYS if diff else 0.0,
        annual_interval(diff, cfg),
        max_drawdown(variant.values),
        max_drawdown(primary.values),
    )


EXCLUSIVE = ("C3", "C4")
"""Two overlays: if both pass, the larger mean difference is adopted."""


def adopted(judgements: Sequence[Judgement]) -> list[str]:
    passed = {j.name: j for j in judgements if j.passed}
    if all(n in passed for n in EXCLUSIVE):
        loser = min(EXCLUSIVE, key=lambda n: passed[n].mean)
        del passed[loser]
    return [j.name for j in judgements if j.name in passed]


# --- references --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Holding:
    """One ranked name over one rebalance period: its signal on R and its price relative from
    E's close to the next E's (the last close before, for a name that stopped trading)."""

    signal: float
    relative: float


@dataclass(frozen=True, slots=True)
class Period:
    execution: date
    until: date
    names: Mapping[str, Holding]


def equal_weight(periods: Sequence[Period], start: date, value: float = 1.0) -> Curve:
    """The equal-weighted ranked set, compounded period by period (from ``start``'s close, in
    cash until the first E)."""
    dates, values = [start], [value]
    for p in periods:
        if p.names:
            value *= statistics.fmean(h.relative for h in p.names.values())
        dates.append(p.until)
        values.append(value)
    return Curve(tuple(dates), tuple(values))


@dataclass(frozen=True, slots=True)
class SignalCheck:
    months: int
    mean: float | None
    t: float | None


def spearman(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    a = percentile_ranks(dict(enumerate(x for x, _ in pairs)))
    b = percentile_ranks(dict(enumerate(y for _, y in pairs)))
    xs, ys = [a[i] for i in range(len(pairs))], [b[i] for i in range(len(pairs))]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else None


def signal_check(periods: Sequence[Period]) -> SignalCheck:
    values = [
        c
        for p in periods
        if (c := spearman([(h.signal, h.relative) for h in p.names.values()])) is not None
    ]
    if not values:
        return SignalCheck(0, None, None)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    t = mean / (sd / math.sqrt(len(values))) if sd > 0 else None
    return SignalCheck(len(values), mean, t)


@dataclass(frozen=True, slots=True)
class RegimeRow:
    regime: Regime | None
    sessions: int
    book: float
    spy: float
    """Mean daily returns x 252."""

    @property
    def excess(self) -> float:
        return self.book - self.spy


def by_regime(book: Curve, spy: Curve, regimes: Mapping[date, Regime]) -> list[RegimeRow]:
    """Daily returns grouped by the regime computed the evening before each session."""
    groups: dict[Regime | None, list[tuple[float, float]]] = defaultdict(list)
    returns = zip(book.dates, book.returns(), spy.returns(), strict=False)
    for prev, rb, rs in returns:
        groups[regimes.get(prev)].append((rb, rs))
    order = [None, *Regime]
    return [
        RegimeRow(
            r,
            len(groups[r]),
            statistics.fmean(x for x, _ in groups[r]) * TRADING_DAYS,
            statistics.fmean(y for _, y in groups[r]) * TRADING_DAYS,
        )
        for r in order
        if groups.get(r)
    ]


# --- activity ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Activity:
    buys_per_year: float
    slippage_per_year: float
    """Dollars paid to slippage a year, as a fraction of the starting equity."""
    mean_holding_sessions: float | None
    mean_cash_share: float
    stop_outs_per_year: float
    largest_weight: float


def holding_sessions(run: BookRun, position: Mapping[date, int]) -> list[int]:
    end = run.marks[-1].day
    return [position[t.exited or end] - position[t.entered] for t in run.trades]


def activity(run: BookRun, position: Mapping[date, int]) -> Activity:
    years = (len(run.marks) - 1) / TRADING_DAYS
    start = run.marks[0].equity
    held = holding_sessions(run, position)
    per_year = 1 / years if years > 0 else 0.0
    return Activity(
        buys_per_year=len(run.trades) * per_year,
        slippage_per_year=sum(t.slippage for t in run.trades) / start * per_year,
        mean_holding_sessions=statistics.fmean(held) if held else None,
        mean_cash_share=statistics.fmean(m.cash / m.equity for m in run.marks if m.equity > 0),
        stop_outs_per_year=sum(t.reason is ExitReason.STOP for t in run.trades) * per_year,
        largest_weight=max(m.largest for m in run.marks),
    )


@dataclass(frozen=True, slots=True)
class Moved:
    half_day: int
    """Buys on an E that a half day moved off the month's nominal session."""
    retry: int
    """Buys on a retry session."""


def moved(run: BookRun, next_session: Mapping[date, date]) -> Moved:
    half = sum(
        log.bought
        for log in run.rebalances
        if not log.retry and next_session.get(log.ranking) != log.execution
    )
    return Moved(half, sum(log.bought for log in run.rebalances if log.retry))


# --- the blind check ---------------------------------------------------------------------


@dataclass(slots=True)
class BlindCounts:
    """One year's (or the whole period's) counts: no equity, return or drawdown."""

    executions: int = 0
    ranked: list[int] = field(default_factory=list)
    candidates: list[int] = field(default_factory=list)
    buys: int = 0
    holdings: list[int] = field(default_factory=list)
    """After the buys of each E (its retry's, when it had one)."""
    reached: int = 0
    skips: Counter[SkipReason] = field(default_factory=Counter)
    blocked: Counter[EntryBlock] = field(default_factory=Counter)
    """Sessions whose buys each block stopped (E or a retry)."""
    blocked_picks: Counter[EntryBlock] = field(default_factory=Counter)
    retries: int = 0
    exits: Counter[ExitReason] = field(default_factory=Counter)
    holding_sessions: list[int] = field(default_factory=list)
    unfilled: int = 0

    @property
    def price_skip_share(self) -> float:
        price = self.skips[SkipReason.PRICE_FIT] + self.skips[SkipReason.NO_CASH]
        return price / self.reached if self.reached else 0.0

    @property
    def cluster_skip_share(self) -> float:
        cluster = self.skips[SkipReason.CLUSTER]
        listed = cluster + sum(self.candidates)
        return cluster / listed if listed else 0.0


def _add_log(c: BlindCounts, log: RebalanceLog) -> None:
    if log.retry:
        c.retries += 1
    else:
        c.executions += 1
        c.ranked.append(log.ranked)
        c.candidates.append(log.candidates)
    c.buys += log.bought
    c.reached += log.picks + len(log.skipped)
    c.skips.update(s.reason for s in log.skipped)
    c.blocked.update(log.blocks)
    c.blocked_picks.update(b for b in log.blocks for _ in range(log.picks))
    c.unfilled += log.unfilled


def blind_counts(run: BookRun, position: Mapping[date, int]) -> dict[int | None, BlindCounts]:
    """Counts per calendar year, and for the whole period under None."""
    out: dict[int | None, BlindCounts] = defaultdict(BlindCounts)
    last_of: dict[tuple[date, date], RebalanceLog] = {}
    for log in run.rebalances:
        for key in (log.execution.year, None):
            _add_log(out[key], log)
        last_of[log.ranking, log.execution] = log
    for log in last_of.values():
        for key in (log.execution.year, None):
            out[key].holdings.append(log.holdings)
    end = run.marks[-1].day
    for t, held in zip(run.trades, holding_sessions(run, position), strict=True):
        year = (t.exited or end).year
        for key in (year, None):
            out[key].holding_sessions.append(held)
            if t.reason is not None:
                out[key].exits[t.reason] += 1
    return dict(out)


def stop_conditions(overall: BlindCounts, cfg: BookBacktestConfig) -> list[str]:
    """The pre-registration's §7 conditions that fired: each sends the work back to the user."""
    fired = []
    if overall.holdings and statistics.fmean(overall.holdings) < cfg.blind_min_mean_holdings:
        fired.append(
            f"mean holdings after buys {statistics.fmean(overall.holdings):.2f} "
            f"< {cfg.blind_min_mean_holdings}"
        )
    if overall.price_skip_share > cfg.blind_max_price_skips:
        fired.append(
            f"{overall.price_skip_share:.1%} of the names reached skipped for price "
            f"> {cfg.blind_max_price_skips:.0%}"
        )
    if overall.cluster_skip_share > cfg.blind_max_cluster_skips:
        fired.append(
            f"the cluster rule removed {overall.cluster_skip_share:.1%} of the candidates "
            f"> {cfg.blind_max_cluster_skips:.0%}"
        )
    if overall.ranked and min(overall.ranked) < cfg.blind_min_ranked:
        fired.append(f"a ranked set of {min(overall.ranked)} names < {cfg.blind_min_ranked}")
    return fired
