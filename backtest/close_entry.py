"""The close-entry backtest (task 3.8c-d, #144): the two pre-registered candidates, their two
comparisons and the decision-price checks, on the tuning period only.

    uv run python -m backtest.baseline close --blind   # step 3: counts and invariants only
    uv run python -m backtest.baseline close           # step 4: the one scored run

Everything here is fixed in `docs/backtests/close-entry-preregistration.md`, approved by the
user on 2026-10-25 in commit ``PREREGISTRATION_COMMIT``, before this code existed:

- **Candidate 1:** `EpisodicPivot`, bought on its gap session at the decision price if the gap
  held. **Candidate 2:** the four scan strategies, bought on the trigger session if their
  confirmation gate passes. Both are the `CLOSE` candidate in `risk.yaml`
  (``RiskConfig.with_candidate``), played by the one simulator.
- **The listing:** every strategy's capped list as in 3.8, the EPs in their morning view (#143)
  without the 09:36 opening-volume check: volume is judged at the decision time instead.
- **The gate** (section 8.1): the session-block interval above zero, positive in at least 4 of
  the 6 tuning years and >= 0 without the best one, at least 300 closed trades, and the
  decision-price check at or under 15%. A 21-session-block interval is reported beside it.
- **Blind** (step 3): the report shows counts and invariants only, and no trades are saved,
  so the code can be checked on real data without seeing an outcome.
"""

from __future__ import annotations

import random
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from auratrade.config import RiskConfig, StopKind, Strategy
from auratrade.journal.scorecard import Interval, block_bootstrap
from auratrade.plans.model import Event, Rule
from auratrade.sim.sessions import DECISION_BEFORE_CLOSE, Minutes
from auratrade.sim.trade import FillKind, TradeResult
from backtest.comparisons import Difference, Variant, compare, plans
from backtest.pool import (
    BOOTSTRAP_CONFIDENCE,
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    Expectancy,
    Listing,
    PoolInputs,
    PoolTrade,
    expectancy,
    known_at,
    play,
    run_pool,
)

PREREGISTRATION_COMMIT = "5b8a22a"
CANDIDATE = "CLOSE"
CANDIDATE_1 = (Strategy.EPISODIC_PIVOT,)
CANDIDATE_2 = (
    Strategy.VCP_BREAKOUT,
    Strategy.LEADER_PULLBACK,
    Strategy.EMERGING_LEADER,
    Strategy.MOMENTUM_BREAKOUT,
)
FIRST_YEAR = 2017
TUNING_YEARS = tuple(range(FIRST_YEAR, 2023))
MIN_TRADES = 300
MIN_POSITIVE_YEARS = 4
BLOCK_SESSIONS = 21
SAMPLE_SIZE = 200
SAMPLE_SEED = 38
MAX_DISAGREEMENT = 0.15
LIQUID_DOLLAR_VOLUME = 100e6
"""The tier reported apart as the proxy for a usable IEX decision price at 15:40."""

type MinutesFor = Callable[[Mapping[date, set[str]]], Mapping[date, Mapping[str, Minutes]]]
type SymbolsFor = Callable[[Mapping[date, set[str]]], Mapping[tuple[date, str], str]]


# --- The comparisons and the diagnostic runs ----------------------------------------------------


def volume_off(risk: RiskConfig) -> RiskConfig:
    """Comparison 1: every gate without its volume condition."""
    gates = {
        k: g.model_copy(update={"min_volume_ratio": None})
        for k, g in risk.plans.confirmation_gates.items()
    }
    return plans(risk, confirmation_gates=gates)


def structural_stop(risk: RiskConfig) -> RiskConfig:
    """Comparison 2 (candidate 2 only): the stop at the structural level."""
    entry = {
        s: p.model_copy(update={"stop": StopKind.STRUCTURAL}) if s in CANDIDATE_2 else p
        for s, p in risk.plans.entry.items()
    }
    return plans(risk, entry=entry)


def no_max_chase(risk: RiskConfig) -> RiskConfig:
    """Diagnostic: what the attempts skipped by the max chase would have done."""
    gates = {
        k: g.model_copy(update={"max_above_level": None})
        for k, g in risk.plans.confirmation_gates.items()
    }
    return plans(risk, confirmation_gates=gates)


