"""The portfolio backtest's numbers (`docs/backtests/portfolio-preregistration.md` §5-§7), pure.

Everything the report states is computed here from simulated runs: the metrics of §5.1, the
gate of §5.2 and the near miss of §5.3, the reported extras of §5.4, and the blind check's
counts and stop conditions of §4.4 and §7. Nothing here reads files or prints.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from auratrade.config import PortfolioBacktestConfig
from auratrade.sim.portfolio import PortfolioRun
from auratrade.strategies.core_allocation import Trend

DAYS_A_YEAR = 365.25


# --- metrics ---------------------------------------------------------------------------------


def years_between(first: date, last: date) -> float:
    return (last - first).days / DAYS_A_YEAR


def cagr(start: float, final: float, years: float) -> float:
    if start <= 0 or years <= 0:
        raise ValueError("CAGR needs a positive start and a positive span")
    if final <= 0:
        return -1.0
    return float((final / start) ** (1 / years) - 1)


def max_drawdown(marks: Sequence[float]) -> float:
    peak, worst = -math.inf, 0.0
    for mark in marks:
        peak = max(peak, mark)
        if peak > 0:
            worst = max(worst, 1 - mark / peak)
    return worst


def daily_returns(marks: Sequence[float]) -> list[float]:
    return [b / a - 1 for a, b in pairwise(marks) if a > 0]


def volatility(marks: Sequence[float], sessions_a_year: float) -> float:
    returns = daily_returns(marks)
    if len(returns) < 2:
        return 0.0
    return statistics.stdev(returns) * math.sqrt(sessions_a_year)


def year_end_marks(days: Sequence[date], marks: Sequence[float]) -> dict[int, float]:
    out: dict[int, float] = {}
    for day, mark in zip(days, marks, strict=True):
        out[day.year] = mark
    return out


def calendar_years(days: Sequence[date], marks: Sequence[float]) -> dict[int, float]:
    """Each complete calendar year's return on the marks (from the previous year's last mark)."""
    ends = year_end_marks(days, marks)
    years = sorted(ends)
    return {y: ends[y] / ends[p] - 1 for p, y in pairwise(years)}


@dataclass(frozen=True, slots=True)
class Metrics:
    cagr_after_tax: float
    cagr_before_tax: float
    max_drawdown: float
    volatility: float
    worst_year: float
    flips_a_year: float
    tax_a_year: float
    """Tax paid a year as a share of the average equity mark."""
    up: float
    down: float
    capped: float
    halted: float
    """Shares of the R's."""
    stopouts: int
    halt_starts: int
    margin_calls: int

    @property
    def return_over_drawdown(self) -> float:
        return self.cagr_after_tax / self.max_drawdown if self.max_drawdown else math.inf


def metrics(after_tax: PortfolioRun, before_tax: PortfolioRun) -> Metrics:
    days, marks = after_tax.days, after_tax.marks
    span = years_between(days[0], days[-1])
    rankings = after_tax.rankings
    count = len(rankings) or 1
    years = calendar_years(days, marks)
    return Metrics(
        cagr(after_tax.start_equity, after_tax.final_wealth, span),
        cagr(before_tax.start_equity, before_tax.final_wealth, span),
        max_drawdown(marks),
        volatility(marks, len(marks) / span),
        min(years.values()) if years else 0.0,
        after_tax.flips / span,
        after_tax.tax_paid / span / statistics.fmean(marks),
        sum(r.state is Trend.UP for r in rankings) / count,
        sum(r.state is Trend.DOWN for r in rankings) / count,
        sum(r.capped for r in rankings) / count,
        sum(r.halted for r in rankings) / count,
        len(after_tax.stopouts),
        len(after_tax.halt_starts),
        len(after_tax.margin_calls),
    )


# --- the gate --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    first: date
    last: date
    strategy: float
    benchmark: float
    """Final wealth after the final sale's tax."""

    @property
    def ahead(self) -> bool:
        return self.strategy > self.benchmark

    @property
    def excess(self) -> float:
        """The difference in after-tax CAGR over the window."""
        span = years_between(self.first, self.last)
        return cagr(1.0, self.strategy, span) - cagr(1.0, self.benchmark, span)


