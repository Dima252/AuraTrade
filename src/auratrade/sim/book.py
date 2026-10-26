"""The momentum book on daily bars (README §5.3, §7, §8; #147-#149; task 3.12), pure.

The book of `docs/backtests/momentum-preregistration.md` §3.3-§3.8, played session by session
from a cash start. Every rule is the live pure function's: the ranking, hold zone, candidates,
rule-book choice, slots, limits and stops (`strategies.momentum_book`), the halts, the overlay
and the buy guardrail (`guardrails.book`), and every fill goes through `sim.fills` with the
liquidity-tier slippage (#141). Nothing here re-implements them.

- **Each session:** a holding's stop is checked on its bar (from the session after the buy); a
  holding whose security ended exits (§3.8); a rule sell that had no bar on E sells at its next
  close. Then the book is marked at the close.
- **On E:** the halts at E's close; the ranking on R; rule sells at the close; stop raises for
  the holdings kept; then the buys: the candidates and the rule book's picks, through the buy
  guardrail with every block in force, so a blocked session buys nothing.
- **The retry:** buys blocked by a daily or weekly loss halt, or that left slots empty after
  passing over a name with no bar, are tried once more at the next session's close, with R's
  ranking and fresh sizes. An overlay or drawdown block isn't retried; a half day blocks it.
- **Units:** sizing, limits, ticks and stop levels use raw prices on the session they're set
  (#122); quantities, stops and marks are then carried split-adjusted, so a later split changes
  nothing. A stop is checked on the split-adjusted bar.
- **Securities that end** (#130): with a successor, at min(the last raw close, the successor's
  first raw open); without one, at the last close; minus slippage either way, booked on the
  first session the security has no bar.
- **Cluster data:** each name's closes on the last ``cluster_correlation_sessions + 1`` sessions
  up to R, a session without a bar carrying the close before it.
- **Invariants** (§7) are checked as the book runs; a failure raises `BookInvariantError`.

Slippage is the tier of the 50-session average dollar volume up to the session before the fill.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from auratrade.config import Regime, RiskConfig
from auratrade.guardrails.blocks import EntryBlock
from auratrade.guardrails.book import (
    BookAccount,
    BookBuy,
    book_halts,
    evaluate_buys,
    overlay_blocks,
)
from auratrade.guardrails.clusters import ClusterMember, cluster_check
from auratrade.guardrails.model import Outcome
from auratrade.sim.fills import buy_fill, sell_fill, slippage, stop_on_bar
from auratrade.strategies.momentum_book import (
    BookSecurity,
    Ranked,
    Rebalance,
    SessionDay,
    Signal,
    Skipped,
    SkipReason,
    candidates,
    keeps,
    raised_stop,
    rank,
    rebalances,
    rule_book_choice,
    slot_value,
)

# Float noise when carrying values between raw and split-adjusted units.
_TOLERANCE = 1e-9

RETRIED_HALTS = frozenset({EntryBlock.DAILY_LOSS_HALT, EntryBlock.WEEKLY_LOSS_HALT})


class BookInvariantError(RuntimeError):
    """A pre-registered invariant (§7) failed: a bug in the book, never a market outcome."""


class ExitReason(StrEnum):
    STOP = "STOP"
    RULE = "RULE"
    """Out of the hold zone or the ranked set at a rebalance."""
    ENDED_SUCCESSOR = "ENDED_SUCCESSOR"
    """Ended by the history's split, with a successor piece (#130)."""
    ENDED = "ENDED"
    """Stopped trading before the period's end, without a successor: a delisting."""


@dataclass(frozen=True, slots=True)
class BookSeries:
    """One security's bars, oldest first: split-adjusted OHLCV plus the close as traded."""

    key: str
    symbol: str
    dates: Sequence[date]
    open: Sequence[float]
    high: Sequence[float]
    low: Sequence[float]
    close: Sequence[float]
    raw_close: Sequence[float]
    volume: Sequence[float]


@dataclass(frozen=True, slots=True)
class BookMarket:
    """Everything the book reads, keyed by security key unless named otherwise."""

    sessions: Sequence[SessionDay]
    """The calendar, oldest first, from the warm-up through the period's end."""
    series: Mapping[str, BookSeries]
    members: Mapping[date, Collection[str]]
    """The point-in-time universe per session (README §5.1)."""
    regimes: Mapping[date, Regime]
    """The computed regime on each session's evening."""
    crisis: Collection[date] = frozenset()
    """The evenings on which the crisis override is active."""
    industries: Mapping[str, str] = field(default_factory=dict)
    successors: Mapping[str, str] = field(default_factory=dict)
    """A security ended by the history's split -> its next piece."""
    spy_above_sma: Mapping[date, bool] = field(default_factory=dict)
    """Comparison C4: SPY's close above its long SMA, per session."""


