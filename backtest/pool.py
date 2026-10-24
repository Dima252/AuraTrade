"""The setup pool of the 3.8 baseline backtest (pre-registration step 4): every ranked setup
played under its strategy's default entry policy, exit plan and risk, for the baseline gate.

- **The lists:** each session's full scan scored and capped as the evening does (the top 40 of
  the computed regime's fundable strategies, #138); "conviction >= 4" is the top quarter by
  score, as the baseline book reads it (#142).
- **Units:** one trade per (security, strategy, structure), the scorecard's unit. A unit is
  attempted on the session after each scan that lists it until it fills. An EP (the gap-session
  proxy for the morning list) is attempted on its gap session.
- **Setup-level guardrails only:** each attempt goes through the live ``guardrails.evaluate``
  alone, on an empty book at $5,000, multiplier 1, no strategy record: price levels, stop width,
  the 50-day extension, the base count and the earnings blackout apply; heat, exposure,
  position and cluster limits can't bind. The market's entry blocks (a half day, the crisis
  override) apply; the book's loss halts don't. The regime is the one computed the evening
  before the entry session.
- **No report dates:** the history has EDGAR earnings dates only for securities still
  trading (#130); live, every tracked ticker has a calendar. A security with none is read
  as having no report within the blackout (``reject_without_reports`` False), not
  rejected, or the pool would hold almost no delisted names. ``True`` keeps the live rule
  (fail closed), the survivors-only figure the report shows beside it.
- **Fills:** ``sim.trade.simulate_trade``. The entry session needs minute bars; they are
  fetched only where its daily high reaches the pivot (a stop-limit can't fill otherwise; such
  an attempt is ``NOT_REACHED``) and for every EP. Later sessions run on daily bars.
- **Units of price:** the history is split-adjusted to today and minute bars are as traded, so
  each trade runs in its entry session's traded units: the adjusted bars and levels times the
  session's raw / adjusted close. Whole shares are then counted at the real prices.
"""

from __future__ import annotations

import bisect
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum

from auratrade.calendar.sessions import Session
from auratrade.config import Regime, RiskConfig, Strategy
from auratrade.guardrails.blocks import EntryBlock
from auratrade.guardrails.evaluate import ApprovedEntry, Context, SetupFacts, evaluate
from auratrade.guardrails.model import Finding, GuardrailRule, Outcome
from auratrade.history.read import HistoryBar
from auratrade.history.store import bar_key, from_bytes
from auratrade.indicators.bars import OHLCV
from auratrade.indicators.series import rolling_mean
from auratrade.indicators.volatility import atr
from auratrade.journal import baseline
from auratrade.journal.evening import HISTORY_SESSIONS, report_distances
from auratrade.journal.scorecard import Interval, block_bootstrap
from auratrade.plans.cushion import EarningsTiming as CushionTiming
from auratrade.plans.model import reached
from auratrade.regime.classifier import RegimeResult
from auratrade.regime.state import early_uptrend_stepped
from auratrade.sim.minutes import MinuteCache
from auratrade.sim.sessions import CalendarDay, Market, Minutes, SymbolDays, sim_sessions
from auratrade.sim.trade import TradeOutcome, TradeResult, TradeSetup, simulate_trade
from auratrade.storage import ObjectStore
from auratrade.strategies.episodic_pivot import EpisodicPivotDetail
from auratrade.strategies.leader_pullback import PullbackDetail
from auratrade.strategies.ranking import score_pool, select
from auratrade.strategies.verdict_cache import structure
from backtest.history_scan import Found, History

POOL_EQUITY = 5_000.0
HORIZONS = (130, 520)
"""Sessions simulated after the entry; a trade still open is played again over the next span,
then to the end of the data."""
NO_REPORT_KNOWN = 10**6
"""``next_report_in`` for a security without report dates: none falls in the blackout."""
BOOTSTRAP_SAMPLES = 2_000
BOOTSTRAP_CONFIDENCE = 0.90
BOOTSTRAP_SEED = 38


@dataclass(frozen=True, slots=True)
class Listing:
    """A setup with a rank in its scan session's capped list."""

    found: Found
    scan_day: date
    rank: int
    score: float
    conviction: int