@dataclass(frozen=True, slots=True)
class Gate:
    g1: bool
    g2: bool
    g3: bool
    window_share: float
    cagr_gap: float
    """The strategy's after-tax CAGR less the benchmark's."""
    near_miss: bool

    @property
    def passed(self) -> bool:
        return self.g1 and self.g2 and self.g3

    @property
    def verdict(self) -> str:
        if self.passed:
            return "PASS"
        return "NEAR MISS" if self.near_miss else "CLEAR FAIL"


def gate(
    strategy: Metrics, benchmark: Metrics, windows: Sequence[Window], bt: PortfolioBacktestConfig
) -> Gate:
    gap = strategy.cagr_after_tax - benchmark.cagr_after_tax
    share = sum(w.ahead for w in windows) / len(windows) if windows else 0.0
    g1 = gap > 0
    g2 = strategy.max_drawdown <= benchmark.max_drawdown
    g3 = share >= bt.min_window_share
    near = not (g1 and g2 and g3) and g2 and gap >= -bt.near_miss_cagr
    return Gate(g1, g2, g3, share, gap, near)


def percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    k = (len(ordered) - 1) * q
    low, high = math.floor(k), math.ceil(k)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


# --- reported beside the gate ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Crash:
    name: str
    strategy_fall: float
    benchmark_fall: float
    strategy_months: float | None
    benchmark_months: float | None
    """Months from the period's trough back to the peak before it; None if never recovered."""


CRASHES = (
    ("1929-32", date(1929, 9, 1), date(1932, 7, 31)),
    ("1937-38", date(1937, 3, 1), date(1938, 3, 31)),
    ("1973-74", date(1973, 1, 1), date(1974, 12, 31)),
    ("1987", date(1987, 8, 1), date(1987, 12, 31)),
    ("2000-02", date(2000, 3, 1), date(2002, 10, 31)),
    ("2007-09", date(2007, 10, 1), date(2009, 3, 31)),
    ("2020", date(2020, 2, 1), date(2020, 4, 30)),
    ("2022", date(2022, 1, 1), date(2022, 10, 31)),
)


def fall_and_recovery(
    days: Sequence[date], marks: Sequence[float], first: date, last: date
) -> tuple[float, float | None]:
    """The deepest fall within [first, last] from the peak before it, and the months from that
    trough until the marks regain the peak."""
    inside = [i for i, d in enumerate(days) if first <= d <= last]
    if not inside:
        return 0.0, 0.0
    trough = min(inside, key=lambda i: marks[i])
    peak = max(marks[: trough + 1])
    back = next((i for i in range(trough, len(marks)) if marks[i] >= peak), None)
    months = None if back is None else (days[back] - days[trough]).days / (DAYS_A_YEAR / 12)
    return 1 - marks[trough] / peak, months


def crashes(strategy: PortfolioRun, benchmark: PortfolioRun) -> list[Crash]:
    out = []
    for name, first, last in CRASHES:
        s = fall_and_recovery(strategy.days, strategy.marks, first, last)
        b = fall_and_recovery(benchmark.days, benchmark.marks, first, last)
        out.append(Crash(name, s[0], b[0], s[1], b[1]))
    return out