def no_slippage(risk: RiskConfig) -> RiskConfig:
    """Diagnostic: the costs' share of the result."""
    tiers = [t.model_copy(update={"fraction": 0.0}) for t in risk.sim.slippage]
    return risk.model_copy(update={"sim": risk.sim.model_copy(update={"slippage": tiers})})


COMPARISONS = (
    Variant(1, "volume_off", "the gate's volume condition off", tuple(Strategy), volume_off),
    Variant(2, "structural_stop", "the stop at the structural level", CANDIDATE_2, structural_stop),
)


# --- The gate ---------------------------------------------------------------------------------


def blocks(p: PoolInputs) -> Callable[[date], int]:
    """Each session's 21-session block, counted from the first session of ``FIRST_YEAR``. 2016
    has 252 sessions, 12 blocks, so counting from the history's first session gives the same
    blocks."""
    first = next(d for d in p.days if d.year >= FIRST_YEAR)
    start = p.position[first]
    return lambda day: (p.position[day] - start) // BLOCK_SESSIONS


def secondary(trades: Iterable[PoolTrade], block_of: Callable[[date], int]) -> Interval | None:
    """The 21-session-block interval of the expectancy (reported, decides nothing)."""
    by_session: dict[date, list[float]] = defaultdict(list)
    for t in trades:
        if (r := t.r) is not None:
            by_session[t.entry_day].append(r)
    return block_bootstrap(
        [by_session],
        lambda g: statistics.fmean(g[0]) if g[0] else None,
        samples=BOOTSTRAP_SAMPLES,
        confidence=BOOTSTRAP_CONFIDENCE,
        seed=BOOTSTRAP_SEED,
        block_of=block_of,
    )


@dataclass(frozen=True, slots=True)
class Verdict:
    """A pool of closed trades against the pre-registered gate (section 8.1)."""

    expectancy: Expectancy
    secondary: Interval | None
    years: dict[int, float | None]
    """The expectancy of each tuning year (None: no closed trade that year)."""
    without_best: float | None
    """The expectancy with the best year's trades left out."""

    @property
    def positive_years(self) -> int:
        return sum(1 for v in self.years.values() if v is not None and v > 0)

    @property
    def robust(self) -> bool:
        """Positive in at least 4 of the 6 years and >= 0 without the best one."""
        return (
            self.positive_years >= MIN_POSITIVE_YEARS
            and self.without_best is not None
            and self.without_best >= 0
        )

    @property
    def enough(self) -> bool:
        return self.expectancy.trades >= MIN_TRADES

    def passes(self, decision_check_ok: bool) -> bool:
        return self.expectancy.passes and self.robust and self.enough and decision_check_ok

    @property
    def one_year(self) -> bool:
        """Passes the interval but not the per-year rule: "passes on one year"."""
        return self.expectancy.passes and not self.robust


def verdict(trades: Sequence[PoolTrade], block_of: Callable[[date], int]) -> Verdict:
    closed = [t for t in trades if t.r is not None]
    by_year: dict[int, list[float]] = defaultdict(list)
    for t in closed:
        if t.r is not None:
            by_year[t.entry_day.year].append(t.r)
    years = {y: statistics.fmean(by_year[y]) if by_year.get(y) else None for y in TUNING_YEARS}
    scored = {y: v for y, v in years.items() if v is not None}
    without_best = None
    if scored:
        best = max(scored, key=lambda y: scored[y])
        rest = [r for y, rs in by_year.items() if y != best for r in rs]
        without_best = statistics.fmean(rest) if rest else None
    return Verdict(expectancy(closed), secondary(closed, block_of), years, without_best)


# --- Invariants (checked blind) -------------------------------------------------------------


def invariant_violations(trades: Iterable[PoolTrade]) -> list[str]:
    """What must hold for every filled close entry, whatever its outcome."""
    out = []
    for t in trades:
        r = t.result
        if not t.triggered or r is None:
            continue
        name = f"{t.listing.found.setup.symbol} {t.strategy.value} {t.entry_day}"
        order, fills = r.order, r.fills
        if order is None or not fills or fills[0].kind is not FillKind.ENTRY:
            out.append(f"{name}: no entry order or fill")
            continue
        if not order.stop < fills[0].price <= order.limit:
            out.append(f"{name}: fill {fills[0].price} not between the stop and the limit")
        if order.tranches.second != 0 or order.tranches.first < 1:
            out.append(f"{name}: not one tranche of at least one share")
        if r.risk <= 0:
            out.append(f"{name}: no risk")
        if r.gate is None or not r.gate.passed:
            out.append(f"{name}: filled without its gate")
        exits = [i for i, f in enumerate(fills) if f.kind is FillKind.EXIT]
        if exits and exits[0] != len(fills) - 1:
            out.append(f"{name}: a fill after the exit")
    return out


