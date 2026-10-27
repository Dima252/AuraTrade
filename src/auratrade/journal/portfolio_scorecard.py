"""The scorecard: the evidence for whether the LLM adds value (README §5.5; task 4.4), pure.

Each decision is scored over its **period**, from its E's close to the next E's; the latest
decision's period runs to the latest close and is still open. Its E's close is after its own
trades, so a period carries the decision's holdings, and the next E's trades (and their
costs) close it.

- **The headline:** the book's return less the twin's, before and after the estimated tax
  (equity less the tax a sale of everything would owe at that close, `TaxBook.tax_if_sold`),
  with SPY bought and held beside them; cumulative from the trial's start; and once
  ``bootstrap_months`` periods are complete, a block-bootstrap interval of the after-tax excess
  a month, annualized.
- **The tilts:** each satellite weight less its neutral (the market fund at the satellite's
  whole share, #163) times its total return less the market fund's; the core shift times the
  market fund's return less the cash fund's. The market fund's own tilt always scores 0.
- **The expectations:** each stated ``expected_vs_market`` (the market fund's own excluded)
  against the fund's return less the market fund's over a complete period: the direction
  (within ``flat_band`` of 0, both must be) and the size (within ``size_factor`` either way).
- **The tax each decision realized:** the book's realized gains on its E, and the tax rate on
  them (before the year's offsets), with the twin's on the same E beside them.

After the estimated tax, realizing a gain doesn't move the book at once (the tax was owed
either way); it costs the compounding of the tax paid at the year's end, which shows up later.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from auratrade.config import PmScorecardConfig

MONTHS_PER_YEAR = 12


@dataclass(frozen=True, slots=True)
class Marks:
    """One book's closes from the trial's start (all three books share the days)."""

    days: Sequence[date]
    equity: Sequence[float]
    tax_owed: Sequence[float]
    """The estimated tax if everything were sold at the close."""
    realized: Sequence[float]
    """The year's realized gains so far."""

    def after_tax(self, i: int) -> float:
        return self.equity[i] - self.tax_owed[i]

    def realized_on(self, i: int) -> float:
        """The gains realized at close ``i`` (the year's so far, less the day before's)."""
        if i == 0 or self.days[i - 1].year != self.days[i].year:
            return self.realized[i]
        return self.realized[i] - self.realized[i - 1]


@dataclass(frozen=True, slots=True)
class Decision:
    session: date
    """E."""
    shift: float
    """The enforced level less the rule level."""
    satellite: Mapping[str, float]
    """The enforced satellite (a held one is last month's targets)."""
    expectations: Mapping[str, float]
    """``expected_vs_market`` by fund, as the LLM stated them (none when it held)."""


@dataclass(frozen=True, slots=True)
class Expectation:
    symbol: str
    expected: float
    actual: float
    direction: bool
    size: bool


@dataclass(frozen=True, slots=True)
class Returns:
    book: float
    twin: float
    spy: float

    @property
    def excess(self) -> float:
        return self.book - self.twin


@dataclass(frozen=True, slots=True)
class Period:
    session: date
    end: date
    complete: bool
    before_tax: Returns
    after_tax: Returns
    tilts: Mapping[str, float]
    shift_score: float
    expectations: Sequence[Expectation]
    realized_gain: float
    twin_realized_gain: float
    tax_rate: float

    @property
    def tilt_score(self) -> float:
        return math.fsum(self.tilts.values())

    @property
    def tax_realized(self) -> float:
        return self.tax_rate * self.realized_gain

    @property
    def twin_tax_realized(self) -> float:
        return self.tax_rate * self.twin_realized_gain