@dataclass(slots=True)
class BookTrade:
    """One position, from its buy to its exit (or open at the end)."""

    key: str
    symbol: str
    shares: int
    """Whole shares as bought."""
    entered: date
    entry_price: float
    """The fill, as traded."""
    cost: float
    limit: float
    stop: float
    """The initial stop, as traded."""
    exited: date | None = None
    exit_price: float | None = None
    """As traded on the exit session."""
    proceeds: float | None = None
    reason: ExitReason | None = None
    slippage: float = 0.0
    """Dollars paid to slippage on the buy and the sell."""


@dataclass(slots=True)
class RebalanceLog:
    """One E (or its retry): counts for the blind check."""

    ranking: date
    execution: date
    session: date
    """The session the buys were tried on: E, or the retry's."""
    retry: bool
    blocks: frozenset[EntryBlock]
    ranked: int = 0
    rule_sells: int = 0
    stop_raises: int = 0
    candidates: int = 0
    picks: int = 0
    """The rule book's choices, before the guardrail (blocked ones included)."""
    bought: int = 0
    skipped: tuple[Skipped, ...] = ()
    unfilled: int = 0
    """Picks whose fill would have been above the limit."""
    holdings: int = 0
    """Holdings after the buys."""
    retried: bool = False
    """This session's buys were tried again on the next session."""


@dataclass(frozen=True, slots=True)
class SessionMark:
    day: date
    equity: float
    cash: float
    holdings: int


@dataclass(frozen=True, slots=True)
class BookRun:
    marks: tuple[SessionMark, ...]
    """The start's close, then every session's close, through the end."""
    trades: tuple[BookTrade, ...]
    rebalances: tuple[RebalanceLog, ...]


@dataclass(slots=True)
class _Position:
    trade: BookTrade
    qty: float
    """Split-adjusted shares."""
    stop: float
    """Split-adjusted."""
    last: int
    """The index of the last bar seen."""
    sell_pending: bool = False


@dataclass(frozen=True, slots=True)
class _Bar:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class _Retry:
    rebalance: Rebalance
    ranking: tuple[Ranked, ...]
    left: int


def _retried_halt(blocks: frozenset[EntryBlock]) -> bool:
    """Blocked by a daily or weekly loss halt and nothing else: the buys are retried."""
    return bool(blocks & RETRIED_HALTS) and not blocks - RETRIED_HALTS


def _index(s: BookSeries, day: date) -> int | None:
    i = bisect.bisect_left(s.dates, day)
    return i if i < len(s.dates) and s.dates[i] == day else None


def _factor(s: BookSeries, i: int) -> float:
    """Raw / split-adjusted on bar ``i``."""
    return s.raw_close[i] / s.close[i]


def _dollar_volume(s: BookSeries, i: int, window: int) -> float | None:
    """The average of ``window`` bars' close x volume up to bar ``i`` (split-adjusted close
    times split-adjusted volume is the raw dollar volume)."""
    if i + 1 < window or i < 0:
        return None
    return sum(s.close[j] * s.volume[j] for j in range(i - window + 1, i + 1)) / window


class _Members(Mapping[str, ClusterMember]):
    """Cluster data by symbol, built on first use: only the names the lists reach need it."""

    def __init__(self, book: _Book, keys: Mapping[str, str], ranking: date) -> None:
        self._book, self._keys, self._ranking = book, keys, ranking
        self._cache: dict[str, ClusterMember] = {}

    def __getitem__(self, symbol: str) -> ClusterMember:
        if symbol not in self._cache:
            self._cache[symbol] = self._book.member(self._keys[symbol], self._ranking)
        return self._cache[symbol]

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


