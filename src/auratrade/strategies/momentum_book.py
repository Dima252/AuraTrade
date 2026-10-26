"""The momentum book's ranking and the rule book's choice (README §5.3, §7; #147; task 3.10).

Pure: no I/O. The live `rank` job, the rule book and the backtest (`sim/book.py`) all call these
functions; the exact rules are fixed in `docs/backtests/momentum-preregistration.md` §3.

- **Ranked set:** universe members with at least ``formation_sessions + 1`` closes, cut to the
  ``universe_size`` with the highest 50-session average raw dollar volume (ties by symbol).
- **Signal:** 12-1, ``close[-1 - skip] / close[-1 - formation] - 1`` on split-adjusted closes;
  or, for comparison C1, nearness to the 52-week high, ``close[-1] / max(last formation closes)``.
  A security whose signal can't be computed (a non-positive close) is left out: fail closed.
- **Rank order:** by the signal, highest first; ties by the 12-1 signal (C1 only), then the
  symbol. **Percentile:** ``percentile_ranks`` over the ranked set's signals.
- **Hold zone:** a holding is kept while its percentile is at least ``hold_percentile``; one
  missing from the ranked set is sold.
- **Candidates:** in rank order, the names not held, with a price, that fit at least 1 share in a
  slot at the limit, and aren't linked by the cluster rule to ``max_per_cluster`` holdings.
- **The rule book** fills open slots from the candidates in order, re-checking the cluster rule
  against holdings plus the names already chosen, and sizing each on the cash left.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from auratrade.calendar.rebalance import Rebalance, SessionDay, rebalances
from auratrade.config import BookConfig, BookSignal, GuardrailsConfig
from auratrade.execution.prices import round_down, round_up
from auratrade.guardrails.clusters import ClusterMember, cluster_check
from auratrade.indicators.relative_strength import percentile_ranks

# Float noise in slot / limit must not floor 10.0 shares to 9.
_SHARE_TOLERANCE = 1e-9

# The calendar moved to `auratrade.calendar.rebalance` (task 3.19); re-exported for the book.
__all__ = ["Rebalance", "SessionDay", "rebalances"]


Signal = BookSignal


class SkipReason(StrEnum):
    NO_PRICE = "NO_PRICE"
    PRICE_FIT = "PRICE_FIT"
    """Not even 1 share fits in a slot at the limit (#122)."""
    CLUSTER = "CLUSTER"
    NO_CASH = "NO_CASH"
    """The rule book only: the cash left buys 0 shares."""
    EARNINGS = "EARNINGS"
    """Comparison C5: a report reacts within the blackout."""


@dataclass(frozen=True, slots=True)
class BookSecurity:
    """One security as of the ranking session."""

    key: str
    symbol: str
    closes: Sequence[float]
    """Split-adjusted closes of its own traded sessions up to the ranking session, oldest first."""
    dollar_volume: float | None
    """The 50-session average raw dollar volume on the ranking session."""
    in_universe: bool
    """A member of the point-in-time universe on the ranking session (README §5.1)."""


@dataclass(frozen=True, slots=True)
class Ranked:
    key: str
    symbol: str
    signal: float
    percentile: float
    rank: int
    """1 is the strongest."""


def twelve_one(closes: Sequence[float], cfg: BookConfig) -> float | None:
    """The 12-1 return; None without enough closes or with a non-positive one."""
    if len(closes) <= cfg.formation_sessions:
        return None
    start, end = closes[-1 - cfg.formation_sessions], closes[-1 - cfg.skip_sessions]
    if not (start > 0 and end > 0 and math.isfinite(start) and math.isfinite(end)):
        return None
    return end / start - 1


def high_52w(closes: Sequence[float], cfg: BookConfig) -> float | None:
    """The last close over the highest of the last ``formation_sessions`` closes."""
    if len(closes) <= cfg.formation_sessions:
        return None
    window = closes[-cfg.formation_sessions :]
    if not all(c > 0 and math.isfinite(c) for c in window):
        return None
    return window[-1] / max(window)


def ranked_set(securities: Iterable[BookSecurity], cfg: BookConfig) -> list[BookSecurity]:
    """The universe members with enough history, cut to the most liquid ``universe_size``."""
    eligible = [
        s
        for s in securities
        if s.in_universe
        and len(s.closes) > cfg.formation_sessions
        and s.dollar_volume is not None
        and math.isfinite(s.dollar_volume)
    ]
    eligible.sort(key=lambda s: (-(s.dollar_volume or 0.0), s.symbol))
    return eligible[: cfg.universe_size]


def rank(
    securities: Iterable[BookSecurity], cfg: BookConfig, signal: Signal = Signal.TWELVE_ONE
) -> list[Ranked]:
    """The ranked set in rank order, with each security's signal and percentile."""
    scored: list[tuple[BookSecurity, float, float]] = []
    for s in ranked_set(securities, cfg):
        base = twelve_one(s.closes, cfg)
        value = base if signal is Signal.TWELVE_ONE else high_52w(s.closes, cfg)
        if value is None or base is None:
            continue
        scored.append((s, value, base))
    if not scored:
        return []
    percentiles = percentile_ranks({s.key: value for s, value, _ in scored})
    scored.sort(key=lambda item: (-item[1], -item[2], item[0].symbol))
    return [
        Ranked(s.key, s.symbol, value, percentiles[s.key], i)
        for i, (s, value, _) in enumerate(scored, start=1)
    ]


def keeps(holding: str, by_key: Mapping[str, Ranked], cfg: BookConfig) -> bool:
    """True while the holding (a security key) is in the hold zone; False if it fell below it or
    left the ranked set (README §7.3)."""
    ranked = by_key.get(holding)
    return ranked is not None and ranked.percentile >= cfg.hold_percentile


# --- levels and sizing -------------------------------------------------------------------


def slot_value(equity: float, cfg: BookConfig, fraction: float = 1.0) -> float:
    """One slot: equity x max exposure / holdings, times the Portfolio Manager's fraction."""
    return max(equity, 0.0) * cfg.max_exposure / cfg.holdings * fraction


def buy_limit(price: float, slippage: float) -> float:
    """The marketable limit: the decision price x (1 + slippage), rounded up to the tick."""
    return round_up(price * (1 + slippage))


def shares_for(value: float, limit: float) -> int:
    """Whole shares of ``value`` at the limit, floored; 0 when nothing fits or inputs are bad."""
    if not (math.isfinite(value) and math.isfinite(limit)) or limit <= 0 or value <= 0:
        return 0
    return max(math.floor(value / limit + _SHARE_TOLERANCE), 0)


def initial_stop(price: float, cfg: BookConfig) -> float:
    """The entry stop: the decision price x (1 - stop fraction), rounded down to the tick."""
    return round_down(price * (1 - cfg.stop_fraction))


def raised_stop(current: float, price: float, cfg: BookConfig) -> float:
    """At a rebalance the stop rises to the same fraction below the decision price; never
    lower (README §7.2)."""
    return max(current, initial_stop(price, cfg))


# --- candidates and the rule book --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Skipped:
    symbol: str
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class CandidateList:
    candidates: tuple[Ranked, ...]
    skipped: tuple[Skipped, ...]
    """The names passed over before the list was full, with the reason (for the blind check)."""


def _clustered(
    symbol: str,
    group: Sequence[ClusterMember],
    members: Mapping[str, ClusterMember],
    guard: GuardrailsConfig,
) -> bool:
    member = members.get(symbol)
    if member is None:
        return True  # no data to rule a link out: fail closed (#140)
    return cluster_check(member, group, guard) is not None


def candidates(
    ranking: Sequence[Ranked],
    *,
    held: Collection[str],
    prices: Mapping[str, float | None],
    slot: float,
    limit_slippage: float,
    holdings: Sequence[ClusterMember],
    members: Mapping[str, ClusterMember],
    guard: GuardrailsConfig,
    count: int,
    clusters: bool = True,
    blackout: Collection[str] = (),
) -> CandidateList:
    """The first ``count`` buyable names in rank order (README §5.3). ``held``, ``prices`` and
    ``blackout`` are keyed by security key, ``members`` by symbol; ``clusters=False`` is
    comparison C7, ``blackout`` (names with a report too close) comparison C5."""
    chosen: list[Ranked] = []
    skipped: list[Skipped] = []
    for r in ranking:
        if len(chosen) >= count:
            break
        if r.key in held:
            continue
        price = prices.get(r.key)
        if price is None or not math.isfinite(price) or price <= 0:
            skipped.append(Skipped(r.symbol, SkipReason.NO_PRICE))
        elif r.key in blackout:
            skipped.append(Skipped(r.symbol, SkipReason.EARNINGS))
        elif shares_for(slot, buy_limit(price, limit_slippage)) < 1:
            skipped.append(Skipped(r.symbol, SkipReason.PRICE_FIT))
        elif clusters and _clustered(r.symbol, holdings, members, guard):
            skipped.append(Skipped(r.symbol, SkipReason.CLUSTER))
        else:
            chosen.append(r)
    return CandidateList(tuple(chosen), tuple(skipped))


@dataclass(frozen=True, slots=True)
class Pick:
    ranked: Ranked
    shares: int
    limit: float
    stop: float


@dataclass(frozen=True, slots=True)
class RuleBookChoice:
    picks: tuple[Pick, ...]
    skipped: tuple[Skipped, ...]


def rule_book_choice(
    listed: Sequence[Ranked],
    *,
    open_slots: int,
    cash: float,
    slot: float,
    prices: Mapping[str, float],
    limit_slippage: float,
    holdings: Sequence[ClusterMember],
    members: Mapping[str, ClusterMember],
    guard: GuardrailsConfig,
    cfg: BookConfig,
    clusters: bool = True,
) -> RuleBookChoice:
    """The rule book's buys: the candidates in order, each re-checked against the holdings and
    the names already chosen, sized on min(slot, cash left) at its limit; 0 shares -> the next
    one. ``prices`` holds every listed candidate's decision price by security key."""
    picks: list[Pick] = []
    skipped: list[Skipped] = []
    group = list(holdings)
    cash_left = cash
    for r in listed:
        if len(picks) >= open_slots:
            break
        if clusters and _clustered(r.symbol, group, members, guard):
            skipped.append(Skipped(r.symbol, SkipReason.CLUSTER))
            continue
        price = prices[r.key]
        limit = buy_limit(price, limit_slippage)
        shares = shares_for(min(slot, cash_left), limit)
        if shares < 1:
            skipped.append(Skipped(r.symbol, SkipReason.NO_CASH))
            continue
        picks.append(Pick(r, shares, limit, initial_stop(price, cfg)))
        cash_left -= shares * limit
        member = members.get(r.symbol)
        if member is not None:
            group.append(member)
    return RuleBookChoice(tuple(picks), tuple(skipped))