# --- The decision-price checks (section 6) ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecisionCheck:
    """The gate on the close against the gate on the last SIP trade at 15:40."""

    sampled: int
    compared: int
    disagree: int
    no_minutes: int
    gaps: tuple[float, ...]
    """|close - 15:40 price| / the 15:40 price, on the attempts both gates passed."""

    @property
    def rate(self) -> float | None:
        return self.disagree / self.compared if self.compared else None

    @property
    def ok(self) -> bool:
        return self.rate is not None and self.rate <= MAX_DISAGREEMENT


@dataclass(frozen=True, slots=True)
class PositionDayCheck:
    """3.8's owed check: a held session's soft-rule decisions on the 15:40 price vs the close."""

    sampled: int
    compared: int
    disagree: int
    no_minutes: int

    @property
    def rate(self) -> float | None:
        return self.disagree / self.compared if self.compared else None


def gated(trades: Iterable[PoolTrade]) -> list[PoolTrade]:
    """The attempts that reached the gate at the decision time."""
    return [t for t in trades if t.result is not None and t.result.gate is not None]


def sip_price_at_decision(p: PoolInputs, day: date, minutes: Minutes) -> float | None:
    """The last SIP trade before the decision time: the close of the last bar starting before
    it (no age limit, section 6)."""
    decision_at = p.times[day].close_at - DECISION_BEFORE_CLOSE
    before = [b for b in minutes.sip if b.start < decision_at]
    return before[-1].close if before else None


def _minutes_of(
    p: PoolInputs,
    pairs: Sequence[tuple[date, str]],
    minutes_for: MinutesFor,
    symbols_for: SymbolsFor,
) -> dict[tuple[date, str], Minutes]:
    """Each (session, security)'s minute bars, fetched once."""
    wanted: dict[date, set[str]] = defaultdict(set)
    for day, security in pairs:
        wanted[day].add(security)
    symbols = symbols_for(wanted)
    by_symbol: dict[date, set[str]] = defaultdict(set)
    for (day, _), traded in symbols.items():
        by_symbol[day].add(traded)
    got = minutes_for(by_symbol)
    out = {}
    for day, security in pairs:
        symbol = symbols.get((day, security))
        bars = got.get(day, {}).get(symbol) if symbol is not None else None
        if bars is not None:
            out[day, security] = bars
    return out


def decision_check(
    p: PoolInputs,
    trades: Sequence[PoolTrade],
    minutes_for: MinutesFor,
    symbols_for: SymbolsFor,
) -> DecisionCheck:
    attempts = gated(trades)
    sample = random.Random(SAMPLE_SEED).sample(  # noqa: S311 - sampling, not crypto
        attempts, min(SAMPLE_SIZE, len(attempts))
    )
    pairs = [(t.entry_day, t.listing.found.security) for t in sample]
    held = _minutes_of(p, pairs, minutes_for, symbols_for)
    market = p.market()
    compared = disagree = missing = 0
    gaps = []
    for t in sample:
        bars = held.get((t.entry_day, t.listing.found.security))
        price = sip_price_at_decision(p, t.entry_day, bars) if bars is not None else None
        if bars is None or price is None or t.approved is None or t.result is None:
            missing += 1
            continue
        sip_only = Minutes(bars.sip, None)
        at_1540 = play(
            p, t.listing, t.entry_day, t.approved, sip_only, market, decision_price=price
        )
        close_gate, gate_1540 = t.result.gate, at_1540.gate
        if close_gate is None or gate_1540 is None:
            missing += 1
            continue
        compared += 1
        if close_gate.passed != gate_1540.passed:
            disagree += 1
        elif close_gate.passed:
            s = p.h.series[t.listing.found.security]
            close = s.raw_close[s.dates.index(t.entry_day)]
            gaps.append(abs(close - price) / price)
    return DecisionCheck(len(sample), compared, disagree, missing, tuple(gaps))