@dataclass(frozen=True, slots=True)
class Interval:
    mean: float
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class Scorecard:
    through: date
    periods: Sequence[Period]
    before_tax: Returns
    """Cumulative from the trial's start."""
    after_tax: Returns
    interval: Interval | None
    """The after-tax excess a year, from ``bootstrap_months`` complete periods on."""

    @property
    def complete(self) -> list[Period]:
        return [p for p in self.periods if p.complete]

    @property
    def expectations(self) -> list[Expectation]:
        return [e for p in self.complete for e in p.expectations]

    def summary(self) -> dict[str, float]:
        """The Portfolio Manager's facts (`PmFacts.scorecard`): numbers only, a rate left out
        while nothing is scored."""
        periods = self.periods
        out = {
            "months_complete": float(len(self.complete)),
            "book_return_after_tax": self.after_tax.book,
            "twin_return_after_tax": self.after_tax.twin,
            "spy_return_after_tax": self.after_tax.spy,
            "excess_after_tax": self.after_tax.excess,
            "excess_before_tax": self.before_tax.excess,
            "tilt_score": math.fsum(p.tilt_score for p in periods),
            "shift_score": math.fsum(p.shift_score for p in periods),
            "tax_realized": math.fsum(p.tax_realized for p in periods),
            "twin_tax_realized": math.fsum(p.twin_tax_realized for p in periods),
            "expectations_scored": float(len(self.expectations)),
        }
        if self.expectations:
            n = len(self.expectations)
            out["expectation_direction_rate"] = sum(e.direction for e in self.expectations) / n
            out["expectation_size_rate"] = sum(e.size for e in self.expectations) / n
        if self.interval is not None:
            out["excess_after_tax_a_year"] = self.interval.mean
            out["excess_after_tax_a_year_low"] = self.interval.low
            out["excess_after_tax_a_year_high"] = self.interval.high
        return out


def score(
    book: Marks,
    twin: Marks,
    spy: Marks,
    decisions: Sequence[Decision],
    closes: Mapping[str, Mapping[date, float]],
    *,
    market: str,
    cash: str,
    satellite_share: float,
    tax_rate: float,
    cfg: PmScorecardConfig,
) -> Scorecard:
    """The scorecard through the books' last close; ``closes`` are each fund's total-return
    closes. Decisions whose E is after the last close aren't scored yet."""
    if not book.days or not (book.days == twin.days == spy.days):
        raise ValueError("the books must share their closes")
    at = {d: i for i, d in enumerate(book.days)}
    last = len(book.days) - 1
    scored = sorted((d for d in decisions if d.session in at), key=lambda d: d.session)
    periods = []
    for k, d in enumerate(scored):
        start = at[d.session]
        end = at[scored[k + 1].session] if k + 1 < len(scored) else last
        complete = k + 1 < len(scored)
        r = _fund_returns(closes, start_day=d.session, end_day=book.days[end])
        periods.append(
            Period(
                session=d.session,
                end=book.days[end],
                complete=complete,
                before_tax=_returns(book, twin, spy, start, end, after_tax=False),
                after_tax=_returns(book, twin, spy, start, end, after_tax=True),
                tilts=_tilts(d.satellite, r, market, satellite_share),
                shift_score=_shift_score(d.shift, r, market, cash),
                expectations=_expectations(d.expectations, r, market, cfg) if complete else (),
                realized_gain=book.realized_on(start),
                twin_realized_gain=twin.realized_on(start),
                tax_rate=tax_rate,
            )
        )
    monthly = [p.after_tax.excess for p in periods if p.complete]
    return Scorecard(
        through=book.days[last],
        periods=periods,
        before_tax=_returns(book, twin, spy, 0, last, after_tax=False),
        after_tax=_returns(book, twin, spy, 0, last, after_tax=True),
        interval=block_bootstrap(monthly, cfg) if len(monthly) >= cfg.bootstrap_months else None,
    )


def _returns(book: Marks, twin: Marks, spy: Marks, i: int, j: int, *, after_tax: bool) -> Returns:
    def one(m: Marks) -> float:
        if after_tax:
            return m.after_tax(j) / m.after_tax(i) - 1
        return m.equity[j] / m.equity[i] - 1

    return Returns(one(book), one(twin), one(spy))