def capped_lists(
    h: History,
    found: Iterable[Found],
    regimes: Mapping[date, Regime],
    risk: RiskConfig,
    cap: int = 40,
) -> dict[date, list[Listing]]:
    """Each scan session's capped list, in rank order, selected on that session's regime."""
    by_day: dict[date, list[Found]] = defaultdict(list)
    for f in found:
        if f.features is None:
            raise ValueError("the pool needs the scan's features")
        by_day[h.series[f.security].dates[f.index]].append(f)
    out = {}
    for day, pool in sorted(by_day.items()):
        regime = regimes.get(day)
        if regime is None:
            continue
        scored = score_pool([(f.setup, f.features) for f in pool if f.features is not None], risk)
        chosen = select(scored, regime, risk, cap)
        by_id = {id(s.setup): f for s, f in zip(scored, pool, strict=True)}
        top = baseline.convictions([s.score for s in chosen])
        out[day] = [
            Listing(by_id[id(s.setup)], day, rank, s.score, conviction)
            for rank, (s, conviction) in enumerate(zip(chosen, top, strict=True), start=1)
        ]
    return out


class Status(StrEnum):
    REJECTED = "REJECTED"
    """A setup-level guardrail (or a market block) rejected it."""
    NOT_REACHED = "NOT_REACHED"
    """The entry session's high never reached the pivot."""
    NO_MINUTES = "NO_MINUTES"
    """The vendor has no minute bars for the entry session."""
    SKIPPED = "SKIPPED"
    MISSED = "MISSED"
    CLOSED = "CLOSED"
    OPEN = "OPEN"


@dataclass(frozen=True, slots=True)
class PoolTrade:
    listing: Listing
    entry_day: date
    regime: Regime
    """The regime computed the evening before the entry session."""
    status: Status
    findings: tuple[Finding, ...] = ()
    approved: ApprovedEntry | None = None
    result: TradeResult | None = None

    @property
    def strategy(self) -> Strategy:
        return self.listing.found.setup.strategy

    @property
    def r(self) -> float | None:
        """The R of a closed trade; None otherwise (open trades are kept out of the gate)."""
        if self.status is not Status.CLOSED or self.result is None:
            return None
        return self.result.r

    @property
    def triggered(self) -> bool:
        return self.status in (Status.CLOSED, Status.OPEN)


def unit(listing: Listing) -> tuple[str, Strategy, float | int]:
    return (listing.found.security, listing.found.setup.strategy, structure(listing.found.setup))