class _Book:
    def __init__(
        self,
        market: BookMarket,
        risk: RiskConfig,
        *,
        equity: float,
        signal: Signal,
        clusters: bool,
    ) -> None:
        self.market, self.risk, self.cfg = market, risk, risk.book
        self.signal, self.clusters = signal, clusters
        self.days = [s.day for s in market.sessions]
        self.position_of = {d: i for i, d in enumerate(self.days)}
        self.early = {s.day for s in market.sessions if s.is_early_close}
        self.cash = equity
        self.positions: dict[str, _Position] = {}
        self.trades: list[BookTrade] = []
        self.logs: list[RebalanceLog] = []
        self.marks: list[SessionMark] = []
        self.peak = equity
        self.previous = equity
        self.week_base = equity
        self.limit_slippage = risk.execution.marketable_limit_slippage

    # --- data ---------------------------------------------------------------------------

    def slip(self, s: BookSeries, i: int) -> float:
        return slippage(_dollar_volume(s, i - 1, self.risk.sim.volume_sessions), self.risk.sim)

    def member(self, key: str, ranking: date) -> ClusterMember:
        s = self.market.series[key]
        n = self.risk.guardrails.cluster_correlation_sessions + 1
        end = self.position_of[ranking]
        window = self.days[max(end - n + 1, 0) : end + 1]
        closes: list[float] = []
        i = bisect.bisect_right(s.dates, window[0]) - 1
        last = s.close[i] if i >= 0 else math.nan
        for day in window:
            j = _index(s, day)
            if j is not None:
                last = s.close[j]
            closes.append(last)
        return ClusterMember(s.symbol, self.market.industries.get(key), closes)

    def ranking(self, day: date) -> list[Ranked]:
        window = self.risk.universe.dollar_volume_sessions
        keep = self.cfg.formation_sessions + 1
        securities = []
        for key in self.market.members.get(day, ()):
            s = self.market.series.get(key)
            if s is None:
                continue
            i = bisect.bisect_right(s.dates, day) - 1
            if i < 0:
                continue
            securities.append(
                BookSecurity(
                    key,
                    s.symbol,
                    s.close[max(i - keep + 1, 0) : i + 1],
                    _dollar_volume(s, i, window),
                    in_universe=True,
                )
            )
        return rank(securities, self.cfg, self.signal)

    def equity(self) -> float:
        return self.cash + self.exposure()

    def exposure(self) -> float:
        return sum(p.qty * self.market.series[k].close[p.last] for k, p in self.positions.items())

    # --- exits --------------------------------------------------------------------------

    def close_position(
        self, key: str, day: date, *, raw_price: float, factor: float, reason: ExitReason
    ) -> None:
        """Sell the whole holding at ``raw_price`` (as traded, slippage included) on a bar whose
        raw / adjusted factor is ``factor``."""
        p = self.positions.pop(key)
        proceeds = p.qty / factor * raw_price
        t = p.trade
        t.exited, t.exit_price, t.proceeds, t.reason = day, raw_price, proceeds, reason
        self.cash += proceeds

    def sell_at_close(self, key: str, i: int, day: date, reason: ExitReason) -> None:
        s = self.market.series[key]
        slip = self.slip(s, i)
        price = sell_fill(s.raw_close[i], slip)
        p = self.positions[key]
        f = _factor(s, i)
        p.trade.slippage += p.qty / f * (s.raw_close[i] - price)
        self.close_position(key, day, raw_price=price, factor=f, reason=reason)

    def end_position(self, key: str, day: date) -> None:
        """A holding whose security ended before ``day`` (§3.8)."""
        p = self.positions[key]
        s = self.market.series[key]
        last = len(s.dates) - 1
        price = s.raw_close[last]
        reason = ExitReason.ENDED
        successor = self.market.series.get(self.market.successors.get(key, ""))
        if successor is not None and successor.dates:
            price = min(price, successor.open[0] * _factor(successor, 0))
            reason = ExitReason.ENDED_SUCCESSOR
        slip = self.slip(s, last + 1)
        fill = sell_fill(price, slip)
        f = _factor(s, last)
        p.trade.slippage += p.qty / f * (price - fill)
        self.close_position(key, day, raw_price=fill, factor=f, reason=reason)

    def session_exits(self, day: date) -> None:
        """Stops, ended securities and pending rule sells, before the close. They run before
        the session's buys, so a stop is active from the session after its buy."""
        for key in sorted(self.positions):
            p = self.positions[key]
            s = self.market.series[key]
            i = _index(s, day)
            if i is None:
                if s.dates[-1] < day:
                    self.end_position(key, day)
                continue
            p.last = i
            bar = _Bar(s.open[i], s.high[i], s.low[i], s.close[i])
            fill = stop_on_bar(bar, stop=p.stop, slip=self.slip(s, i))
            if fill is not None:
                p.trade.slippage += p.qty * (min(p.stop, s.open[i]) - fill)
                f = _factor(s, i)
                self.close_position(key, day, raw_price=fill * f, factor=f, reason=ExitReason.STOP)
                continue
            if p.sell_pending:
                self.sell_at_close(key, i, day, ExitReason.RULE)

    # --- the rebalance ------------------------------------------------------------------

    def halts(self, equity: float) -> frozenset[EntryBlock]:
        return book_halts(
            day_return=equity / self.previous - 1,
            week_return=equity / self.week_base - 1,
            drawdown=1 - equity / max(self.peak, equity),
            halts=self.risk.halts,
            cfg=self.cfg,
        )

    def overlay(self, ranking: date) -> frozenset[EntryBlock]:
        return overlay_blocks(
            self.cfg,
            effective_regime=self.market.regimes.get(ranking),
            crisis=ranking in self.market.crisis,
            spy_above_sma=self.market.spy_above_sma.get(ranking),
        )

    def rebalance(self, r: Rebalance) -> _Retry | None:
        day = r.execution
        blocks = self.halts(self.equity()) | self.overlay(r.ranking)
        ranking = self.ranking(r.ranking)
        by_key = {x.key: x for x in ranking}
        log = RebalanceLog(r.ranking, day, day, retry=False, blocks=blocks, ranked=len(ranking))
        for key in sorted(self.positions):
            p = self.positions[key]
            s = self.market.series[key]
            i = _index(s, day)
            if not keeps(key, by_key, self.cfg):
                log.rule_sells += 1
                if i is None:
                    p.sell_pending = True
                else:
                    self.sell_at_close(key, i, day, ExitReason.RULE)
                continue
            if i is None:
                continue
            f = _factor(s, i)
            stop = raised_stop(p.stop * f, s.raw_close[i], self.cfg) / f
            if stop < p.stop - _TOLERANCE:
                raise BookInvariantError(f"{key}: stop lowered from {p.stop} to {stop} on {day}")
            if stop > p.stop + _TOLERANCE:
                p.stop = stop
                log.stop_raises += 1
        self.buy(r, tuple(ranking), blocks, log)
        self.logs.append(log)
        halted = _retried_halt(blocks)
        no_bar = not blocks and any(x.reason is SkipReason.NO_PRICE for x in log.skipped)
        open_slots = len(self.positions) < self.cfg.holdings
        if (halted or (no_bar and open_slots)) and self.cfg.buy_retry_sessions > 0:
            log.retried = True
            return _Retry(r, tuple(ranking), self.cfg.buy_retry_sessions)
        return None

    def retry(self, pending: _Retry, day: date) -> _Retry | None:
        r = pending.rebalance
        blocks = self.halts(self.equity()) | self.overlay(r.ranking)
        if day in self.early:
            blocks |= {EntryBlock.EARLY_CLOSE}
        log = RebalanceLog(
            r.ranking, r.execution, day, retry=True, blocks=blocks, ranked=len(pending.ranking)
        )
        self.buy(r, pending.ranking, blocks, log)
        self.logs.append(log)
        if pending.left > 1 and _retried_halt(blocks):
            log.retried = True
            return _Retry(r, pending.ranking, pending.left - 1)
        return None

    def buy(
        self,
        r: Rebalance,
        ranking: Sequence[Ranked],
        blocks: frozenset[EntryBlock],
        log: RebalanceLog,
    ) -> None:
        day = log.session
        series = self.market.series
        bars = {x.key: i for x in ranking if (i := _index(series[x.key], day)) is not None}
        prices = {k: series[k].raw_close[i] for k, i in bars.items()}
        keys = {x.symbol: x.key for x in ranking} | {
            p.trade.symbol: k for k, p in self.positions.items()
        }
        members = _Members(self, keys, r.ranking)
        holdings = [members[p.trade.symbol] for p in self.positions.values()]
        equity = self.equity()
        slot = slot_value(equity, self.cfg)
        listed = candidates(
            ranking,
            held=self.positions.keys(),
            prices=prices,
            slot=slot,
            limit_slippage=self.limit_slippage,
            holdings=holdings,
            members=members,
            guard=self.risk.guardrails,
            count=self.cfg.candidates,
            clusters=self.clusters,
        )
        choice = rule_book_choice(
            listed.candidates,
            open_slots=self.cfg.holdings - len(self.positions),
            cash=self.cash,
            slot=slot,
            prices=prices,
            limit_slippage=self.limit_slippage,
            holdings=holdings,
            members=members,
            guard=self.risk.guardrails,
            cfg=self.cfg,
            clusters=self.clusters,
        )
        log.candidates, log.picks = len(listed.candidates), len(choice.picks)
        log.skipped = listed.skipped + choice.skipped
        decisions = evaluate_buys(
            [BookBuy(p.ranked.symbol) for p in choice.picks],
            candidates=[c.symbol for c in listed.candidates],
            account=BookAccount(
                equity=equity,
                cash=self.cash,
                exposure=equity - self.cash,
                held=frozenset(p.trade.symbol for p in self.positions.values()),
                pending=frozenset(),
            ),
            prices={x.symbol: prices.get(x.key) for x in listed.candidates},
            blocks=blocks,
            holdings=holdings,
            members=members,
            cfg=self.cfg,
            guard=self.risk.guardrails,
            limit_slippage=self.limit_slippage,
            clusters=self.clusters,
        )
        bought: list[ClusterMember] = []
        for pick, decision in zip(choice.picks, decisions, strict=True):
            if decision.outcome is Outcome.REJECT:
                continue
            if blocks:
                raise BookInvariantError(f"a buy on {day} despite {sorted(blocks)}")
            if decision.limit is None or decision.stop is None:
                raise BookInvariantError(f"{pick.ranked.symbol}: an approval without levels")
            key = pick.ranked.key
            s = series[key]
            i = bars[key]
            slip = self.slip(s, i)
            fill = buy_fill(s.raw_close[i], slip)
            if fill > decision.limit:
                log.unfilled += 1
                continue
            if decision.stop >= fill:
                raise BookInvariantError(f"{key}: stop {decision.stop} not below the fill {fill}")
            member = members[pick.ranked.symbol]
            if self.clusters and cluster_check(member, holdings + bought, self.risk.guardrails):
                raise BookInvariantError(f"{key}: bought into a full cluster on {day}")
            bought.append(member)
            f = _factor(s, i)
            cost = decision.shares * fill
            self.cash -= cost
            trade = BookTrade(
                key,
                s.symbol,
                decision.shares,
                day,
                fill,
                cost,
                decision.limit,
                decision.stop,
                slippage=decision.shares * (fill - s.raw_close[i]),
            )
            self.trades.append(trade)
            self.positions[key] = _Position(trade, decision.shares * f, decision.stop / f, i)
            log.bought += 1
        log.holdings = len(self.positions)

    # --- the loop -----------------------------------------------------------------------

    def check(self, day: date) -> None:
        if len(self.positions) > self.cfg.holdings:
            raise BookInvariantError(f"{len(self.positions)} holdings on {day}")
        if self.cash < -_TOLERANCE:
            raise BookInvariantError(f"cash {self.cash} on {day}")

    def run(self, start: date, end: date, offset: int) -> BookRun:
        if start not in self.position_of or end not in self.position_of:
            raise ValueError("start and end must be sessions of the calendar")
        due = {r.execution: r for r in rebalances(self.market.sessions, offset)}
        self.marks.append(SessionMark(start, self.cash, self.cash, 0))
        pending: _Retry | None = None
        for day in self.days[self.position_of[start] + 1 : self.position_of[end] + 1]:
            if day.isocalendar()[:2] != self.marks[-1].day.isocalendar()[:2]:
                self.week_base = self.previous
            self.session_exits(day)
            if pending is not None:
                pending = self.retry(pending, day)
            elif (r := due.get(day)) is not None:
                pending = self.rebalance(r)
            equity = self.equity()
            self.check(day)
            self.marks.append(SessionMark(day, equity, self.cash, len(self.positions)))
            self.previous = equity
            self.peak = max(self.peak, equity)
        return BookRun(tuple(self.marks), tuple(self.trades), tuple(self.logs))


def simulate_book(
    market: BookMarket,
    risk: RiskConfig,
    *,
    start: date,
    end: date,
    equity: float,
    signal: Signal = Signal.TWELVE_ONE,
    clusters: bool = True,
    offset: int = 1,
) -> BookRun:
    """The book from ``equity`` in cash at ``start``'s close, marked through ``end``'s close.
    ``risk.book`` carries the stop, overlay and universe size of a comparison; ``signal`` is C1,
    ``clusters=False`` C7, ``offset`` the timing-luck diagnostic."""
    book = _Book(market, risk, equity=equity, signal=signal, clusters=clusters)
    return book.run(start, end, offset)