def _session_decisions(result: TradeResult, n: int) -> tuple[tuple[Event, ...], tuple[Rule, ...]]:
    """Session ``n``'s plan events and its sales and exits by rules other than the hard stop."""
    events = tuple(e for s, e in result.events if s == n)
    rules = tuple(
        f.rule
        for f in result.fills
        if f.session == n and f.rule is not None and f.rule is not Rule.HARD_STOP
    )
    return events, rules


def position_day_check(
    p: PoolInputs,
    trades: Sequence[PoolTrade],
    minutes_for: MinutesFor,
    symbols_for: SymbolsFor,
) -> PositionDayCheck:
    held_days: list[tuple[PoolTrade, int, date]] = []
    for t in trades:
        if not t.triggered or t.result is None:
            continue
        k = p.position[t.entry_day]
        for n in range(2, len(t.result.marks) + 1):
            if k + n - 1 < len(p.days):
                held_days.append((t, n, p.days[k + n - 1]))
    sample = random.Random(SAMPLE_SEED).sample(  # noqa: S311 - sampling, not crypto
        held_days, min(SAMPLE_SIZE, len(held_days))
    )
    pairs = [(day, t.listing.found.security) for t, _, day in sample]
    held = _minutes_of(p, pairs, minutes_for, symbols_for)
    market = p.market()
    compared = disagree = missing = 0
    for t, n, day in sample:
        bars = held.get((day, t.listing.found.security))
        if bars is None or t.approved is None or t.result is None:
            missing += 1
            continue
        replayed = play(p, t.listing, t.entry_day, t.approved, None, market, later={day: bars})
        if len(replayed.marks) < n - 1:
            missing += 1
            continue
        compared += 1
        if _session_decisions(replayed, n) != _session_decisions(t.result, n):
            disagree += 1
    return PositionDayCheck(len(sample), compared, disagree, missing)


# --- The run ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CloseRun:
    reference: list[PoolTrade]
    compared: list[tuple[Variant, list[Difference], list[PoolTrade]]]
    no_chase: list[PoolTrade]
    no_cost: list[PoolTrade]
    decision: DecisionCheck
    positions: PositionDayCheck
    liquid: frozenset[tuple[str, date]]
    """(security, entry session) of the attempts at or above ``LIQUID_DOLLAR_VOLUME``."""
    block_of: Callable[[date], int]
    blind: bool


def liquid_attempts(p: PoolInputs, trades: Iterable[PoolTrade]) -> frozenset[tuple[str, date]]:
    out = set()
    for t in trades:
        f = t.listing.found
        dv = p.dollar_volume_at(f.security, known_at(f))
        if dv is not None and dv >= LIQUID_DOLLAR_VOLUME:
            out.add((f.security, t.entry_day))
    return frozenset(out)


def run_close(
    p: PoolInputs,
    lists: Mapping[date, Sequence[Listing]],
    minutes_for: MinutesFor,
    symbols_for: SymbolsFor,
    *,
    blind: bool,
    log: Callable[[str], None] = lambda _: None,
) -> CloseRun:
    """``p.risk`` is the candidate's config. Blind, the outcome-only runs are skipped."""
    reference = run_pool(p, lists, minutes_for, symbols_for)
    log(f"reference: {len(reference)} attempts")
    compared: list[tuple[Variant, list[Difference], list[PoolTrade]]] = []
    no_chase: list[PoolTrade] = []
    no_cost: list[PoolTrade] = []
    if not blind:
        for v in COMPARISONS:
            played = run_pool(_with(p, v.change(p.risk)), lists, minutes_for, symbols_for)
            compared.append((v, compare(v, reference, played), played))
            log(f"comparison {v.name}: done")
        no_chase = run_pool(_with(p, no_max_chase(p.risk)), lists, minutes_for, symbols_for)
        no_cost = run_pool(_with(p, no_slippage(p.risk)), lists, minutes_for, symbols_for)
    decision = decision_check(p, reference, minutes_for, symbols_for)
    log(f"decision-price check: {decision.disagree} of {decision.compared} disagree")
    positions = position_day_check(p, reference, minutes_for, symbols_for)
    log(f"position-day check: {positions.disagree} of {positions.compared} disagree")
    return CloseRun(
        reference,
        compared,
        no_chase,
        no_cost,
        decision,
        positions,
        liquid_attempts(p, reference),
        blocks(p),
        blind,
    )


def _with(p: PoolInputs, risk: RiskConfig) -> PoolInputs:
    return replace(p, risk=risk)