def decades(strategy: PortfolioRun, benchmark: PortfolioRun) -> list[tuple[str, float, float]]:
    """Each decade's annualized return on the marks (before the final sale): strategy, benchmark."""
    s_ends = year_end_marks(strategy.days, strategy.marks)
    b_ends = year_end_marks(benchmark.days, benchmark.marks)
    years = sorted(s_ends)
    out = []
    for start in range(years[0] - years[0] % 10, years[-1], 10):
        first, last = max(start - 1, years[0]), min(start + 9, years[-1])
        if last <= first:
            continue
        span = last - first
        out.append(
            (
                f"{start}s",
                (s_ends[last] / s_ends[first]) ** (1 / span) - 1,
                (b_ends[last] / b_ends[first]) ** (1 / span) - 1,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class Bootstrap:
    mean: float
    low: float
    high: float


def block_bootstrap(
    strategy: Sequence[float], benchmark: Sequence[float], bt: PortfolioBacktestConfig
) -> Bootstrap:
    """The daily excess (strategy - benchmark returns) x 252, with its interval from
    non-overlapping blocks drawn with replacement."""
    excess = [a - b for a, b in zip(daily_returns(strategy), daily_returns(benchmark), strict=True)]
    size = bt.bootstrap_block_sessions
    blocks = [excess[i : i + size] for i in range(0, len(excess) - size + 1, size)]
    rng = random.Random(bt.seed)  # noqa: S311 - a reproducible resample, not a secret
    means = []
    for _ in range(bt.bootstrap_samples):
        drawn = [x for _ in blocks for x in rng.choice(blocks)]
        means.append(statistics.fmean(drawn) * 252)
    tail = (1 - bt.confidence) / 2
    return Bootstrap(
        statistics.fmean(excess) * 252, percentile(means, tail), percentile(means, 1 - tail)
    )


# --- the blind check -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataCheck:
    name: str
    correlation: float
    cagr_gap: float
    sessions: int
    passed: bool


def data_check(
    name: str,
    proxy: Sequence[float],
    real: Sequence[float],
    years: float,
    bt: PortfolioBacktestConfig,
) -> DataCheck:
    """A proxy's daily returns against a real fund's over the same sessions."""
    a, b = daily_returns(proxy), daily_returns(real)
    correlation = statistics.correlation(a, b)
    gap = abs(cagr(proxy[0], proxy[-1], years) - cagr(real[0], real[-1], years))
    ok = correlation >= bt.check_min_correlation and gap <= bt.check_max_cagr_gap
    return DataCheck(name, correlation, gap, len(a) + 1, ok)


KNOWN_CLOSURES = ((date(1933, 3, 3), date(1933, 3, 15)),)
"""Gaps of more than 7 days the pre-registration expects (§4.4): the 1933 bank holiday."""


def session_gaps(
    days: Sequence[date], bt: PortfolioBacktestConfig
) -> list[tuple[date, date, bool]]:
    """Every gap of more than ``max_session_gap_days`` between sessions, and whether it is a
    known closure."""
    out = []
    for a, b in pairwise(days):
        if (b - a).days > bt.max_session_gap_days:
            known = any(first <= a and b <= last for first, last in KNOWN_CLOSURES)
            out.append((a, b, known))
    return out


@dataclass(frozen=True, slots=True)
class BlindCounts:
    flips_a_year: float
    halt_share: float
    trades_a_year: float
    stopouts: int
    up: float
    down: float
    capped: float
    halted: float
    lots_opened: int


def blind_counts(run: PortfolioRun) -> BlindCounts:
    span = years_between(run.days[0], run.days[-1])
    count = len(run.rankings) or 1
    buys = sum(t.units > 0 for t in run.trades)
    return BlindCounts(
        run.flips / span,
        sum(r.halted for r in run.rankings) / count,
        len(run.trades) / span,
        len(run.stopouts),
        sum(r.state is Trend.UP for r in run.rankings) / count,
        sum(r.state is Trend.DOWN for r in run.rankings) / count,
        sum(r.capped for r in run.rankings) / count,
        sum(r.halted for r in run.rankings) / count,
        buys,
    )


def blind_stops(
    counts: BlindCounts,
    checks: Sequence[DataCheck],
    gaps: Sequence[tuple[date, date, bool]],
    bt: PortfolioBacktestConfig,
) -> list[str]:
    """The pre-registration §7 conditions that send the work back to the user."""
    out = [f"data check failed: {c.name}" for c in checks if not c.passed]
    out += [f"unexpected gap: {a} to {b}" for a, b, known in gaps if not known]
    if counts.flips_a_year > bt.blind_max_flips_per_year:
        out.append(f"{counts.flips_a_year:.2f} flips a year (> {bt.blind_max_flips_per_year})")
    if counts.halt_share > bt.blind_max_halt_share:
        out.append(f"halt on {counts.halt_share:.0%} of the R's (> {bt.blind_max_halt_share:.0%})")
    return out