def _fund_returns(
    closes: Mapping[str, Mapping[date, float]], *, start_day: date, end_day: date
) -> dict[str, float]:
    """Each fund's total return over the period; a fund without both closes is left out."""
    out = {}
    for symbol, by_day in closes.items():
        a, b = by_day.get(start_day), by_day.get(end_day)
        if a and b is not None:
            out[symbol] = b / a - 1
    return out


def _tilts(
    satellite: Mapping[str, float], r: Mapping[str, float], market: str, share: float
) -> dict[str, float]:
    if market not in r:
        return {}
    out = {}
    for symbol in sorted(set(satellite) - {market}):
        if symbol in r:
            out[symbol] = satellite[symbol] * (r[symbol] - r[market])
    # The market fund's own tilt (its weight less the whole share) scores 0 against itself.
    return out


def _shift_score(shift: float, r: Mapping[str, float], market: str, cash: str) -> float:
    if market not in r or cash not in r:
        return 0.0
    return shift * (r[market] - r[cash])


def _expectations(
    stated: Mapping[str, float], r: Mapping[str, float], market: str, cfg: PmScorecardConfig
) -> list[Expectation]:
    if market not in r:
        return []
    out = []
    for symbol in sorted(set(stated) - {market}):
        if symbol not in r:
            continue
        expected, actual = stated[symbol], r[symbol] - r[market]
        direction, size = judge(expected, actual, cfg)
        out.append(Expectation(symbol, expected, actual, direction, size))
    return out


def judge(expected: float, actual: float, cfg: PmScorecardConfig) -> tuple[bool, bool]:
    """(right direction, roughly right size). An expectation within ``flat_band`` of 0 says
    "level": both are right when the outcome is level too. Otherwise the direction is right on
    the same sign, and the size when the outcome is within ``size_factor`` of it."""
    if abs(expected) <= cfg.flat_band:
        level = abs(actual) <= cfg.flat_band
        return level, level
    direction = (expected > 0) == (actual > 0) and actual != 0
    ratio = actual / expected
    size = direction and 1 / cfg.size_factor <= ratio <= cfg.size_factor
    return direction, size


def block_bootstrap(monthly: Sequence[float], cfg: PmScorecardConfig) -> Interval:
    """The mean monthly excess a year, with its percentile interval from non-overlapping
    blocks of ``block_months`` drawn with replacement (a partial last block is kept)."""
    size = cfg.block_months
    blocks = [monthly[i : i + size] for i in range(0, len(monthly), size)]
    rng = random.Random(cfg.seed)  # noqa: S311 - a reproducible resample, not a secret
    means = []
    for _ in range(cfg.samples):
        drawn = [x for _ in blocks for x in rng.choice(blocks)]
        means.append(statistics.fmean(drawn) * MONTHS_PER_YEAR)
    tail = (1 - cfg.confidence) / 2
    return Interval(
        statistics.fmean(monthly) * MONTHS_PER_YEAR,
        percentile(means, tail),
        percentile(means, 1 - tail),
    )


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q`` quantile with linear interpolation between the sorted values."""
    ordered = sorted(values)
    at = q * (len(ordered) - 1)
    low = math.floor(at)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


def period_json(p: Period) -> dict[str, object]:
    """One period for `portfolio_journal.results`."""
    return {
        "end": str(p.end),
        "complete": p.complete,
        "before_tax": _returns_json(p.before_tax),
        "after_tax": _returns_json(p.after_tax),
        "tilts": dict(p.tilts),
        "tilt_score": p.tilt_score,
        "shift_score": p.shift_score,
        "expectations": [
            {
                "symbol": e.symbol,
                "expected": e.expected,
                "actual": e.actual,
                "direction": e.direction,
                "size": e.size,
            }
            for e in p.expectations
        ],
        "realized_gain": p.realized_gain,
        "tax_realized": p.tax_realized,
        "twin_realized_gain": p.twin_realized_gain,
        "twin_tax_realized": p.twin_tax_realized,
    }


def _returns_json(r: Returns) -> dict[str, float]:
    return {"book": r.book, "twin": r.twin, "spy": r.spy, "excess": r.excess}