@dataclass(slots=True)
class PoolInputs:
    """What the pool reads besides the history: the chosen regime run and the calendar."""

    h: History
    results: Mapping[date, RegimeResult]
    calendar: Sequence[Session]
    risk: RiskConfig
    reject_without_reports: bool = False
    days: list[date] = field(init=False)
    position: dict[date, int] = field(init=False)
    times: dict[date, Session] = field(init=False)
    _atr: dict[str, Sequence[float | None]] = field(default_factory=dict)
    _dollar_volume: dict[str, Sequence[float | None]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.days = [s.day for s in self.calendar]
        self.position = {d: k for k, d in enumerate(self.days)}
        self.times = {s.day: s for s in self.calendar}

    def next_session(self, day: date) -> date | None:
        k = self.position[day] + 1
        return self.days[k] if k < len(self.days) and self.days[k] <= self.h.sessions[-1] else None

    def previous_session(self, day: date) -> date | None:
        k = self.position[day] - 1
        return self.days[k] if k >= 0 else None

    def atr_at(self, security: str, i: int) -> float | None:
        if security not in self._atr:
            bars = self.h.series[security].bars()
            self._atr[security] = atr(bars, self.risk.watchlist.atr_sessions)
        return self._atr[security][i]

    def dollar_volume_at(self, security: str, i: int) -> float | None:
        if security not in self._dollar_volume:
            s = self.h.series[security]
            values = [c * v for c, v in zip(s.close, s.volume, strict=True)]
            self._dollar_volume[security] = rolling_mean(values, self.risk.sim.volume_sessions)
        return self._dollar_volume[security][i]

    def market(self) -> Market:
        last = self.h.sessions[-1]
        return Market(
            calendar=[
                CalendarDay(s.day, s.open_at, s.close_at, s.is_early_close)
                for s in self.calendar
                if s.day <= last
            ],
            regimes={d: r.regime for d, r in self.results.items()},
            crises={d: r.crisis.active for d, r in self.results.items()},
            benchmark=self.h.spy,
        )


def entry_day(p: PoolInputs, listing: Listing) -> date | None:
    """An EP's gap session; the session after the scan for every other strategy."""
    if listing.found.setup.strategy is Strategy.EPISODIC_PIVOT:
        return listing.scan_day
    return p.next_session(listing.scan_day)


def facts(p: PoolInputs, listing: Listing, entry: date) -> SetupFacts:
    """The setup as the guardrails read it, in the entry session's traded units."""
    f = listing.found
    s = p.h.series[f.security]
    k = scale(p.h, f.security, entry)
    feats = f.features
    if feats is None:
        raise ValueError("the pool needs the scan's features")
    adr = feats.adr
    if isinstance(f.setup.detail, EpisodicPivotDetail):
        adr = f.setup.detail.pre_gap_adr
    extension = feats.extension
    close = s.close[f.index]
    atr_value = p.atr_at(f.security, f.index)
    known = reports(p.h, f.security)
    if known or p.reject_without_reports:
        next_in, last_ago = report_distances(known, entry, p.days)
    else:
        next_in, last_ago = NO_REPORT_KNOWN, None
    return SetupFacts(
        symbol=f.setup.symbol,
        strategy=f.setup.strategy,
        pivot=f.setup.pivot * k,
        structural_stop=f.setup.structural_stop * k,
        price=close * k,
        atr=None if atr_value is None else atr_value * k,
        adr=adr,
        sma_50=None if extension is None else close * k / (1 + extension),
        base_stage=feats.base_stage,
        base_reliable=feats.base_reliable,
        industry=p.h.groups.get(f.security),
        closes=(),
        next_report_in=next_in,
        last_report_ago=last_ago,
    )


def reports(h: History, security: str) -> tuple[tuple[date, CushionTiming], ...]:
    """The security's EDGAR report dates, as the earnings cushion reads them."""
    return tuple((d, CushionTiming(t.value.upper())) for d, t in h.earnings.get(security, []))


def scale(h: History, security: str, day: date) -> float:
    """Raw / adjusted close on ``day`` (or the last bar before it)."""
    s = h.series[security]
    i = max(bisect.bisect_right(s.dates, day) - 1, 0)
    return s.raw_close[i] / s.close[i]


def decide(
    p: PoolInputs, listing: Listing, entry: date, setup_facts: SetupFacts
) -> tuple[Regime, tuple[Finding, ...], ApprovedEntry | None]:
    """The attempt through the live guardrails, alone on an empty book."""
    evening = p.previous_session(entry)
    result = p.results.get(evening) if evening is not None else None
    if result is None:
        raise ValueError(f"no computed regime the evening before {entry}")
    step = p.risk.regimes.early_uptrend_step
    blocks = set()
    if p.times[entry].is_early_close:
        blocks.add(EntryBlock.EARLY_CLOSE)
    if result.crisis.active:
        blocks.add(EntryBlock.CRISIS_OVERRIDE)
    context = Context(
        equity=POOL_EQUITY,
        regime=result.regime,
        early_uptrend_stepped=early_uptrend_stepped(
            result.state,
            result.score,
            starter_sessions=step.starter_sessions,
            min_score=step.min_score_to_step_up,
        ),
        multiplier=1.0,
        blocks=frozenset(blocks),
        setups={(setup_facts.symbol, setup_facts.strategy): setup_facts},
        holdings={},
    )
    [proposal] = baseline.proposals([baseline.Listed(setup_facts, listing.score)])
    proposal = replace(proposal, conviction=listing.conviction)
    [decision] = evaluate([proposal], context, p.risk).decisions
    approved = decision.approved if isinstance(decision.approved, ApprovedEntry) else None
    return result.regime, decision.findings, approved


def reachable(p: PoolInputs, listing: Listing, entry: date) -> bool:
    """The entry session's daily high reaches the pivot (every EP is played)."""
    f = listing.found
    if f.setup.strategy is Strategy.EPISODIC_PIVOT:
        return True
    s = p.h.series[f.security]
    i = bisect.bisect_left(s.dates, entry)
    return i < len(s.dates) and s.dates[i] == entry and reached(s.high[i], f.setup.pivot)


# --- Minute bars ------------------------------------------------------------------------------


def traded_symbols(
    store: ObjectStore, wanted: Mapping[date, set[str]]
) -> dict[tuple[date, str], str]:
    """The symbol each security traded under on each session (the bar files keep it)."""
    out = {}
    for day, securities in sorted(wanted.items()):
        table = from_bytes(store.get(bar_key(day))).select(["security", "symbol"]).to_pydict()
        for security, symbol in zip(table["security"], table["symbol"], strict=True):
            if security in securities:
                out[day, security] = symbol
    return out


def prefetch(
    p: PoolInputs,
    cache: MinuteCache,
    wanted: Mapping[date, set[str]],
    log: Callable[[str], None] = lambda _: None,
) -> dict[date, dict[str, Minutes]]:
    """Each session's minute bars for its symbols (fetched once, then read from the cache)."""
    out = {}
    for n, (day, symbols) in enumerate(sorted(wanted.items()), start=1):
        t = p.times[day]
        out[day] = cache.minutes(day, symbols, open_at=t.open_at, close_at=t.close_at)
        if n % 100 == 0:
            log(f"minute bars: {n} of {len(wanted)} sessions")
    return out


# --- Playing a trade --------------------------------------------------------------------------


def _scaled(bar: HistoryBar | OHLCV, k: float) -> HistoryBar:
    return HistoryBar(bar.open * k, bar.high * k, bar.low * k, bar.close * k, bar.volume / k)


def play(
    p: PoolInputs,
    listing: Listing,
    entry: date,
    approved: ApprovedEntry,
    minutes: Minutes,
    market: Market,
) -> TradeResult:
    """``simulate_trade`` from the entry session, in its traded units, over a growing span."""
    f = listing.found
    s = p.h.series[f.security]
    k = scale(p.h, f.security, entry)
    e = bisect.bisect_left(s.dates, entry)
    start = max(e - HISTORY_SESSIONS, 0)
    bars = [_scaled(b, k) for b in s.bars()[start:]]
    dates = s.dates[start:]
    history = bars[: e - start]
    if not history:
        return TradeResult(TradeOutcome.SKIPPED)
    detail = f.setup.detail
    feats = f.features
    adr = feats.adr if feats is not None else None
    if isinstance(detail, EpisodicPivotDetail):
        adr = detail.pre_gap_adr
    atr_value = p.atr_at(f.security, f.index)
    setup = TradeSetup(
        entry=approved,
        code_pivot=f.setup.pivot * k,
        atr=None if atr_value is None else atr_value * k,
        adr=adr,
        dollar_volume=p.dollar_volume_at(f.security, f.index),
        history=history,
    )
    symbol = SymbolDays(dates, bars, reports(p.h, f.security))
    k0 = p.position[entry]
    result = TradeResult(TradeOutcome.SKIPPED)
    for span in (*HORIZONS, None):
        end = len(p.days) if span is None else k0 + span
        days = [d for d in p.days[k0:end] if d <= p.h.sessions[-1]]
        sessions = sim_sessions(
            f.setup.strategy,
            days,
            symbol=symbol,
            minutes={entry: minutes},
            market=market,
            risk=p.risk,
            tested_ema=detail.tested_ema if isinstance(detail, PullbackDetail) else None,
        )
        if not sessions:
            return TradeResult(TradeOutcome.SKIPPED)
        result = simulate_trade(setup, sessions, equity=POOL_EQUITY, record=None, risk=p.risk)
        if result.outcome is not TradeOutcome.OPEN or len(sessions) < len(days):
            break
        if span is not None and k0 + span >= len(p.days):
            break
    return result


_STATUS = {
    TradeOutcome.SKIPPED: Status.SKIPPED,
    TradeOutcome.MISSED: Status.MISSED,
    TradeOutcome.CLOSED: Status.CLOSED,
    TradeOutcome.OPEN: Status.OPEN,
}


def run_pool(
    p: PoolInputs,
    lists: Mapping[date, Sequence[Listing]],
    minutes_for: Callable[[Mapping[date, set[str]]], Mapping[date, Mapping[str, Minutes]]],
    symbols_for: Callable[[Mapping[date, set[str]]], Mapping[tuple[date, str], str]],
) -> list[PoolTrade]:
    """Every attempt of every listed unit, in session order. ``symbols_for`` maps the entry
    sessions' securities to their traded symbols; ``minutes_for`` gives those symbols' minute
    bars (both are I/O, so the caller wires them)."""
    attempts: list[tuple[Listing, date]] = []
    for day in sorted(lists):
        for listing in lists[day]:
            entry = entry_day(p, listing)
            if entry is not None:
                attempts.append((listing, entry))
    reach = {(id(listing), entry): reachable(p, listing, entry) for listing, entry in attempts}
    by_entry: dict[date, set[str]] = defaultdict(set)
    for listing, entry in attempts:
        if reach[id(listing), entry]:
            by_entry[entry].add(listing.found.security)
    symbols = symbols_for(by_entry)
    wanted: dict[date, set[str]] = defaultdict(set)
    for (day, _), symbol in symbols.items():
        wanted[day].add(symbol)
    minutes = minutes_for(wanted)
    market = p.market()

    trades: list[PoolTrade] = []
    filled: set[tuple[str, Strategy, float | int]] = set()
    for listing, entry in sorted(attempts, key=lambda a: (a[1], a[0].scan_day, a[0].rank)):
        u = unit(listing)
        if u in filled:
            continue
        setup_facts = facts(p, listing, entry)
        regime, findings, approved = decide(p, listing, entry, setup_facts)
        if approved is None:
            trades.append(PoolTrade(listing, entry, regime, Status.REJECTED, findings))
            continue
        if not reach[id(listing), entry]:
            trades.append(PoolTrade(listing, entry, regime, Status.NOT_REACHED, findings, approved))
            continue
        traded = symbols.get((entry, listing.found.security))
        bars = minutes.get(entry, {}).get(traded) if traded is not None else None
        if bars is None:
            trades.append(PoolTrade(listing, entry, regime, Status.NO_MINUTES, findings, approved))
            continue
        result = play(p, listing, entry, approved, bars, market)
        trade = PoolTrade(
            listing, entry, regime, _STATUS[result.outcome], findings, approved, result
        )
        trades.append(trade)
        if trade.triggered:
            filled.add(u)
    return trades


# --- The gate ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expectancy:
    trades: int
    sessions: int
    mean: float | None
    interval: Interval | None
    win_rate: float | None
    avg_gain: float | None
    avg_loss: float | None
    """In R (the average loss is negative)."""

    @property
    def passes(self) -> bool:
        """The interval excludes zero from above."""
        return self.interval is not None and self.interval.low > 0


def expectancy(trades: Iterable[PoolTrade]) -> Expectancy:
    """The mean R of the closed trades, with the session-block bootstrap interval."""
    by_session: dict[date, list[float]] = defaultdict(list)
    rs = []
    for t in trades:
        if (r := t.r) is not None:
            by_session[t.entry_day].append(r)
            rs.append(r)
    interval = block_bootstrap(
        [by_session],
        lambda g: statistics.fmean(g[0]) if g[0] else None,
        samples=BOOTSTRAP_SAMPLES,
        confidence=BOOTSTRAP_CONFIDENCE,
        seed=BOOTSTRAP_SEED,
    )
    gains = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return Expectancy(
        trades=len(rs),
        sessions=len(by_session),
        mean=statistics.fmean(rs) if rs else None,
        interval=interval,
        win_rate=len(gains) / len(rs) if rs else None,
        avg_gain=statistics.fmean(gains) if gains else None,
        avg_loss=statistics.fmean(losses) if losses else None,
    )


def rejections(trades: Iterable[PoolTrade]) -> dict[Strategy, dict[GuardrailRule, int]]:
    """How often each guardrail rejected, per strategy."""
    out: dict[Strategy, dict[GuardrailRule, int]] = defaultdict(lambda: defaultdict(int))
    for t in trades:
        if t.status is Status.REJECTED:
            rejected = [f for f in t.findings if f.outcome is Outcome.REJECT]
            if rejected:
                out[t.strategy][rejected[-1].rule] += 1
    return out
