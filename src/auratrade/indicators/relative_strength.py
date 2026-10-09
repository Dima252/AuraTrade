"""RS rating, RS line, momentum rank and industry-group RS rank (README §5.3).

All of these use price returns on split-adjusted closes, as IBD's RS rating does (§5.1). The
cross-sectional functions take one session's values for the whole universe and rank them.
"""

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from auratrade.indicators.series import (
    Series,
    at_window_high,
    at_window_low,
    change,
    check_same_length,
    check_window,
    rolling_max,
)


def rs_score(closes: Sequence[float], weights: Sequence[tuple[int, float]]) -> Series:
    """Weighted sum of returns over several lookbacks: ``sum(w * R_lookback)``.

    The Trend Template's score is ``weights = [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)]``.
    ``None`` until the longest lookback has history.
    """
    if not weights:
        raise ValueError("weights must not be empty")
    returns = [(change(closes, lookback), weight) for lookback, weight in weights]
    out: Series = []
    for t in range(len(closes)):
        total: float | None = 0.0
        for series, weight in returns:
            value = series[t]
            if value is None or total is None:
                total = None
                break
            total += weight * value
        out.append(total)
    return out


def percentile_ranks[K](values: Mapping[K, float]) -> dict[K, float]:
    """Percentile of each value within the set, 0 (lowest) to 100 (highest).

    ``100 * (number below + (number equal - 1) / 2) / (n - 1)``: ties share their average rank. A
    single value ranks 50. The RS rating is this over the universe's ``rs_score``.
    """
    n = len(values)
    if n == 1:
        return dict.fromkeys(values, 50.0)
    ordered = sorted(values.items(), key=lambda item: item[1])
    out: dict[K, float] = {}
    start = 0
    while start < n:
        end = start
        while end < n and ordered[end][1] == ordered[start][1]:
            end += 1
        percentile = 100 * (start + (end - start - 1) / 2) / (n - 1)
        for key, _ in ordered[start:end]:
            out[key] = percentile
        start = end
    return out


def momentum_ranks(
    closes_by_symbol: Mapping[str, Sequence[float]], lookbacks: Sequence[int]
) -> dict[int, dict[str, float]]:
    """Percentile of each symbol's return over each lookback (README §5.3, "momentum rank").

    Every close series ends on the same session. Kullamägi's 1-, 3- and 6-month returns are
    ``lookbacks = [21, 63, 126]``. A symbol without enough history is left out of that lookback's
    ranking rather than ranked on a shorter return.
    """
    out: dict[int, dict[str, float]] = {}
    for lookback in lookbacks:
        check_window(lookback, "lookback")
        returns = {
            symbol: closes[-1] / closes[-1 - lookback] - 1
            for symbol, closes in closes_by_symbol.items()
            if len(closes) > lookback
        }
        out[lookback] = percentile_ranks(returns) if returns else {}
    return out


def rs_line(closes: Sequence[float], benchmark_closes: Sequence[float]) -> list[float]:
    """Price relative to the benchmark (SPY): ``close / benchmark close``, session by session."""
    check_same_length(closes, benchmark_closes)
    return [c / b for c, b in zip(closes, benchmark_closes, strict=True)]


@dataclass(frozen=True, slots=True)
class RsLineState:
    new_high: bool
    """The RS line is at the high of its ``high_window`` (ties count)."""
    near_high: bool
    """Within ``near_tolerance`` of that high (true whenever ``new_high`` is)."""
    at_low: bool
    """At the low of its ``low_window``: "faltering RS" at a 50-session low (§7.3)."""


def rs_line_states(
    line: Sequence[float], *, high_window: int, near_tolerance: float, low_window: int
) -> list[RsLineState | None]:
    """RS-line state per session; ``None`` until both windows have full history."""
    if not 0 <= near_tolerance < 1:
        raise ValueError(f"near_tolerance must be in [0, 1), got {near_tolerance}")
    highs = rolling_max(line, high_window)
    new_highs = at_window_high(line, high_window)
    lows = at_window_low(line, low_window)
    out: list[RsLineState | None] = []
    for t, value in enumerate(line):
        high = highs[t]
        if high is None or t < low_window - 1:
            out.append(None)
            continue
        out.append(
            RsLineState(
                new_high=new_highs[t],
                near_high=value >= high * (1 - near_tolerance),
                at_low=lows[t],
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class GroupRs:
    median_rs: float
    rank: int
    """1 = the strongest group; tied groups share a rank."""
    percentile: float
    members: int


def group_rs_ranks(ratings: Mapping[str, float], group_of: Mapping[str, str]) -> dict[str, GroupRs]:
    """Industry-group RS: the median RS rating of each group's members, ranked across groups.

    Symbols missing from ``group_of`` are ignored.
    """
    members: dict[str, list[float]] = {}
    for symbol, rating in ratings.items():
        group = group_of.get(symbol)
        if group is not None:
            members.setdefault(group, []).append(rating)
    medians = {group: statistics.median(values) for group, values in members.items()}
    if not medians:
        return {}
    percentiles = percentile_ranks(medians)
    return {
        group: GroupRs(
            median_rs=median,
            rank=1 + sum(other > median for other in medians.values()),
            percentile=percentiles[group],
            members=len(members[group]),
        )
        for group, median in medians.items()
    }
